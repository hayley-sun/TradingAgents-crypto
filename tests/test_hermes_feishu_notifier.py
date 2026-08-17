import dataclasses
import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import tradingagents.integrations.hermes_feishu_notifier as notifier
from tradingagents.integrations.hermes_feishu_client import (
    FeishuDeliveryError,
    FeishuNotifierConfig,
    ReportCardData,
)
from tradingagents.integrations.hermes_feishu_notifier import (
    CronExecution,
    ExecutionDiscoveryError,
    ReportDiscoveryError,
    discover_execution_events,
    discover_missing_archive_events,
    discover_report_events,
    load_cron_runs,
    load_verified_archives,
    parse_cron_runs,
)
from tradingagents.integrations.hermes_feishu_state import (
    DeliveryRecord,
    NotificationAlreadyRunning,
    NotificationEvent,
    NotificationStateError,
    NotificationStateStore,
    initialized_state,
)
from tradingagents.integrations.schemas import (
    DailyReportArchive,
    DailyReportArchiveItem,
    DailyReportBatch,
    DailyReportBatchItem,
    DailyReportRequest,
)


JOB_ID = "e93cfab5f78e"
ARCHIVE_JOB_ID = "5b7f7906306a"
SHANGHAI = ZoneInfo("Asia/Shanghai")
NOTIFIER_JOBS = {
    "daily_submit": "2d445dfc1a8a",
    "daily_archive": ARCHIVE_JOB_ID,
    "review_processor": "d6c0e087e5a8",
    "review_memory": "e93cfab5f78e",
}
RUNS_OUTPUT = """\
d5f80f1f5694484f8282bc746277a277  completed  job=e93cfab5f78e  source=direct  2026-08-14T17:30:57.290949+08:00
f9691db864e34293b6a68ea082967e45  failed     job=e93cfab5f78e  source=schedule  2026-08-14T17:14:56.700588+08:00
    DEEPSEEK_API_KEY=must-never-escape
"""


def cron_execution(
    execution_id,
    status,
    claimed_at,
    *,
    job_id=JOB_ID,
    source="schedule",
):
    return CronExecution(
        execution_id=execution_id,
        job_id=job_id,
        status=status,
        source=source,
        claimed_at=claimed_at,
    )


def state_with_cursor(job_id, execution_id):
    return initialized_state(
        datetime(2026, 8, 14, tzinfo=timezone.utc),
        {job_id: [execution_id] if execution_id is not None else []},
        [],
    )


def empty_notification_state():
    return initialized_state(
        datetime(2026, 8, 14, tzinfo=timezone.utc),
        {job_id: [] for job_id in NOTIFIER_JOBS.values()},
        [],
    )


def report_batch(
    trade_date,
    report_bytes,
    *,
    state="ready",
    archive=True,
    items=None,
):
    request = DailyReportRequest(
        trade_date=trade_date,
        symbols=["BTC", "ETH", "SOL"],
        analysts=["market", "news", "fundamentals"],
        research_depth=1,
        llm_provider="deepseek",
        quick_model="deepseek-v4-flash",
        deep_model="deepseek-v4-pro",
    )
    archive_items = items or [
        DailyReportArchiveItem(
            symbol=symbol,
            status="completed",
            processed_signal=f"{symbol} signal",
            final_trade_decision=f"{symbol} decision",
            error_code=None,
        )
        for symbol in request.symbols
    ]
    archived = (
        DailyReportArchive(
            filename=f"{trade_date.isoformat()}.md",
            sha256=hashlib.sha256(report_bytes).hexdigest(),
            state=state,
            archived_at=datetime.combine(trade_date, datetime.min.time(), SHANGHAI),
            items=archive_items,
            scheduled_review_version=2,
        )
        if archive
        else None
    )
    return DailyReportBatch(
        batch_id="report_" + "a" * 16,
        request=request,
        created_at=datetime.combine(trade_date, datetime.min.time(), SHANGHAI),
        items=[
            DailyReportBatchItem(
                symbol=symbol,
                session_id="hermes_" + character * 16,
            )
            for symbol, character in zip(request.symbols, "bcd")
        ],
        archive=archived,
    )


def persist_report_batch(root, batch, report_bytes=b"# Daily report\n"):
    batches = root / "hermes" / "report_batches"
    reports = root / "hermes" / "reports"
    batches.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    batch_path = batches / f"{batch.request.trade_date.isoformat()}.json"
    batch_path.write_text(batch.model_dump_json(), encoding="ascii")
    if batch.archive is not None:
        (reports / batch.archive.filename).write_bytes(report_bytes)
    return batch_path


def run_header(index, *, claimed_at=None):
    occurred_at = claimed_at or (
        datetime(2026, 8, 14, tzinfo=timezone.utc)
        + timedelta(minutes=index)
    )
    return (
        f"{index:032x}  completed  job={JOB_ID}  source=schedule  "
        f"{occurred_at.isoformat()}"
    )


def notifier_config():
    return FeishuNotifierConfig(
        version=1,
        webhook_url=(
            "https://open.feishu.cn/open-apis/bot/v2/hook/"
            "0123456789abcdef"
        ),
        signing_secret="test-secret",
        jobs=NOTIFIER_JOBS,
    )


def execution_histories(now, *, status="completed"):
    return {
        job_id: (
            cron_execution(
                f"{index:032x}",
                status,
                now - timedelta(minutes=index),
                job_id=job_id,
            ),
        )
        for index, job_id in enumerate(NOTIFIER_JOBS.values(), start=1)
    }


