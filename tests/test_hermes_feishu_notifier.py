import dataclasses
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess

from tradingagents.integrations.hermes_feishu_notifier import (
    CronExecution,
    ExecutionDiscoveryError,
    discover_execution_events,
    load_cron_runs,
    parse_cron_runs,
)
from tradingagents.integrations.hermes_feishu_state import initialized_state


JOB_ID = "e93cfab5f78e"
ARCHIVE_JOB_ID = "5b7f7906306a"
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


def run_header(index, *, claimed_at=None):
    occurred_at = claimed_at or (
        datetime(2026, 8, 14, tzinfo=timezone.utc)
        + timedelta(minutes=index)
    )
    return (
        f"{index:032x}  completed  job={JOB_ID}  source=schedule  "
        f"{occurred_at.isoformat()}"
    )


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


if __name__ == "__main__":
    unittest.main()