class HermesFeishuNotifierTests(unittest.TestCase):
    def test_parse_keeps_headers_and_discards_error_detail(self):
        records = parse_cron_runs(RUNS_OUTPUT, "e93cfab5f78e")
        self.assertEqual([item.status for item in records], ["completed", "failed"])
        self.assertNotIn("must-never-escape", repr(records))

    def test_parse_discards_indented_detail_with_header_metadata_words(self):
        marker = "opaque-detail-must-never-escape"
        output = (
            f"{run_header(1)}\n"
            f"    {marker} job={JOB_ID} source=schedule\n"
        )

        records = parse_cron_runs(output, JOB_ID)

        self.assertEqual(len(records), 1)
        self.assertNotIn(marker, repr(records))

    def test_parse_rejects_unknown_preface_or_wrong_job(self):
        with self.assertRaises(ExecutionDiscoveryError):
            parse_cron_runs("unexpected preface\n", "e93cfab5f78e")
        with self.assertRaises(ExecutionDiscoveryError):
            parse_cron_runs(RUNS_OUTPUT, "2d445dfc1a8a")

    def test_loader_uses_absolute_cli_and_limit_500(self):
        seen = []

        def run(command, **kwargs):
            seen.append((command, kwargs))
            return CompletedProcess(command, 0, RUNS_OUTPUT, "")

        load_cron_runs(
            "e93cfab5f78e",
            run_command=run,
            hermes_cli=Path("/home/ubuntu/.local/bin/hermes"),
        )
        self.assertEqual(
            seen[0][0],
            [
                "/home/ubuntu/.local/bin/hermes",
                "cron",
                "runs",
                "e93cfab5f78e",
                "--limit",
                "500",
            ],
        )
        self.assertEqual(
            seen[0][1],
            {
                "capture_output": True,
                "text": True,
                "timeout": 20,
                "check": False,
            },
        )

    def test_parse_accepts_exact_status_vocabulary(self):
        statuses = ("claimed", "running", "completed", "failed", "unknown")
        lines = []
        for index, status in enumerate(statuses, start=1):
            lines.append(
                f"{index:032x}  {status:<9} "
                f"job={JOB_ID}  source=schedule  "
                f"2026-08-14T17:{index:02d}:00+08:00"
            )

        records = parse_cron_runs("\n".join(lines) + "\n", JOB_ID)

        self.assertEqual(
            [record.status for record in records], list(reversed(statuses))
        )

    def test_parse_rejects_status_outside_exact_vocabulary(self):
        output = (
            f"{'a' * 32}  cancelled  job={JOB_ID}  source=schedule  "
            "2026-08-14T17:00:00+08:00\n"
        )

        with self.assertRaises(ExecutionDiscoveryError):
            parse_cron_runs(output, JOB_ID)

    def test_parse_rejects_malformed_follow_up_execution_headers(self):
        valid_header = run_header(1)
        malformed_headers = {
            "unknown status": (
                f"{'b' * 32}  cancelled  job={JOB_ID}  source=schedule  "
                "2026-08-14T17:00:00+08:00"
            ),
            "missing timestamp": (
                f"{'b' * 32}  failed     job={JOB_ID}  source=schedule"
            ),
            "malformed execution ID": (
                f"not-an-execution-id  failed     job={JOB_ID}  "
                "source=schedule  2026-08-14T17:00:00+08:00"
            ),
            "malformed job ID": (
                f"{'b' * 32}  failed     job=not-a-job-id  "
                "source=schedule  2026-08-14T17:00:00+08:00"
            ),
            "missing source": (
                f"{'b' * 32}  failed     job={JOB_ID}  "
                "2026-08-14T17:00:00+08:00"
            ),
            "reordered metadata": (
                f"{'b' * 32}  failed     source=schedule  "
                f"job={JOB_ID}  2026-08-14T17:00:00+08:00"
            ),
        }

        for case, malformed_header in malformed_headers.items():
            with self.subTest(case=case), self.assertRaises(
                ExecutionDiscoveryError
            ):
                parse_cron_runs(
                    f"{valid_header}\n{malformed_header}\n", JOB_ID
                )

    def test_parse_accepts_only_exact_no_records_line(self):
        self.assertEqual(
            parse_cron_runs("\nNo cron execution attempts recorded.\n\n", JOB_ID),
            (),
        )

        for output in (
            "No cron execution attempts recorded\n",
            "No cron execution attempts recorded. extra\n",
            "No cron execution attempts recorded.\nunexpected\n",
            "\n\n",
        ):
            with self.subTest(output=output), self.assertRaises(
                ExecutionDiscoveryError
            ):
                parse_cron_runs(output, JOB_ID)

    def test_parse_rejects_duplicate_execution_ids(self):
        line = (
            f"{'a' * 32}  completed  job={JOB_ID}  source=schedule  "
            "2026-08-14T17:00:00+08:00\n"
        )

        with self.assertRaises(ExecutionDiscoveryError):
            parse_cron_runs(line + line, JOB_ID)

    def test_parse_rejects_invalid_or_offset_naive_timestamps(self):
        for claimed_at in ("not-a-time", "2026-08-14T17:00:00"):
            output = (
                f"{'a' * 32}  completed  job={JOB_ID}  source=schedule  "
                f"{claimed_at}\n"
            )
            with self.subTest(claimed_at=claimed_at), self.assertRaises(
                ExecutionDiscoveryError
            ):
                parse_cron_runs(output, JOB_ID)

    def test_parse_rejects_non_string_job_id_safely(self):
        with self.assertRaises(ExecutionDiscoveryError) as raised:
            parse_cron_runs(RUNS_OUTPUT, 7)

        self.assert_safe_discovery_error(raised.exception)

    def test_parse_accepts_500_headers_and_rejects_501(self):
        five_hundred = "\n".join(run_header(index) for index in range(500))

        records = parse_cron_runs(five_hundred, JOB_ID)

        self.assertEqual(len(records), 500)
        self.assertEqual(records[0].execution_id, f"{499:032x}")
        self.assertEqual(records[-1].execution_id, f"{0:032x}")

        five_hundred_one = f"{five_hundred}\n{run_header(500)}\n"
        with self.assertRaises(ExecutionDiscoveryError) as raised:
            parse_cron_runs(five_hundred_one, JOB_ID)
        self.assert_safe_discovery_error(raised.exception)

    def test_parse_normalizes_newest_first_and_preserves_cli_tie_order(self):
        output = "\n".join(
            (
                f"{'a' * 32}  running   job={JOB_ID}  source=direct  "
                "2026-08-14T17:00:00+08:00",
                f"{'b' * 32}  failed    job={JOB_ID}  source=direct  "
                "2026-08-14T18:00:00+08:00",
                f"{'c' * 32}  claimed   job={JOB_ID}  source=direct  "
                "2026-08-14T18:00:00+08:00",
            )
        )

        records = parse_cron_runs(output, JOB_ID)

        self.assertEqual(
            [record.execution_id for record in records],
            ["b" * 32, "c" * 32, "a" * 32],
        )

    def test_cron_execution_is_frozen_and_has_only_safe_fields(self):
        execution = parse_cron_runs(RUNS_OUTPUT, JOB_ID)[0]

        self.assertEqual(
            [field.name for field in dataclasses.fields(CronExecution)],
            ["execution_id", "job_id", "status", "source", "claimed_at"],
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            execution.status = "failed"

    def test_cron_execution_rejects_invalid_direct_construction(self):
        aware = datetime(2026, 8, 14, tzinfo=timezone.utc)
        valid = {
            "execution_id": "a" * 32,
            "job_id": JOB_ID,
            "status": "completed",
            "source": "schedule",
            "claimed_at": aware,
        }
        invalid_values = (
            {"execution_id": "not-an-execution-id"},
            {"job_id": "not-a-job-id"},
            {"status": "cancelled"},
            {"source": "unsafe source"},
            {"claimed_at": aware.replace(tzinfo=None)},
        )

        for changed in invalid_values:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                CronExecution(**{**valid, **changed})

    def test_loader_rejects_nonzero_exit_without_exposing_process_data(self):
        def run(command, **kwargs):
            return CompletedProcess(
                command,
                1,
                "TOKEN=stdout-must-never-escape",
                "PASSWORD=stderr-must-never-escape",
            )

        with self.assertRaises(ExecutionDiscoveryError) as raised:
            load_cron_runs(JOB_ID, run_command=run)

        self.assert_safe_discovery_error(raised.exception)

    def test_loader_rejects_timeout_without_exposing_process_data(self):
        def run(command, **kwargs):
            raise subprocess.TimeoutExpired(
                command,
                20,
                output="TOKEN=stdout-must-never-escape",
                stderr="PASSWORD=stderr-must-never-escape",
            )

        with self.assertRaises(ExecutionDiscoveryError) as raised:
            load_cron_runs(JOB_ID, run_command=run)

        self.assert_safe_discovery_error(raised.exception)

    def test_loader_rejects_unicode_decode_error_without_exposing_context(self):
        def run(command, **kwargs):
            raise UnicodeDecodeError(
                "utf-8", b"\xff", 0, 1, "decode-marker-must-never-escape"
            )

        with self.assertRaises(ExecutionDiscoveryError) as raised:
            load_cron_runs(JOB_ID, run_command=run)

        self.assert_safe_discovery_error(
            raised.exception, "decode-marker-must-never-escape"
        )

    def test_loader_rejects_oserror_and_malformed_stdout_safely(self):
        def fail_to_start(command, **kwargs):
            raise OSError("DEEPSEEK_API_KEY=must-never-escape")

        def malformed_stdout(command, **kwargs):
            return CompletedProcess(command, 0, "unexpected preface\n", "")

        for run_command in (fail_to_start, malformed_stdout):
            with self.subTest(run_command=run_command.__name__):
                with self.assertRaises(ExecutionDiscoveryError) as raised:
                    load_cron_runs(JOB_ID, run_command=run_command)
                self.assert_safe_discovery_error(raised.exception)

    def test_loader_rejects_relative_cli_without_running_it(self):
        called = False

        def run(command, **kwargs):
            nonlocal called
            called = True

        with self.assertRaises(ExecutionDiscoveryError):
            load_cron_runs(
                JOB_ID, run_command=run, hermes_cli=Path("bin/hermes")
            )

        self.assertFalse(called)

    def test_loader_rejects_non_string_job_id_without_running_command(self):
        called = False

        def run(command, **kwargs):
            nonlocal called
            called = True

        with self.assertRaises(ExecutionDiscoveryError) as raised:
            load_cron_runs(7, run_command=run)

        self.assert_safe_discovery_error(raised.exception)
        self.assertFalse(called)

    def test_loader_rejects_malformed_cli_path_without_running_command(self):
        marker = "path-marker-must-never-escape"
        called = False

        class InvalidPath:
            def __fspath__(self):
                raise TypeError(marker)

        def run(command, **kwargs):
            nonlocal called
            called = True

        for hermes_cli, forbidden_markers in (
            (None, ()),
            (InvalidPath(), (marker,)),
        ):
            with self.subTest(hermes_cli=hermes_cli):
                with self.assertRaises(ExecutionDiscoveryError) as raised:
                    load_cron_runs(
                        JOB_ID, run_command=run, hermes_cli=hermes_cli
                    )
                self.assert_safe_discovery_error(
                    raised.exception, *forbidden_markers
                )
                self.assertFalse(called)

    def test_loader_rejects_malformed_process_results_safely(self):
        malformed_results = {
            "wrong result type": object(),
            "boolean return code": CompletedProcess(
                [], False, RUNS_OUTPUT, ""
            ),
            "float return code": CompletedProcess([], 0.0, RUNS_OUTPUT, ""),
            "non-string stdout": CompletedProcess(
                [], 0, b"stdout-marker-must-never-escape", ""
            ),
        }

        for case, result in malformed_results.items():
            with self.subTest(case=case):
                with self.assertRaises(ExecutionDiscoveryError) as raised:
                    load_cron_runs(
                        JOB_ID, run_command=lambda command, **kwargs: result
                    )
                self.assert_safe_discovery_error(
                    raised.exception, "stdout-marker-must-never-escape"
                )

    def test_loader_rejects_runner_type_error_without_exposing_context(self):
        marker = "runner-marker-must-never-escape"

        def run(command, **kwargs):
            raise TypeError(marker)

        with self.assertRaises(ExecutionDiscoveryError) as raised:
            load_cron_runs(JOB_ID, run_command=run)

        self.assert_safe_discovery_error(raised.exception, marker)

    def test_discovery_rejects_cursor_missing_from_nonempty_window(self):
        start = datetime(2026, 8, 14, tzinfo=timezone.utc)
        rows = tuple(
            cron_execution(
                f"{index:032x}",
                "claimed",
                start + timedelta(minutes=index),
            )
            for index in range(500)
        )
        state = state_with_cursor(JOB_ID, "f" * 32)

        with self.assertRaises(ExecutionDiscoveryError):
            discover_execution_events(
                state,
                "review_memory",
                JOB_ID,
                rows,
                daily_archive_job_id=ARCHIVE_JOB_ID,
            )

        self.assertEqual(state.execution_cursors[JOB_ID], "f" * 32)
        self.assertEqual(state.seen_execution_ids[JOB_ID], ["f" * 32])

    def test_discovery_normalizes_and_processes_new_rows_oldest_first(self):
        base = datetime(2026, 8, 14, tzinfo=timezone.utc)
        cursor_id = "c" * 32
        newest_id = "d" * 32
        first_new_id = "b" * 32
        older_id = "a" * 32
        rows = (
            cron_execution(cursor_id, "completed", base + timedelta(hours=10)),
            cron_execution(newest_id, "failed", base + timedelta(hours=12)),
            cron_execution(older_id, "failed", base + timedelta(hours=9)),
            cron_execution(first_new_id, "failed", base + timedelta(hours=11)),
        )

        updated, events, completed = discover_execution_events(
            state_with_cursor(JOB_ID, cursor_id),
            "review_memory",
            JOB_ID,
            rows,
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )

        self.assertEqual(
            [event.execution_id for event in events],
            [first_new_id, newest_id],
        )
        self.assertEqual(updated.execution_cursors[JOB_ID], newest_id)
        self.assertEqual(
            updated.seen_execution_ids[JOB_ID],
            [newest_id, first_new_id, cursor_id, older_id],
        )
        self.assertEqual(completed, ())

    def test_running_transition_to_failed_emits_once(self):
        claimed_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
        execution_id = "a" * 32
        running = cron_execution(execution_id, "running", claimed_at)

        running_state, events, completed = discover_execution_events(
            state_with_cursor(JOB_ID, None),
            "review_memory",
            JOB_ID,
            (running,),
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )

        self.assertEqual(events, ())
        self.assertEqual(completed, ())
        self.assertIsNone(running_state.execution_cursors[JOB_ID])
        self.assertEqual(
            running_state.seen_execution_ids[JOB_ID], [execution_id]
        )

        failed = cron_execution(execution_id, "failed", claimed_at)
        failed_state, events, completed = discover_execution_events(
            running_state,
            "review_memory",
            JOB_ID,
            (failed,),
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )

        self.assertEqual(
            [event.event_id for event in events],
            [f"failure:{JOB_ID}:{execution_id}"],
        )
        self.assertEqual(completed, ())
        self.assertEqual(failed_state.execution_cursors[JOB_ID], execution_id)

        stable_state, events, completed = discover_execution_events(
            failed_state,
            "review_memory",
            JOB_ID,
            (failed,),
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )
        self.assertEqual(events, ())
        self.assertEqual(completed, ())
        self.assertEqual(stable_state, failed_state)

    def test_running_archive_transition_to_completed_is_returned_once(self):
        claimed_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
        execution_id = "a" * 32
        running = cron_execution(
            execution_id,
            "running",
            claimed_at,
            job_id=ARCHIVE_JOB_ID,
        )

        running_state, events, completed = discover_execution_events(
            state_with_cursor(ARCHIVE_JOB_ID, None),
            "daily_archive",
            ARCHIVE_JOB_ID,
            (running,),
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )

        self.assertEqual(events, ())
        self.assertEqual(completed, ())
        self.assertIsNone(running_state.execution_cursors[ARCHIVE_JOB_ID])

        complete = cron_execution(
            execution_id,
            "completed",
            claimed_at,
            job_id=ARCHIVE_JOB_ID,
        )
        complete_state, events, completed = discover_execution_events(
            running_state,
            "daily_archive",
            ARCHIVE_JOB_ID,
            (complete,),
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )

        self.assertEqual(events, ())
        self.assertEqual(completed, (complete,))
        self.assertEqual(
            complete_state.execution_cursors[ARCHIVE_JOB_ID], execution_id
        )

        stable_state, events, completed = discover_execution_events(
            complete_state,
            "daily_archive",
            ARCHIVE_JOB_ID,
            (complete,),
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )
        self.assertEqual(events, ())
        self.assertEqual(completed, ())
        self.assertEqual(stable_state, complete_state)

    def test_newer_failure_is_not_delayed_by_older_running_execution(self):
        base = datetime(2026, 8, 14, tzinfo=timezone.utc)
        cursor_id = "a" * 32
        running_id = "b" * 32
        failed_id = "c" * 32
        rows = (
            cron_execution(failed_id, "failed", base + timedelta(hours=2)),
            cron_execution(running_id, "running", base + timedelta(hours=1)),
            cron_execution(cursor_id, "completed", base),
        )

        updated, events, completed = discover_execution_events(
            state_with_cursor(JOB_ID, cursor_id),
            "review_memory",
            JOB_ID,
            rows,
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )

        self.assertEqual(
            [event.execution_id for event in events], [failed_id]
        )
        self.assertEqual(completed, ())
        self.assertEqual(updated.execution_cursors[JOB_ID], cursor_id)
        self.assertEqual(
            updated.seen_execution_ids[JOB_ID],
            [failed_id, running_id, cursor_id],
        )

        event = events[0]
        persisted = updated.model_copy(
            update={
                "deliveries": {
                    event.event_id: DeliveryRecord(
                        event=event, next_attempt_at=event.created_at
                    )
                }
            }
        )
        _, repeated_events, _ = discover_execution_events(
            persisted,
            "review_memory",
            JOB_ID,
            rows,
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )
        self.assertEqual(repeated_events, ())

    def test_unknown_is_seen_without_creating_an_alert(self):
        claimed_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
        execution_id = "a" * 32

        updated, events, completed = discover_execution_events(
            state_with_cursor(JOB_ID, None),
            "review_memory",
            JOB_ID,
            (cron_execution(execution_id, "unknown", claimed_at),),
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )

        self.assertEqual(events, ())
        self.assertEqual(completed, ())
        self.assertEqual(updated.execution_cursors[JOB_ID], execution_id)
        self.assertEqual(updated.seen_execution_ids[JOB_ID], [execution_id])

    def test_failed_row_creates_stable_event_with_only_safe_metadata(self):
        claimed_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
        execution_id = "a" * 32
        secret = "DEEPSEEK_API_KEY=must-never-escape"

        _, events, _ = discover_execution_events(
            state_with_cursor(JOB_ID, None),
            "review_memory",
            JOB_ID,
            (
                cron_execution(
                    execution_id,
                    "failed",
                    claimed_at,
                    source=secret,
                ),
            ),
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.event_id, f"failure:{JOB_ID}:{execution_id}")
        self.assertEqual(event.kind, "execution_failure")
        self.assertEqual(event.created_at, claimed_at)
        self.assertEqual(event.job_name, "review_memory")
        self.assertEqual(event.job_id, JOB_ID)
        self.assertEqual(event.execution_id, execution_id)
        self.assertIsNone(event.trade_date)
        self.assertIsNone(event.report_sha256)
        self.assertIsNone(event.batch_state)
        self.assertNotIn(secret, repr(event))

    def test_completed_archive_rows_are_returned_for_report_discovery(self):
        base = datetime(2026, 8, 14, tzinfo=timezone.utc)
        cursor_id = "a" * 32
        completed_id = "b" * 32
        rows = (
            cron_execution(
                completed_id,
                "completed",
                base + timedelta(hours=1),
                job_id=ARCHIVE_JOB_ID,
            ),
            cron_execution(
                cursor_id, "completed", base, job_id=ARCHIVE_JOB_ID
            ),
        )

        updated, events, completed = discover_execution_events(
            state_with_cursor(ARCHIVE_JOB_ID, cursor_id),
            "daily_archive",
            ARCHIVE_JOB_ID,
            rows,
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )

        self.assertEqual(events, ())
        self.assertEqual(completed, (rows[0],))
        self.assertEqual(updated.execution_cursors[ARCHIVE_JOB_ID], completed_id)

    def test_completed_non_archive_rows_only_advance_state(self):
        claimed_at = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
        execution_id = "a" * 32

        updated, events, completed = discover_execution_events(
            state_with_cursor(JOB_ID, None),
            "review_memory",
            JOB_ID,
            (cron_execution(execution_id, "completed", claimed_at),),
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )

        self.assertEqual(events, ())
        self.assertEqual(completed, ())
        self.assertEqual(updated.execution_cursors[JOB_ID], execution_id)

    def test_seen_history_is_replaced_by_observed_500_row_window(self):
        start = datetime(2026, 8, 14, tzinfo=timezone.utc)
        rows = tuple(
            cron_execution(
                f"{index:032x}",
                "claimed",
                start + timedelta(minutes=index),
            )
            for index in range(500)
        )
        oldest_id = rows[0].execution_id
        state = state_with_cursor(JOB_ID, oldest_id).model_copy(
            update={
                "seen_execution_ids": {
                    JOB_ID: [oldest_id] + [f"old-{index}" for index in range(600)]
                }
            }
        )

        updated, events, completed = discover_execution_events(
            state,
            "review_memory",
            JOB_ID,
            rows,
            daily_archive_job_id=ARCHIVE_JOB_ID,
        )

        self.assertEqual(events, ())
        self.assertEqual(completed, ())
        self.assertEqual(len(updated.seen_execution_ids[JOB_ID]), 500)
        self.assertEqual(
            updated.seen_execution_ids[JOB_ID],
            [row.execution_id for row in reversed(rows)],
        )

    def test_discovery_rejects_rows_for_another_job_without_state_change(self):
        state = state_with_cursor(JOB_ID, None)
        row = cron_execution(
            "a" * 32,
            "failed",
            datetime(2026, 8, 14, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )

        with self.assertRaises(ExecutionDiscoveryError):
            discover_execution_events(
                state,
                "review_memory",
                JOB_ID,
                (row,),
                daily_archive_job_id=ARCHIVE_JOB_ID,
            )

        self.assertEqual(state.execution_cursors[JOB_ID], None)
        self.assertEqual(state.seen_execution_ids[JOB_ID], [])

    def test_discovery_rejects_non_string_job_ids_without_state_change(self):
        state = state_with_cursor(JOB_ID, None)
        invalid_boundaries = (
            {"job_id": 7, "daily_archive_job_id": ARCHIVE_JOB_ID},
            {"job_id": JOB_ID, "daily_archive_job_id": 7},
        )

        for arguments in invalid_boundaries:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ExecutionDiscoveryError) as raised:
                    discover_execution_events(
                        state,
                        "review_memory",
                        arguments["job_id"],
                        (),
                        daily_archive_job_id=arguments[
                            "daily_archive_job_id"
                        ],
                    )
                self.assert_safe_discovery_error(raised.exception)
                self.assertEqual(state, state_with_cursor(JOB_ID, None))

    def test_load_verified_archives_builds_ordered_snapshots_and_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_date = date(2026, 8, 14)
            middle_date = date(2026, 8, 15)
            last_date = date(2026, 8, 16)
            first_bytes = b"# First\n"
            middle_bytes = b"# Middle\n"
            last_bytes = b"# Last\n"
            persist_report_batch(
                root, report_batch(last_date, last_bytes, state="degraded"), last_bytes
            )
            persist_report_batch(
                root, report_batch(first_date, first_bytes), first_bytes
            )
            persist_report_batch(
                root,
                report_batch(
                    middle_date,
                    middle_bytes,
                    items=[
                        DailyReportArchiveItem(
                            symbol="BTC",
                            status="unreadable",
                            processed_signal=None,
                            final_trade_decision=None,
                            error_code="SOURCE_UNREADABLE",
                        ),
                        DailyReportArchiveItem(
                            symbol="ETH",
                            status="completed",
                            processed_signal="hold",
                            final_trade_decision="hold",
                            error_code=None,
                        ),
                        DailyReportArchiveItem(
                            symbol="SOL",
                            status="failed",
                            processed_signal=None,
                            final_trade_decision=None,
                            error_code="ANALYSIS_FAILED",
                        ),
                    ],
                ),
                middle_bytes,
            )

            archives = load_verified_archives(root)

        self.assertEqual(
            [archive.trade_date for archive in archives],
            [first_date, middle_date, last_date],
        )
        self.assertIsNone(archives[0].previous)
        self.assertEqual(archives[1].previous.trade_date, first_date)
        self.assertEqual(archives[2].previous.trade_date, middle_date)
        self.assertEqual(archives[2].state, "degraded")
        self.assertEqual(
            [item.status for item in archives[1].items],
            ["unreadable", "completed", "failed"],
        )
        self.assertEqual(
            archives[0].event_id,
            f"report:2026-08-14:{hashlib.sha256(first_bytes).hexdigest()}",
        )
        card = archives[1].to_card_data(archives[1].event_id)
        self.assertIsInstance(card, ReportCardData)
        self.assertEqual([item.symbol for item in card.items], ["BTC", "ETH", "SOL"])
        self.assertEqual(card.report_path, root / "hermes" / "reports" / "2026-08-15.md")

    def test_load_verified_archives_rejects_tampered_report_with_safe_error(self):
        marker = "report-content-must-never-escape"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_bytes = marker.encode("ascii")
            batch = report_batch(date(2026, 8, 14), report_bytes)
            persist_report_batch(root, batch, b"tampered")

            with self.assertRaises(ReportDiscoveryError) as raised:
                load_verified_archives(root)

        self.assert_safe_report_error(raised.exception, marker)

    def test_load_verified_archives_rejects_invalid_canonical_sources_fail_closed(self):
        cases = (
            "batch date mismatch",
            "archive date mismatch",
            "naive archive timestamp",
            "malformed JSON",
            "schema invalid",
            "missing report",
            "nonregular batch",
            "symlink batch",
            "nonregular report",
            "symlink report",
            "non-UTF batch",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                report_bytes = b"# Valid\n"
                batch = report_batch(date(2026, 8, 14), report_bytes)
                batch_path = persist_report_batch(root, batch, report_bytes)
                report_path = root / "hermes" / "reports" / "2026-08-14.md"
                if case == "batch date mismatch":
                    batch_path.rename(batch_path.with_name("2026-08-15.json"))
                elif case == "archive date mismatch":
                    payload = batch.model_dump(mode="json")
                    payload["archive"]["filename"] = "2026-08-15.md"
                    batch_path.write_text(json.dumps(payload), encoding="ascii")
                elif case == "naive archive timestamp":
                    payload = batch.model_dump(mode="json")
                    payload["archive"]["archived_at"] = "2026-08-14T00:00:00"
                    batch_path.write_text(json.dumps(payload), encoding="ascii")
                elif case == "malformed JSON":
                    batch_path.write_text("{unsafe-json", encoding="ascii")
                elif case == "schema invalid":
                    batch_path.write_text("{}", encoding="ascii")
                elif case == "missing report":
                    report_path.unlink()
                elif case == "nonregular batch":
                    batch_path.unlink()
                    batch_path.mkdir()
                elif case == "symlink batch":
                    batch_path.unlink()
                    batch_path.symlink_to(root / "elsewhere.json")
                elif case == "nonregular report":
                    report_path.unlink()
                    report_path.mkdir()
                elif case == "symlink report":
                    report_path.unlink()
                    report_path.symlink_to(root / "elsewhere.md")
                elif case == "non-UTF batch":
                    batch_path.write_bytes(b"\xff")

                with self.assertRaises(ReportDiscoveryError) as raised:
                    load_verified_archives(root)
                self.assert_safe_report_error(raised.exception, "unsafe-json")

    def test_load_verified_archives_validates_every_json_before_returning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_bytes = b"# Valid\n"
            persist_report_batch(
                root, report_batch(date(2026, 8, 14), report_bytes), report_bytes
            )
            invalid_path = root / "hermes" / "report_batches" / "unrelated.json"
            invalid_path.write_text("not JSON", encoding="ascii")
            (root / "hermes" / "report_batches" / "README.txt").write_text(
                "ignored", encoding="ascii"
            )

            with self.assertRaises(ReportDiscoveryError):
                load_verified_archives(root)

    def test_load_verified_archives_rejects_invalid_source_directories(self):
        marker = "directory-boundary-marker-must-never-escape"
        cases = (
            "results root symlink",
            "results root regular file",
            "hermes regular file",
            "hermes symlink",
            "batches regular file",
            "batches symlink",
            "reports regular file",
            "reports symlink",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "configured"
                external = Path(directory) / marker
                report_bytes = b"# Valid\n"
                if case == "results root symlink":
                    persist_report_batch(
                        external,
                        report_batch(date(2026, 8, 14), report_bytes),
                        report_bytes,
                    )
                    root.symlink_to(external, target_is_directory=True)
                elif case == "results root regular file":
                    root.write_text("not a directory", encoding="ascii")
                elif case == "hermes regular file":
                    root.mkdir()
                    (root / "hermes").write_text("not a directory", encoding="ascii")
                elif case == "hermes symlink":
                    root.mkdir()
                    target = root / "hermes-target"
                    target.mkdir()
                    (root / "hermes").symlink_to(
                        target, target_is_directory=True
                    )
                else:
                    persist_report_batch(
                        root,
                        report_batch(date(2026, 8, 14), report_bytes),
                        report_bytes,
                    )
                    boundary = root / "hermes" / (
                        "report_batches"
                        if case.startswith("batches")
                        else "reports"
                    )
                    if case.endswith("regular file"):
                        for child in boundary.iterdir():
                            child.unlink()
                        boundary.rmdir()
                        boundary.write_text("not a directory", encoding="ascii")
                    else:
                        target = root / f"{boundary.name}-target"
                        boundary.rename(target)
                        boundary.symlink_to(target, target_is_directory=True)

                with self.assertRaises(ReportDiscoveryError) as raised:
                    load_verified_archives(root)
                self.assert_safe_report_error(raised.exception, marker)

    def test_load_verified_archives_rejects_unsafe_pathlike(self):
        marker = "unsafe-path-marker"

        class UnsafePath:
            def __fspath__(self):
                raise RuntimeError(marker)

        with self.assertRaises(ReportDiscoveryError) as raised:
            load_verified_archives(UnsafePath())

        self.assert_safe_report_error(raised.exception, marker)

    def test_public_report_discovery_rejects_keyerror_pathlike_safely(self):
        marker = "key-marker-must-never-escape"

        class UnsafePath:
            def __fspath__(self):
                raise KeyError(marker)

        execution = cron_execution(
            "8" * 32,
            "completed",
            datetime(2026, 8, 14, 5, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )
        for discover in (
            lambda: load_verified_archives(UnsafePath()),
            lambda: discover_missing_archive_events(
                UnsafePath(),
                (execution,),
                (),
                empty_notification_state(),
                job_name="daily_archive",
                daily_archive_job_id=ARCHIVE_JOB_ID,
            ),
        ):
            with self.subTest(discover=discover), self.assertRaises(
                ReportDiscoveryError
            ) as raised:
                discover()
            self.assert_safe_report_error(raised.exception, marker)
        execution = cron_execution(
            "7" * 32,
            "completed",
            datetime(2026, 8, 14, 5, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )
        with self.assertRaises(ReportDiscoveryError) as raised:
            discover_missing_archive_events(
                UnsafePath(),
                (execution,),
                (),
                empty_notification_state(),
                job_name="daily_archive",
                daily_archive_job_id=ARCHIVE_JOB_ID,
            )

        self.assert_safe_report_error(raised.exception, marker)

    def test_discover_report_events_is_oldest_first_and_suppresses_seen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            early_bytes = b"# Early\n"
            later_bytes = b"# Later\n"
            persist_report_batch(
                root, report_batch(date(2026, 8, 14), early_bytes), early_bytes
            )
            persist_report_batch(
                root, report_batch(date(2026, 8, 15), later_bytes), later_bytes
            )
            archives = load_verified_archives(root)

        events = discover_report_events(empty_notification_state(), archives)

        self.assertEqual([event.event_id for event in events], [
            archive.event_id for archive in archives
        ])
        self.assertEqual(events[0].trade_date, date(2026, 8, 14))
        self.assertEqual(events[0].report_sha256, archives[0].report_sha256)
        self.assertEqual(events[0].batch_state, "ready")
        self.assertEqual(events[0].created_at, archives[0].archived_at)
        seen = empty_notification_state().model_copy(
            update={"seen_report_event_ids": [archives[0].event_id]}
        )
        self.assertEqual(discover_report_events(seen, archives), (events[1],))
        delivered = empty_notification_state().model_copy(
            update={
                "deliveries": {
                    events[1].event_id: DeliveryRecord(
                        event=events[1], next_attempt_at=events[1].created_at
                    )
                }
            }
        )
        self.assertEqual(discover_report_events(delivered, archives), (events[0],))

    def test_completed_unarchived_batch_creates_one_missing_archive_event(self):
        trade_date = date(2026, 8, 14)
        execution = cron_execution(
            "e" * 32,
            "completed",
            datetime(2026, 8, 14, 5, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persist_report_batch(root, report_batch(trade_date, b"", archive=False))
            archives = load_verified_archives(root)
            events = discover_missing_archive_events(
                root,
                (execution,),
                archives,
                empty_notification_state(),
                job_name="daily_archive",
                daily_archive_job_id=ARCHIVE_JOB_ID,
            )

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.event_id, f"missing_archive:{ARCHIVE_JOB_ID}:{'e' * 32}")
        self.assertEqual(event.kind, "missing_archive")
        self.assertEqual(event.trade_date, trade_date)
        self.assertEqual(event.batch_state, "unarchived")
        self.assertEqual(event.job_name, "daily_archive")
        self.assertEqual(event.job_id, ARCHIVE_JOB_ID)
        self.assertEqual(event.execution_id, "e" * 32)

    def test_naive_batch_created_at_does_not_block_missing_archive_discovery(self):
        trade_date = date(2026, 8, 14)
        batch = report_batch(trade_date, b"", archive=False).model_copy(
            update={"created_at": datetime(2026, 8, 14)}
        )
        execution = cron_execution(
            "1" * 32,
            "completed",
            datetime(2026, 8, 14, 5, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persist_report_batch(root, batch)
            archives = load_verified_archives(root)
            events = discover_missing_archive_events(
                root,
                (execution,),
                archives,
                empty_notification_state(),
                job_name="daily_archive",
                daily_archive_job_id=ARCHIVE_JOB_ID,
            )

        self.assertEqual(archives, ())
        self.assertEqual([event.trade_date for event in events], [trade_date])

    def test_archived_batch_suppresses_missing_archive_for_shanghai_date(self):
        trade_date = date(2026, 8, 14)
        report_bytes = b"# Archived\n"
        execution = cron_execution(
            "2" * 32,
            "completed",
            datetime(2026, 8, 14, 5, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persist_report_batch(
                root, report_batch(trade_date, report_bytes), report_bytes
            )
            archives = load_verified_archives(root)
            events = discover_missing_archive_events(
                root,
                (execution,),
                archives,
                empty_notification_state(),
                job_name="daily_archive",
                daily_archive_job_id=ARCHIVE_JOB_ID,
            )

        self.assertEqual(events, ())

    def test_missing_archive_uses_shanghai_date_across_utc_rollover(self):
        trade_date = date(2026, 8, 14)
        execution = cron_execution(
            "3" * 32,
            "completed",
            datetime(2026, 8, 13, 16, 30, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persist_report_batch(root, report_batch(trade_date, b"", archive=False))
            events = discover_missing_archive_events(
                root,
                (execution,),
                (),
                empty_notification_state(),
                job_name="daily_archive",
                daily_archive_job_id=ARCHIVE_JOB_ID,
            )

        self.assertEqual([event.trade_date for event in events], [trade_date])

    def test_missing_archive_discovery_validates_other_canonical_dates_first(self):
        marker = "other-date-marker-must-never-escape"
        trade_date = date(2026, 8, 14)
        execution = cron_execution(
            "4" * 32,
            "completed",
            datetime(2026, 8, 14, 5, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persist_report_batch(root, report_batch(trade_date, b"", archive=False))
            (root / "hermes" / "report_batches" / "2026-08-15.json").write_text(
                marker, encoding="ascii"
            )

            with self.assertRaises(ReportDiscoveryError) as raised:
                discover_missing_archive_events(
                    root,
                    (execution,),
                    (),
                    empty_notification_state(),
                    job_name="daily_archive",
                    daily_archive_job_id=ARCHIVE_JOB_ID,
                )

        self.assert_safe_report_error(raised.exception, marker)

    def test_missing_archive_discovery_uses_one_immutable_inventory_snapshot(self):
        trade_date = date(2026, 8, 14)
        execution = cron_execution(
            "5" * 32,
            "completed",
            datetime(2026, 8, 14, 5, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_path = persist_report_batch(
                root, report_batch(trade_date, b"", archive=False)
            )
            original_loader = notifier._load_report_inventory

            def load_then_remove(source_root):
                inventory = original_loader(source_root)
                batch_path.unlink()
                return inventory

            with patch.object(
                notifier,
                "_load_report_inventory",
                side_effect=load_then_remove,
            ) as loader:
                events = discover_missing_archive_events(
                    root,
                    (execution,),
                    (),
                    empty_notification_state(),
                    job_name="daily_archive",
                    daily_archive_job_id=ARCHIVE_JOB_ID,
                )

        self.assertEqual(loader.call_count, 1)
        self.assertEqual([event.trade_date for event in events], [trade_date])

    def test_missing_archive_discovery_does_not_trust_stale_archives(self):
        trade_date = date(2026, 8, 14)
        execution = cron_execution(
            "6" * 32,
            "completed",
            datetime(2026, 8, 14, 5, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "empty"
            root.mkdir()
            external = Path(directory) / "external"
            report_bytes = b"# Archived\n"
            persist_report_batch(
                external, report_batch(trade_date, report_bytes), report_bytes
            )
            stale_archives = load_verified_archives(external)
            events = discover_missing_archive_events(
                root,
                (execution,),
                stale_archives,
                empty_notification_state(),
                job_name="daily_archive",
                daily_archive_job_id=ARCHIVE_JOB_ID,
            )

        self.assertEqual(events, ())

    def test_missing_archive_discovery_is_exact_date_deduplicated_and_fail_closed(self):
        trade_date = date(2026, 8, 14)
        matching = cron_execution(
            "e" * 32,
            "completed",
            datetime(2026, 8, 14, 5, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )
        missing = cron_execution(
            "f" * 32,
            "completed",
            datetime(2026, 8, 15, 5, tzinfo=timezone.utc),
            job_id=ARCHIVE_JOB_ID,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            persist_report_batch(root, report_batch(trade_date, b"", archive=False))
            archives = load_verified_archives(root)
            state = empty_notification_state()
            events = discover_missing_archive_events(
                root,
                (matching, missing, matching),
                archives,
                state,
                job_name="daily_archive",
                daily_archive_job_id=ARCHIVE_JOB_ID,
            )
            self.assertEqual([event.execution_id for event in events], ["e" * 32])
            delivered_state = state.model_copy(
                update={
                    "deliveries": {
                        events[0].event_id: DeliveryRecord(
                            event=events[0], next_attempt_at=events[0].created_at
                        )
                    }
                }
            )
            self.assertEqual(
                discover_missing_archive_events(
                    root,
                    (matching,),
                    archives,
                    delivered_state,
                    job_name="daily_archive",
                    daily_archive_job_id=ARCHIVE_JOB_ID,
                ),
                (),
            )
            (root / "hermes" / "report_batches" / "broken.json").write_text(
                "source-marker-must-never-escape", encoding="ascii"
            )
            with self.assertRaises(ReportDiscoveryError) as raised:
                discover_missing_archive_events(
                    root,
                    (matching,),
                    archives,
                    state,
                    job_name="daily_archive",
                    daily_archive_job_id=ARCHIVE_JOB_ID,
                )
        self.assert_safe_report_error(
            raised.exception, "source-marker-must-never-escape"
        )

    def assert_safe_discovery_error(self, error, *forbidden_markers):
        self.assertEqual(str(error), "Hermes execution history unavailable")
        self.assertEqual(error.args, ("Hermes execution history unavailable",))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        rendered = repr(error)
        self.assertNotIn("must-never-escape", rendered)
        self.assertNotIn("stdout", rendered)
        self.assertNotIn("stderr", rendered)
        for marker in forbidden_markers:
            self.assertNotIn(marker, rendered)

    def assert_safe_report_error(self, error, *forbidden_markers):
        self.assertEqual(str(error), "daily report archive unavailable")
        self.assertEqual(error.args, ("daily report archive unavailable",))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        rendered = repr(error)
        for marker in forbidden_markers:
            self.assertNotIn(marker, rendered)


class HermesFeishuOrchestrationTests(unittest.TestCase):
    NOW = datetime(2026, 8, 17, 4, 0, tzinfo=timezone.utc)

    def failure_event(self, *, execution_id="f" * 32):
        job_id = NOTIFIER_JOBS["review_memory"]
        return NotificationEvent(
            event_id=f"failure:{job_id}:{execution_id}",
            kind="execution_failure",
            created_at=self.NOW,
            job_name="review_memory",
            job_id=job_id,
            execution_id=execution_id,
        )

    def test_initialize_persists_baseline_without_network(self):
        histories = execution_histories(self.NOW)
        archive_calls = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_bytes = b"# Baseline\n"
            persist_report_batch(
                root,
                report_batch(date(2026, 8, 16), report_bytes),
                report_bytes,
            )
            inventory = notifier._load_report_inventory(root)
            store = NotificationStateStore(root / "notifier-state")

            state, payload = notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda job_id: histories[job_id],
                lambda: archive_calls.append(True) or inventory,
                self.NOW,
            )

            persisted = store.load()
        self.assertEqual(state, persisted)
        self.assertEqual(state.deliveries, {})
        self.assertEqual(
            state.seen_report_event_ids, [inventory.archives[0].event_id]
        )
        self.assertEqual(set(state.seen_execution_ids), set(NOTIFIER_JOBS.values()))
        self.assertEqual(payload, {
            "ok": True,
            "mode": "initialize",
            "already_initialized": False,
            "execution_count": 4,
            "report_count": 1,
        })
        self.assertEqual(len(archive_calls), 1)

    def test_repeated_initialize_is_byte_stable_and_does_not_reload_sources(self):
        histories = execution_histories(self.NOW)
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda job_id: histories[job_id],
                lambda: (),
                self.NOW,
            )
            before = store.path.read_bytes()

            state, payload = notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda _job_id: self.fail("execution source was reloaded"),
                lambda: self.fail("archive source was reloaded"),
                self.NOW + timedelta(minutes=1),
            )

            after = store.path.read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(state.deliveries, {})
        self.assertEqual(payload["already_initialized"], True)
        self.assertEqual(payload["execution_count"], 4)
        self.assertEqual(payload["report_count"], 0)

    def test_initialize_source_failure_creates_no_state(self):
        marker = "source-secret-must-never-escape"
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")

            with self.assertRaises(ExecutionDiscoveryError) as raised:
                notifier.initialize_notifier(
                    store,
                    notifier_config(),
                    lambda _job_id: (_ for _ in ()).throw(RuntimeError(marker)),
                    lambda: (),
                    self.NOW,
                )

            self.assertFalse(store.path.exists())
        self.assertNotIn(marker, repr(raised.exception))

    def test_initialize_keeps_leading_running_row_observable(self):
        histories = execution_histories(self.NOW)
        job_id = NOTIFIER_JOBS["review_memory"]
        running_id = "a" * 32
        baseline_id = "b" * 32
        histories[job_id] = (
            cron_execution(
                running_id,
                "running",
                self.NOW,
                job_id=job_id,
            ),
            cron_execution(
                baseline_id,
                "completed",
                self.NOW - timedelta(minutes=1),
                job_id=job_id,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            state, _payload = notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda source_job_id: histories[source_job_id],
                lambda: (),
                self.NOW,
            )
            self.assertEqual(state.execution_cursors[job_id], baseline_id)
            current = dict(histories)
            current[job_id] = (
                dataclasses.replace(histories[job_id][0], status="failed"),
                histories[job_id][1],
            )

            class RecordingClient:
                def __init__(inner_self):
                    inner_self.calls = 0

                def send(inner_self, _payload):
                    inner_self.calls += 1

            client = RecordingClient()
            code, payload = notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda source_job_id: current[source_job_id],
                lambda: (),
                self.NOW + timedelta(minutes=1),
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["discovered"], 1)
        self.assertEqual(client.calls, 1)

    def test_initialize_rejects_interleaved_nonterminal_baseline(self):
        histories = execution_histories(self.NOW)
        job_id = NOTIFIER_JOBS["review_memory"]
        histories[job_id] = (
            cron_execution(
                "c" * 32,
                "completed",
                self.NOW,
                job_id=job_id,
            ),
            cron_execution(
                "d" * 32,
                "running",
                self.NOW - timedelta(minutes=1),
                job_id=job_id,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            with self.assertRaises(ExecutionDiscoveryError):
                notifier.initialize_notifier(
                    store,
                    notifier_config(),
                    lambda source_job_id: histories[source_job_id],
                    lambda: (),
                    self.NOW,
                )
            self.assertFalse(store.path.exists())

    def test_run_source_failure_preserves_state_bytes_and_skips_network(self):
        histories = execution_histories(self.NOW)
        marker = "loader-secret-must-never-escape"
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda job_id: histories[job_id],
                lambda: (),
                self.NOW,
            )
            before = store.path.read_bytes()
            calls = []

            class NoSendClient:
                def send(self, _payload):
                    calls.append(True)

            code, payload = notifier.run_notifier_once(
                store,
                notifier_config(),
                NoSendClient(),
                lambda _job_id: (_ for _ in ()).throw(RuntimeError(marker)),
                lambda: self.fail("report source must not run after failure"),
                self.NOW + timedelta(minutes=1),
            )
            after = store.path.read_bytes()

        self.assertEqual(code, 1)
        self.assertEqual(payload["result"], "discovery_error")
        self.assertNotIn(marker, repr(payload))
        self.assertEqual(before, after)
        self.assertEqual(calls, [])

    def test_run_uses_exactly_one_report_inventory(self):
        histories = execution_histories(self.NOW)
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda job_id: histories[job_id],
                lambda: (),
                self.NOW,
            )
            archive_calls = []
            code, _payload = notifier.run_notifier_once(
                store,
                notifier_config(),
                type("NoSendClient", (), {"send": lambda *_args: None})(),
                lambda job_id: histories[job_id],
                lambda: archive_calls.append(True) or (),
                self.NOW + timedelta(minutes=1),
            )

        self.assertEqual(code, 0)
        self.assertEqual(archive_calls, [True])

    def test_uninitialized_and_locked_runs_are_safe_noops(self):
        source_calls = []

        class NoSendClient:
            def send(self, _payload):
                source_calls.append("send")

        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            code, payload = notifier.run_notifier_once(
                store,
                notifier_config(),
                NoSendClient(),
                lambda _job_id: source_calls.append("execution"),
                lambda: source_calls.append("archive"),
                self.NOW,
            )
            self.assertEqual(code, 1)
            self.assertEqual(payload["result"], "uninitialized")
            self.assertEqual(source_calls, [])

            histories = execution_histories(self.NOW)
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda job_id: histories[job_id],
                lambda: (),
                self.NOW,
            )
            with store.lock():
                code, payload = notifier.run_notifier_once(
                    store,
                    notifier_config(),
                    NoSendClient(),
                    lambda _job_id: source_calls.append("execution"),
                    lambda: source_calls.append("archive"),
                    self.NOW,
                )

        self.assertEqual(code, 0)
        self.assertEqual(payload["result"], "already_running")
        self.assertEqual(source_calls, [])

    def test_retry_attempts_one_through_six_use_bounded_schedule(self):
        state = notifier.add_pending_events(
            empty_notification_state(), (self.failure_event(),), self.NOW
        )
        expected_minutes = [5, 10, 20, 40, 60, 60]
        actual_minutes = []
        for expected in expected_minutes:
            state = notifier.begin_attempt(state, self.failure_event().event_id, self.NOW)
            record = state.deliveries[self.failure_event().event_id]
            actual_minutes.append(
                int((record.next_attempt_at - self.NOW).total_seconds() / 60)
            )
            self.assertEqual(record.attempt_count, len(actual_minutes))
            self.assertEqual(actual_minutes[-1], expected)

        self.assertEqual(actual_minutes, expected_minutes)

    def test_retry_after_extends_never_shortens_and_is_capped(self):
        event = self.failure_event()
        pending = notifier.add_pending_events(
            empty_notification_state(), (event,), self.NOW
        )
        attempted = notifier.begin_attempt(pending, event.event_id, self.NOW)
        short = notifier.record_delivery_failure(
            attempted,
            event.event_id,
            self.NOW,
            FeishuDeliveryError("rate_limited", 10),
        )
        extended = notifier.record_delivery_failure(
            attempted,
            event.event_id,
            self.NOW,
            FeishuDeliveryError("rate_limited", 3600),
        )
        capped = notifier.record_delivery_failure(
            attempted,
            event.event_id,
            self.NOW,
            FeishuDeliveryError("rate_limited", 86400),
        )
        invalid = notifier.record_delivery_failure(
            attempted,
            event.event_id,
            self.NOW,
            FeishuDeliveryError("rate_limited", True),
        )

        baseline = self.NOW + timedelta(minutes=5)
        self.assertEqual(short.deliveries[event.event_id].next_attempt_at, baseline)
        self.assertEqual(
            extended.deliveries[event.event_id].next_attempt_at,
            self.NOW + timedelta(hours=1),
        )
        self.assertEqual(
            capped.deliveries[event.event_id].next_attempt_at,
            self.NOW + timedelta(hours=24),
        )
        self.assertEqual(invalid.deliveries[event.event_id].next_attempt_at, baseline)

    def test_not_yet_due_and_delivered_records_are_skipped(self):
        event = self.failure_event()
        state = notifier.add_pending_events(
            empty_notification_state(), (event,), self.NOW
        )
        state = notifier.begin_attempt(state, event.event_id, self.NOW)
        self.assertEqual(
            notifier.due_event_ids(state, self.NOW + timedelta(minutes=4)), ()
        )
        self.assertEqual(
            notifier.due_event_ids(state, self.NOW + timedelta(minutes=5)),
            (event.event_id,),
        )
        delivered = notifier.record_delivery_success(
            state, event.event_id, self.NOW
        )
        self.assertEqual(
            notifier.due_event_ids(delivered, self.NOW + timedelta(days=1)), ()
        )

    def test_save_failure_before_send_makes_zero_network_calls(self):
        histories = execution_histories(self.NOW)
        job_id = NOTIFIER_JOBS["review_memory"]
        with tempfile.TemporaryDirectory() as directory:
            real_store = NotificationStateStore(Path(directory) / "state")
            notifier.initialize_notifier(
                real_store,
                notifier_config(),
                lambda source_job_id: histories[source_job_id],
                lambda: (),
                self.NOW,
            )
            current = dict(histories)
            current[job_id] = (
                cron_execution(
                    "e" * 32,
                    "failed",
                    self.NOW + timedelta(minutes=1),
                    job_id=job_id,
                ),
                *histories[job_id],
            )
            before = real_store.path.read_bytes()

            class FailingStore:
                def lock(inner_self):
                    return real_store.lock()

                def load_optional(inner_self):
                    return real_store.load_optional()

                def save(inner_self, _state):
                    raise NotificationStateError("safe")

            calls = []
            code, payload = notifier.run_notifier_once(
                FailingStore(),
                notifier_config(),
                type(
                    "RecordingClient",
                    (),
                    {"send": lambda _self, card: calls.append(card)},
                )(),
                lambda source_job_id: current[source_job_id],
                lambda: (),
                self.NOW + timedelta(minutes=2),
            )
            after = real_store.path.read_bytes()

        self.assertEqual(code, 1)
        self.assertEqual(payload["result"], "state_error")
        self.assertEqual(calls, [])
        self.assertEqual(before, after)

    def test_save_failures_preserve_last_successful_delivery_state(self):
        cases = (
            ("initial_pending", 1, "success", 0, 0, None),
            ("begin_attempt", 2, "success", 0, 0, None),
            ("failure_result", 3, "failure", 1, 1, None),
            ("success_result", 3, "success", 1, 1, None),
            ("final_prune", 4, "success", 1, 1, "delivered"),
        )
        histories = execution_histories(self.NOW)
        job_id = NOTIFIER_JOBS["review_memory"]
        failure_id = "7" * 32
        current = dict(histories)
        current[job_id] = (
            cron_execution(
                failure_id,
                "failed",
                self.NOW + timedelta(minutes=1),
                job_id=job_id,
            ),
            *histories[job_id],
        )
        event_id = f"failure:{job_id}:{failure_id}"

        for (
            name,
            failed_save,
            client_result,
            expected_client_calls,
            expected_attempts,
            expected_result,
        ) in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                real_store = NotificationStateStore(Path(directory) / "state")
                notifier.initialize_notifier(
                    real_store,
                    notifier_config(),
                    lambda source_job_id: histories[source_job_id],
                    lambda: (),
                    self.NOW,
                )

                class FailingSaveStore:
                    def __init__(inner_self):
                        inner_self.save_calls = 0
                        inner_self.last_successful = real_store.load()

                    def lock(inner_self):
                        return real_store.lock()

                    def load_optional(inner_self):
                        return real_store.load_optional()

                    def save(inner_self, state):
                        inner_self.save_calls += 1
                        if inner_self.save_calls == failed_save:
                            raise NotificationStateError("safe")
                        real_store.save(state)
                        inner_self.last_successful = state

                class CaseClient:
                    def __init__(inner_self):
                        inner_self.calls = 0

                    def send(inner_self, _card):
                        inner_self.calls += 1
                        if client_result == "failure":
                            raise FeishuDeliveryError("timeout")

                store = FailingSaveStore()
                client = CaseClient()
                code, payload = notifier.run_notifier_once(
                    store,
                    notifier_config(),
                    client,
                    lambda source_job_id: current[source_job_id],
                    lambda: (),
                    self.NOW + timedelta(minutes=2),
                )
                durable = real_store.load()

            self.assertEqual(code, 1)
            self.assertEqual(payload["result"], "state_error")
            self.assertEqual(client.calls, expected_client_calls)
            self.assertEqual(durable, store.last_successful)
            if event_id not in durable.deliveries:
                self.assertEqual(expected_attempts, 0)
                continue
            record = durable.deliveries[event_id]
            self.assertEqual(record.attempt_count, expected_attempts)
            self.assertEqual(record.last_result, expected_result)
            self.assertEqual(
                record.delivered_at is not None,
                expected_result == "delivered",
            )

    def test_unexpected_client_crash_retries_same_event_id(self):
        histories = execution_histories(self.NOW)
        job_id = NOTIFIER_JOBS["review_memory"]
        failure_id = "9" * 32
        current = dict(histories)
        current[job_id] = (
            cron_execution(
                failure_id,
                "failed",
                self.NOW + timedelta(minutes=1),
                job_id=job_id,
            ),
            *histories[job_id],
        )
        event_id = f"failure:{job_id}:{failure_id}"
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda source_job_id: histories[source_job_id],
                lambda: (),
                self.NOW,
            )

            class CrashingClient:
                def send(self, _payload):
                    raise KeyboardInterrupt()

            attempt_at = self.NOW + timedelta(minutes=2)
            with self.assertRaises(KeyboardInterrupt):
                notifier.run_notifier_once(
                    store,
                    notifier_config(),
                    CrashingClient(),
                    lambda source_job_id: current[source_job_id],
                    lambda: (),
                    attempt_at,
                )
            crashed = store.load()
            self.assertEqual(crashed.deliveries[event_id].attempt_count, 1)
            self.assertIsNone(crashed.deliveries[event_id].delivered_at)

            calls = []
            retry_at = attempt_at + timedelta(minutes=5)
            code, _payload = notifier.run_notifier_once(
                store,
                notifier_config(),
                type(
                    "RecordingClient",
                    (),
                    {"send": lambda _self, card: calls.append(card)},
                )(),
                lambda source_job_id: current[source_job_id],
                lambda: (),
                retry_at,
            )
            retried = store.load()

        self.assertEqual(code, 0)
        self.assertEqual(calls and len(calls), 1)
        self.assertEqual(list(retried.deliveries), [event_id])
        self.assertEqual(retried.deliveries[event_id].attempt_count, 2)
        self.assertEqual(retried.deliveries[event_id].delivered_at, retry_at)

    def test_client_notification_already_running_crash_propagates(self):
        self.assert_client_state_exception_propagates(
            NotificationAlreadyRunning("client crash")
        )

    def test_client_notification_state_error_crash_propagates(self):
        self.assert_client_state_exception_propagates(
            NotificationStateError("client crash")
        )

    def assert_client_state_exception_propagates(self, crash):
        histories = execution_histories(self.NOW)
        job_id = NOTIFIER_JOBS["review_memory"]
        failure_id = "8" * 32
        current = dict(histories)
        current[job_id] = (
            cron_execution(
                failure_id,
                "failed",
                self.NOW + timedelta(minutes=1),
                job_id=job_id,
            ),
            *histories[job_id],
        )
        event_id = f"failure:{job_id}:{failure_id}"
        attempt_at = self.NOW + timedelta(minutes=2)

        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda source_job_id: histories[source_job_id],
                lambda: (),
                self.NOW,
            )

            class CrashingClient:
                def send(self, _payload):
                    raise crash

            with self.assertRaises(type(crash)) as raised:
                notifier.run_notifier_once(
                    store,
                    notifier_config(),
                    CrashingClient(),
                    lambda source_job_id: current[source_job_id],
                    lambda: (),
                    attempt_at,
                )
            durable = store.load().deliveries[event_id]

        self.assertIs(raised.exception, crash)
        self.assertEqual(str(raised.exception), "client crash")
        self.assertEqual(durable.attempt_count, 1)
        self.assertEqual(
            durable.next_attempt_at, attempt_at + timedelta(minutes=5)
        )
        self.assertIsNone(durable.delivered_at)

    def test_lock_release_named_errors_after_normal_body_are_state_errors(self):
        histories = execution_histories(self.NOW)
        for release_error in (
            NotificationAlreadyRunning("release collision"),
            NotificationStateError("release failure"),
        ):
            with self.subTest(error=type(release_error).__name__):
                with tempfile.TemporaryDirectory() as directory:
                    real_store = NotificationStateStore(
                        Path(directory) / "state"
                    )
                    notifier.initialize_notifier(
                        real_store,
                        notifier_config(),
                        lambda job_id: histories[job_id],
                        lambda: (),
                        self.NOW,
                    )

                    class ExitFailure:
                        def __enter__(inner_self):
                            return None

                        def __exit__(
                            inner_self, exception_type, exception, traceback
                        ):
                            self.assertIsNone(exception_type)
                            raise release_error

                    class ReleaseFailingStore:
                        def lock(inner_self):
                            return ExitFailure()

                        def load_optional(inner_self):
                            return real_store.load_optional()

                        def save(inner_self, state):
                            real_store.save(state)

                    code, payload = notifier.run_notifier_once(
                        ReleaseFailingStore(),
                        notifier_config(),
                        type(
                            "NoSendClient", (), {"send": lambda *_args: None}
                        )(),
                        lambda job_id: histories[job_id],
                        lambda: (),
                        self.NOW + timedelta(minutes=1),
                    )
                    durable = real_store.load()

                self.assertEqual(code, 1)
                self.assertEqual(payload["result"], "state_error")
                self.assertEqual(durable.deliveries, {})

    def test_lock_cleanup_does_not_replace_active_client_crash(self):
        histories = execution_histories(self.NOW)
        job_id = NOTIFIER_JOBS["review_memory"]
        failure_id = "6" * 32
        current = dict(histories)
        current[job_id] = (
            cron_execution(
                failure_id,
                "failed",
                self.NOW + timedelta(minutes=1),
                job_id=job_id,
            ),
            *histories[job_id],
        )
        event_id = f"failure:{job_id}:{failure_id}"
        client_crash = RuntimeError("client crash")

        with tempfile.TemporaryDirectory() as directory:
            real_store = NotificationStateStore(Path(directory) / "state")
            notifier.initialize_notifier(
                real_store,
                notifier_config(),
                lambda source_job_id: histories[source_job_id],
                lambda: (),
                self.NOW,
            )

            class CleanupFailure:
                def __enter__(inner_self):
                    return None

                def __exit__(
                    inner_self, exception_type, exception, traceback
                ):
                    self.assertIs(exception, client_crash)
                    raise NotificationStateError("cleanup failure")

            class CleanupFailingStore:
                def lock(inner_self):
                    return CleanupFailure()

                def load_optional(inner_self):
                    return real_store.load_optional()

                def save(inner_self, state):
                    real_store.save(state)

            class CrashingClient:
                def send(inner_self, _card):
                    raise client_crash

            attempt_at = self.NOW + timedelta(minutes=2)
            with self.assertRaises(RuntimeError) as raised:
                notifier.run_notifier_once(
                    CleanupFailingStore(),
                    notifier_config(),
                    CrashingClient(),
                    lambda source_job_id: current[source_job_id],
                    lambda: (),
                    attempt_at,
                )
            durable = real_store.load().deliveries[event_id]

        self.assertIs(raised.exception, client_crash)
        self.assertEqual(durable.attempt_count, 1)
        self.assertIsNone(durable.delivered_at)

    def test_report_render_uses_nearest_previous_and_missing_exact_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for trade_date, content in (
                (date(2026, 8, 14), b"# Prior\n"),
                (date(2026, 8, 16), b"# Current\n"),
            ):
                persist_report_batch(
                    root, report_batch(trade_date, content), content
                )
            inventory = notifier._load_report_inventory(root)
            event = discover_report_events(
                empty_notification_state(), inventory.archives
            )[-1]

            card = notifier.render_persisted_event(event, inventory)

        self.assertIn("2026-08-14", repr(card))
        self.assertIn("2026-08-16", repr(card))
        with self.assertRaises(ReportDiscoveryError):
            notifier.render_persisted_event(event, ())

    def test_report_id_is_durable_before_send_and_delivered_report_does_not_resend(self):
        histories = execution_histories(self.NOW)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = NotificationStateStore(root / "state")
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda job_id: histories[job_id],
                lambda: (),
                self.NOW,
            )
            report_bytes = b"# Newly archived\n"
            persist_report_batch(
                root,
                report_batch(date(2026, 8, 17), report_bytes),
                report_bytes,
            )
            inventory = notifier._load_report_inventory(root)
            event_id = inventory.archives[0].event_id

            class InspectingClient:
                def __init__(inner_self):
                    inner_self.calls = 0

                def send(inner_self, _payload):
                    durable = store.load()
                    self.assertIn(event_id, durable.seen_report_event_ids)
                    self.assertIn(event_id, durable.deliveries)
                    self.assertEqual(
                        durable.deliveries[event_id].attempt_count, 1
                    )
                    inner_self.calls += 1

            client = InspectingClient()
            code, _payload = notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda job_id: histories[job_id],
                lambda: inventory,
                self.NOW + timedelta(minutes=1),
            )
            rerun_code, rerun_payload = notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda job_id: histories[job_id],
                lambda: inventory,
                self.NOW + timedelta(minutes=2),
            )

        self.assertEqual(code, 0)
        self.assertEqual(rerun_code, 0)
        self.assertEqual(rerun_payload["discovered"], 0)
        self.assertEqual(rerun_payload["delivered"], 0)
        self.assertEqual(client.calls, 1)

    def test_disappeared_pending_report_fails_without_sending_fabricated_card(self):
        histories = execution_histories(self.NOW)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = NotificationStateStore(root / "state")
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda job_id: histories[job_id],
                lambda: (),
                self.NOW,
            )
            report_bytes = b"# Ephemeral\n"
            persist_report_batch(
                root,
                report_batch(date(2026, 8, 17), report_bytes),
                report_bytes,
            )
            inventory = notifier._load_report_inventory(root)
            event_id = inventory.archives[0].event_id

            class CrashingClient:
                def send(self, _payload):
                    raise KeyboardInterrupt()

            first_attempt = self.NOW + timedelta(minutes=1)
            with self.assertRaises(KeyboardInterrupt):
                notifier.run_notifier_once(
                    store,
                    notifier_config(),
                    CrashingClient(),
                    lambda job_id: histories[job_id],
                    lambda: inventory,
                    first_attempt,
                )
            sends = []
            code, payload = notifier.run_notifier_once(
                store,
                notifier_config(),
                type(
                    "RecordingClient",
                    (),
                    {"send": lambda _self, card: sends.append(card)},
                )(),
                lambda job_id: histories[job_id],
                lambda: (),
                first_attempt + timedelta(minutes=5),
            )
            state = store.load()

        self.assertEqual(code, 1)
        self.assertEqual(payload["pending"], 1)
        self.assertEqual(sends, [])
        self.assertEqual(state.deliveries[event_id].attempt_count, 2)
        self.assertEqual(
            state.deliveries[event_id].last_result, "report_unavailable"
        )

    def test_old_failure_above_blocked_cursor_is_retained_until_cursor_advances(self):
        histories = execution_histories(self.NOW)
        job_id = NOTIFIER_JOBS["review_memory"]
        cursor_row = histories[job_id][0]
        blocker = cron_execution(
            "b" * 32,
            "running",
            self.NOW + timedelta(minutes=1),
            job_id=job_id,
        )
        failure = cron_execution(
            "c" * 32,
            "failed",
            self.NOW + timedelta(minutes=2),
            job_id=job_id,
        )
        blocked = dict(histories)
        blocked[job_id] = (failure, blocker, cursor_row)
        event_id = f"failure:{job_id}:{failure.execution_id}"

        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda source_job_id: histories[source_job_id],
                lambda: (),
                self.NOW,
            )
            sends = []
            client = type(
                "RecordingClient",
                (),
                {"send": lambda _self, card: sends.append(card)},
            )()
            notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda source_job_id: blocked[source_job_id],
                lambda: (),
                self.NOW + timedelta(minutes=3),
            )
            old_now = self.NOW + timedelta(days=91)
            notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda source_job_id: blocked[source_job_id],
                lambda: (),
                old_now,
            )
            retained = store.load()

            advanced = dict(blocked)
            advanced[job_id] = (
                failure,
                dataclasses.replace(blocker, status="unknown"),
                cursor_row,
            )
            notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda source_job_id: advanced[source_job_id],
                lambda: (),
                old_now + timedelta(minutes=1),
            )
            pruned = store.load()
            notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda source_job_id: advanced[source_job_id],
                lambda: (),
                old_now + timedelta(minutes=2),
            )

        self.assertIn(event_id, retained.deliveries)
        self.assertEqual(retained.execution_cursors[job_id], cursor_row.execution_id)
        self.assertNotIn(event_id, pruned.deliveries)
        self.assertEqual(pruned.execution_cursors[job_id], failure.execution_id)
        self.assertEqual(len(sends), 1)

    def test_old_missing_archive_above_blocked_cursor_is_retained_until_advance(self):
        histories = execution_histories(self.NOW)
        job_id = ARCHIVE_JOB_ID
        cursor_row = histories[job_id][0]
        blocker = cron_execution(
            "d" * 32,
            "running",
            self.NOW + timedelta(minutes=1),
            job_id=job_id,
        )
        completed = cron_execution(
            "e" * 32,
            "completed",
            self.NOW + timedelta(minutes=2),
            job_id=job_id,
        )
        blocked = dict(histories)
        blocked[job_id] = (completed, blocker, cursor_row)
        trade_date = completed.claimed_at.astimezone(SHANGHAI).date()
        inventory = notifier._ReportInventory((), frozenset({trade_date}))
        event_id = f"missing_archive:{job_id}:{completed.execution_id}"

        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda source_job_id: histories[source_job_id],
                lambda: (),
                self.NOW,
            )
            sends = []
            client = type(
                "RecordingClient",
                (),
                {"send": lambda _self, card: sends.append(card)},
            )()
            notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda source_job_id: blocked[source_job_id],
                lambda: inventory,
                self.NOW + timedelta(minutes=3),
            )
            old_now = self.NOW + timedelta(days=91)
            notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda source_job_id: blocked[source_job_id],
                lambda: inventory,
                old_now,
            )
            retained = store.load()

            advanced = dict(blocked)
            advanced[job_id] = (
                completed,
                dataclasses.replace(blocker, status="unknown"),
                cursor_row,
            )
            notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda source_job_id: advanced[source_job_id],
                lambda: inventory,
                old_now + timedelta(minutes=1),
            )
            pruned = store.load()
            notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda source_job_id: advanced[source_job_id],
                lambda: inventory,
                old_now + timedelta(minutes=2),
            )

        self.assertIn(event_id, retained.deliveries)
        self.assertEqual(retained.execution_cursors[job_id], cursor_row.execution_id)
        self.assertNotIn(event_id, pruned.deliveries)
        self.assertEqual(pruned.execution_cursors[job_id], completed.execution_id)
        self.assertEqual(len(sends), 1)

    def test_invalid_loaded_attempt_counts_fail_before_sources_or_mutation(self):
        event = self.failure_event()
        pending = notifier.add_pending_events(
            empty_notification_state(), (event,), self.NOW
        )
        for attempt_count in (-1, True, 10**100):
            with self.subTest(attempt_count=attempt_count):
                record = pending.deliveries[event.event_id].model_copy(
                    update={"attempt_count": attempt_count}
                )
                malformed = pending.model_copy(
                    update={"deliveries": {event.event_id: record}}
                )
                with tempfile.TemporaryDirectory() as directory:
                    real_store = NotificationStateStore(
                        Path(directory) / "state"
                    )
                    baseline = empty_notification_state()
                    real_store.save(baseline)
                    before = real_store.path.read_bytes()
                    activity = []

                    class MalformedStateStore:
                        def lock(inner_self):
                            return real_store.lock()

                        def load_optional(inner_self):
                            return malformed

                        def save(inner_self, state):
                            activity.append("save")
                            real_store.save(state)

                    code, payload = notifier.run_notifier_once(
                        MalformedStateStore(),
                        notifier_config(),
                        type(
                            "NoSendClient",
                            (),
                            {
                                "send": lambda _self, _card: activity.append(
                                    "send"
                                )
                            },
                        )(),
                        lambda _job_id: activity.append("execution"),
                        lambda: activity.append("archive"),
                        self.NOW,
                    )
                    after = real_store.path.read_bytes()

                self.assertEqual(code, 1)
                self.assertEqual(payload["result"], "state_error")
                self.assertEqual(activity, [])
                self.assertEqual(before, after)

    def test_raw_non_strict_attempt_counts_fail_before_sources_or_network(self):
        event = self.failure_event()
        state = notifier.add_pending_events(
            empty_notification_state(), (event,), self.NOW
        )
        for attempt_count in (True, "1", 1.0):
            with self.subTest(attempt_count=attempt_count):
                with tempfile.TemporaryDirectory() as directory:
                    store = NotificationStateStore(Path(directory) / "state")
                    store.save(state)
                    payload = json.loads(store.path.read_text(encoding="ascii"))
                    payload["deliveries"][event.event_id][
                        "attempt_count"
                    ] = attempt_count
                    store.path.write_text(
                        json.dumps(payload, ensure_ascii=True),
                        encoding="ascii",
                    )
                    malformed = store.path.read_bytes()
                    activity = []

                    def load_execution(_job_id):
                        activity.append("execution")
                        return ()

                    def load_archives():
                        activity.append("archive")
                        return ()

                    code, result = notifier.run_notifier_once(
                        store,
                        notifier_config(),
                        type(
                            "NoSendClient",
                            (),
                            {
                                "send": lambda _self, _card: activity.append(
                                    "send"
                                )
                            },
                        )(),
                        load_execution,
                        load_archives,
                        self.NOW,
                    )
                    after = store.path.read_bytes()

                self.assertEqual(code, 1)
                self.assertEqual(result["result"], "state_error")
                self.assertEqual(activity, [])
                self.assertEqual(after, malformed)

    def test_extreme_aware_times_fail_before_lock_or_sources(self):
        for now in (
            datetime.min.replace(tzinfo=timezone.utc),
            datetime.max.replace(tzinfo=timezone.utc),
        ):
            with self.subTest(now=now):
                activity = []

                class UntouchedStore:
                    def lock(inner_self):
                        activity.append("lock")
                        raise AssertionError("lock must not be acquired")

                code, payload = notifier.run_notifier_once(
                    UntouchedStore(),
                    notifier_config(),
                    type(
                        "NoSendClient",
                        (),
                        {
                            "send": lambda _self, _card: activity.append(
                                "send"
                            )
                        },
                    )(),
                    lambda _job_id: activity.append("execution"),
                    lambda: activity.append("archive"),
                    now,
                )

                self.assertEqual(code, 1)
                self.assertEqual(payload["result"], "invalid_time")
                self.assertEqual(activity, [])

    def test_run_persists_attempt_before_send_and_marks_success(self):
        histories = execution_histories(self.NOW)
        failing_job_id = NOTIFIER_JOBS["review_memory"]
        failure_id = "f" * 32
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda job_id: histories[job_id],
                lambda: (),
                self.NOW,
            )
            current = dict(histories)
            current[failing_job_id] = (
                cron_execution(
                    failure_id,
                    "failed",
                    self.NOW + timedelta(minutes=1),
                    job_id=failing_job_id,
                ),
                *histories[failing_job_id],
            )

            class InspectingClient:
                calls = []

                def send(inner_self, payload):
                    event_id = f"failure:{failing_job_id}:{failure_id}"
                    durable = store.load().deliveries[event_id]
                    self.assertIsNone(durable.delivered_at)
                    self.assertEqual(durable.attempt_count, 1)
                    inner_self.calls.append(payload)

            client = InspectingClient()
            code, payload = notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda job_id: current[job_id],
                lambda: (),
                self.NOW + timedelta(minutes=2),
            )

            delivery = store.load().deliveries[
                f"failure:{failing_job_id}:{failure_id}"
            ]
        self.assertEqual(code, 0)
        self.assertEqual(payload, {
            "ok": True,
            "mode": "run",
            "discovered": 1,
            "delivered": 1,
            "pending": 0,
        })
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(delivery.attempt_count, 1)
        self.assertEqual(delivery.delivered_at, self.NOW + timedelta(minutes=2))
        self.assertEqual(delivery.last_result, "delivered")

    def test_delivery_failure_is_safe_pending_and_does_not_block_next_event(self):
        histories = execution_histories(self.NOW)
        failed_jobs = list(NOTIFIER_JOBS.values())[:2]
        with tempfile.TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "state")
            notifier.initialize_notifier(
                store,
                notifier_config(),
                lambda job_id: histories[job_id],
                lambda: (),
                self.NOW,
            )
            current = dict(histories)
            for index, job_id in enumerate(failed_jobs, start=10):
                current[job_id] = (
                    cron_execution(
                        f"{index:032x}",
                        "failed",
                        self.NOW + timedelta(minutes=index),
                        job_id=job_id,
                    ),
                    *histories[job_id],
                )

            class SelectiveClient:
                def __init__(inner_self):
                    inner_self.calls = 0

                def send(inner_self, _payload):
                    inner_self.calls += 1
                    if inner_self.calls == 1:
                        raise FeishuDeliveryError(
                            "secret-result-must-never-escape"
                        )

            client = SelectiveClient()
            attempt_at = self.NOW + timedelta(minutes=20)
            code, payload = notifier.run_notifier_once(
                store,
                notifier_config(),
                client,
                lambda job_id: current[job_id],
                lambda: (),
                attempt_at,
            )
            state = store.load()

        pending = [
            record for record in state.deliveries.values()
            if record.delivered_at is None
        ]
        self.assertEqual(code, 1)
        self.assertEqual(payload["discovered"], 2)
        self.assertEqual(payload["delivered"], 1)
        self.assertEqual(payload["pending"], 1)
        self.assertNotIn("secret-result-must-never-escape", repr(payload))
        self.assertEqual(client.calls, 2)
        self.assertEqual(pending[0].attempt_count, 1)
        self.assertEqual(pending[0].next_attempt_at, attempt_at + timedelta(minutes=5))
        self.assertEqual(pending[0].last_result, "delivery_error")

    def test_send_test_card_uses_utc_event_id_and_no_state(self):
        class RecordingClient:
            def __init__(self):
                self.cards = []

            def send(self, payload):
                self.cards.append(payload)

        client = RecordingClient()
        result = notifier.send_test_card(
            notifier_config(), client, self.NOW.astimezone(SHANGHAI)
        )

        self.assertEqual(result, {
            "ok": True,
            "mode": "test",
            "event_id": "test:2026-08-17T04:00:00+00:00",
        })
        self.assertEqual(len(client.cards), 1)
        self.assertEqual(
            client.cards[0]["card"]["header"],
            {
                "template": "orange",
                "title": {
                    "tag": "plain_text",
                    "content": "TradingAgents 飞书通知配置验收",
                },
            },
        )


class HermesFeishuNotifierCliTests(unittest.TestCase):
    def _main(self, argv, config=None):
        stream = io.StringIO()
        with patch("sys.stdout", stream), patch("sys.stderr", io.StringIO()):
            result = notifier.main(argv, config=config or notifier_config())
        return result, stream.getvalue()

    def test_test_requires_exact_confirmation_without_runtime_dependencies(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(notifier, "NotificationStateStore") as store,
            patch.object(notifier, "FeishuClient") as client,
            patch.object(notifier, "load_cron_runs") as runs,
            patch.object(notifier, "_load_report_inventory") as inventory,
            patch("sys.stdout", stdout),
            patch("sys.stderr", stderr),
        ):
            code = notifier.main(["test"], config=notifier_config())

        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {
                "ok": False,
                "mode": "test",
                "error": {
                    "code": "INVALID_NOTIFY_REQUEST",
                    "message": "The Feishu notifier request is invalid.",
                    "suggested_action": (
                        "Use initialize, run, or explicitly confirmed test mode."
                    ),
                },
            },
        )
        self.assertTrue(stdout.getvalue().isascii())
        self.assertEqual(stderr.getvalue(), "")
        store.assert_not_called()
        client.assert_not_called()
        runs.assert_not_called()
        inventory.assert_not_called()

    def test_rejects_every_form_except_the_three_exact_commands(self):
        rejected = (
            [],
            ["unknown"],
            ["initialize", "extra"],
            ["run", "--confirm-external-send"],
            ["test", "--confirm-external-send", "--confirm-external-send"],
            ["test", "--confirm-external-sends"],
            ["test", "--confirm-external-send", "extra"],
        )
        for argv in rejected:
            with self.subTest(argv=argv):
                code, stdout = self._main(list(argv))
                payload = json.loads(stdout)
                self.assertEqual(code, 1)
                self.assertEqual(payload["error"]["code"], "INVALID_NOTIFY_REQUEST")
                self.assertEqual(stdout.count("\n"), 1)

    def test_initialize_uses_no_client_and_fixed_runtime_dependencies(self):
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        store = SimpleNamespace()
        expected = {"ok": True, "mode": "initialize"}
        with (
            patch.object(notifier, "NotificationStateStore", return_value=store) as state_store,
            patch.object(notifier, "FeishuClient") as client,
            patch.object(
                notifier, "initialize_notifier", return_value=(object(), expected)
            ) as initialize,
            patch.object(notifier, "load_cron_runs", return_value=()) as runs,
            patch.object(notifier, "_load_report_inventory", return_value=object()) as inventory,
            patch.object(notifier, "_utc_now", return_value=now),
        ):
            code, stdout = self._main(["initialize"])
            execution_loader = initialize.call_args.args[2]
            archive_loader = initialize.call_args.args[3]
            execution_loader(JOB_ID)
            archive_loader()

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), expected)
        state_store.assert_called_once_with(notifier.STATE_ROOT)
        client.assert_not_called()
        initialize.assert_called_once()
        self.assertEqual(initialize.call_args.args[4], now)
        runs.assert_called_once_with(JOB_ID, hermes_cli=notifier.HERMES_CLI)
        inventory.assert_called_once_with(notifier.RESULTS_ROOT)

    def test_run_constructs_configured_client_and_uses_fixed_defaults(self):
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        store = SimpleNamespace()
        expected = {"ok": True, "mode": "run", "discovered": 0, "delivered": 0, "pending": 0}
        config = notifier_config()
        with (
            patch.object(notifier, "NotificationStateStore", return_value=store),
            patch.object(notifier, "FeishuClient") as client,
            patch.object(notifier, "run_notifier_once", return_value=(0, expected)) as run_once,
            patch.object(notifier, "load_cron_runs", return_value=()) as runs,
            patch.object(notifier, "_load_report_inventory", return_value=object()) as inventory,
            patch.object(notifier, "_utc_now", return_value=now),
        ):
            code, stdout = self._main(["run"], config)
            args = run_once.call_args.args
            args[3](JOB_ID)
            args[4]()

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), expected)
        client.assert_called_once_with(config)
        self.assertEqual(args[:3], (store, config, client.return_value))
        self.assertEqual(args[5], now)
        runs.assert_called_once_with(JOB_ID, hermes_cli=notifier.HERMES_CLI)
        inventory.assert_called_once_with(notifier.RESULTS_ROOT)

    def test_test_mode_sends_once_only_after_confirmation(self):
        now = datetime(2026, 8, 17, tzinfo=timezone.utc)
        config = notifier_config()
        expected = {"ok": True, "mode": "test", "event_id": "test:ok"}
        with (
            patch.object(notifier, "NotificationStateStore") as store,
            patch.object(notifier, "FeishuClient") as client,
            patch.object(notifier, "send_test_card", return_value=expected) as send,
            patch.object(notifier, "_utc_now", return_value=now),
        ):
            code, stdout = self._main(["test", "--confirm-external-send"], config)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), expected)
        store.assert_not_called()
        client.assert_called_once_with(config)
        send.assert_called_once_with(config, client.return_value, now)


class HermesFeishuBootstrapTests(unittest.TestCase):
    def test_loads_config_before_late_runner_import_and_preserves_arguments(self):
        from tradingagents.integrations import hermes_feishu_bootstrap as bootstrap

        config = notifier_config()
        received = []

        def runner_main(argv, *, config):
            received.append((argv, config))
            return 7

        runner = SimpleNamespace(main=runner_main)
        order = []
        with (
            patch.object(bootstrap, "load_private_config", side_effect=lambda path: order.append("load") or config),
            patch.object(bootstrap, "import_module", side_effect=lambda name: order.append("import") or runner),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
        ):
            code = bootstrap.main(["run"])

        self.assertEqual(code, 7)
        self.assertEqual(order, ["load", "import"])
        self.assertEqual(received, [(["run"], config)])

    def test_startup_failures_are_constant_safe_json_and_do_not_import_after_config_failure(self):
        from tradingagents.integrations import hermes_feishu_bootstrap as bootstrap

        marker = "https://example.invalid/hook/secret-signature"
        for failure in ("config", "import", "runner"):
            with self.subTest(failure=failure):
                stream = io.StringIO()
                runner = SimpleNamespace(main=lambda argv, *, config: (_ for _ in ()).throw(RuntimeError(marker)))
                with (
                    patch.object(
                        bootstrap,
                        "load_private_config",
                        side_effect=RuntimeError(marker) if failure == "config" else notifier_config(),
                    ),
                    patch.object(
                        bootstrap,
                        "import_module",
                        side_effect=RuntimeError(marker) if failure == "import" else lambda name: runner,
                    ) as importer,
                    patch("sys.stdout", stream),
                    patch("sys.stderr", io.StringIO()),
                ):
                    code = bootstrap.main(["run"])

                self.assertEqual(code, 1)
                self.assertEqual(
                    json.loads(stream.getvalue()),
                    {
                        "ok": False,
                        "mode": "run",
                        "error": {
                            "code": "FEISHU_NOTIFIER_FAILED",
                            "message": "The Feishu notifier could not complete.",
                            "suggested_action": (
                                "Inspect the safe notifier Cron result and private configuration."
                            ),
                        },
                    },
                )
                self.assertNotIn(marker, stream.getvalue())
                if failure == "config":
                    importer.assert_not_called()

    def test_wrapper_is_exact_executable_no_agent_command(self):
        wrapper = (
            Path(__file__).parents[1]
            / "deploy"
            / "hermes"
            / "scripts"
            / "tradingagents-feishu-notifier.sh"
        )
        self.assertEqual(
            wrapper.read_text(encoding="ascii"),
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n\n"
            "PROJECT_DIR=/home/ubuntu/workspace/TradingAgents-crypto\n"
            "exec \"$PROJECT_DIR/.venv-hermes-mcp/bin/python\" -m "
            "tradingagents.integrations.hermes_feishu_bootstrap run \"$@\"\n",
        )
        self.assertNotEqual(wrapper.stat().st_mode & 0o111, 0)


if __name__ == "__main__":
    unittest.main()
