"""Fail-closed discovery of Hermes Cron execution events."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, Protocol, TypeGuard
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from tradingagents.integrations.hermes_feishu_client import (
    EXPECTED_JOB_NAMES,
    FeishuDeliveryError,
    FeishuClient,
    FeishuNotifierConfig,
    ReportCardData,
    ReportCardItem,
    render_failure_card,
    render_missing_archive_card,
    render_report_card,
    render_test_card,
)
from tradingagents.integrations.hermes_feishu_state import (
    DeliveryRecord,
    MAX_ATTEMPT_COUNT,
    NotificationAlreadyRunning,
    NotificationEvent,
    NotificationState,
    NotificationStateError,
    NotificationStateStore,
    initialized_state,
    retry_delay,
)
from tradingagents.integrations.schemas import DailyReportBatch


ExecutionStatus = Literal[
    "claimed", "running", "completed", "failed", "unknown"
]
EXECUTION_STATUSES = frozenset(
    {"claimed", "running", "completed", "failed", "unknown"}
)
NONTERMINAL_EXECUTION_STATUSES = frozenset({"claimed", "running"})
EXECUTION_ERROR_MESSAGE = "Hermes execution history unavailable"
NO_EXECUTIONS_LINE = "No cron execution attempts recorded."
DEFAULT_HERMES_CLI = Path("/home/ubuntu/.local/bin/hermes")
RESULTS_ROOT = Path.cwd() / "results"
STATE_ROOT = RESULTS_ROOT / "hermes" / "feishu_notifications"
HERMES_CLI = Path.home() / ".local" / "bin" / "hermes"
MAX_EXECUTION_ROWS = 500
MAX_BATCH_BYTES = 1_048_576
MAX_REPORT_BYTES = 20 * 1_048_576
REPORT_DISCOVERY_ERROR_MESSAGE = "daily report archive unavailable"
SHANGHAI = ZoneInfo("Asia/Shanghai")
RUN_LINE = re.compile(
    r"^(?P<id>[0-9a-f]{32})  "
    r"(?P<status>claimed|running|completed|failed|unknown)\s+"
    r"job=(?P<job_id>[0-9a-f]{12})  "
    r"source=(?P<source>[^\s]+)  (?P<claimed_at>[^\s]+)$"
)
EXECUTION_ID = re.compile(r"^[0-9a-f]{32}$")
JOB_ID = re.compile(r"^[0-9a-f]{12}$")


class ExecutionDiscoveryError(RuntimeError):
    """Raised without carrying unsafe execution output or process details."""

    def __init__(self, *_ignored: object) -> None:
        super().__init__(EXECUTION_ERROR_MESSAGE)


class ReportDiscoveryError(RuntimeError):
    """Raised without preserving untrusted report archive details."""

    def __init__(self, *_ignored: object) -> None:
        super().__init__(REPORT_DISCOVERY_ERROR_MESSAGE)


@dataclass(frozen=True)
class VerifiedArchive:
    """A hash-verified archive with structured data for notification cards."""

    trade_date: date
    batch_id: str
    report_sha256: str
    state: str
    items: tuple[ReportCardItem, ...]
    report_path: Path
    archived_at: datetime
    previous: "VerifiedArchive | None" = None

    @property
    def event_id(self) -> str:
        return (
            f"report:{self.trade_date.isoformat()}:{self.report_sha256}"
        )

    def to_card_data(self, event_id: str) -> ReportCardData:
        return ReportCardData(
            event_id=event_id,
            trade_date=self.trade_date,
            state=self.state,
            items=self.items,
            report_path=self.report_path,
        )


def _is_job_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and JOB_ID.fullmatch(value) is not None


@dataclass(frozen=True)
class CronExecution:
    execution_id: str
    job_id: str
    status: ExecutionStatus
    source: str
    claimed_at: datetime

    def __post_init__(self) -> None:
        try:
            aware = (
                isinstance(self.claimed_at, datetime)
                and self.claimed_at.utcoffset() is not None
            )
        except (OverflowError, ValueError):
            aware = False
        valid = (
            isinstance(self.execution_id, str)
            and EXECUTION_ID.fullmatch(self.execution_id) is not None
            and _is_job_id(self.job_id)
            and isinstance(self.status, str)
            and self.status in EXECUTION_STATUSES
            and isinstance(self.source, str)
            and bool(self.source)
            and not any(character.isspace() for character in self.source)
            and aware
        )
        if not valid:
            raise ValueError("invalid Cron execution")


def _aware_datetime(value: str) -> datetime | None:
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(value)
        offset = parsed.utcoffset()
    except (TypeError, ValueError, OverflowError):
        offset = None
    if parsed is None or offset is None:
        return None
    return parsed


def _valid_execution(record: object, job_id: str) -> bool:
    if not isinstance(record, CronExecution):
        return False
    try:
        aware = record.claimed_at.utcoffset() is not None
    except (AttributeError, OverflowError, ValueError):
        aware = False
    return (
        EXECUTION_ID.fullmatch(record.execution_id) is not None
        and record.job_id == job_id
        and record.status in EXECUTION_STATUSES
        and bool(record.source)
        and not any(character.isspace() for character in record.source)
        and aware
    )


def _normalize_rows(rows: Sequence[CronExecution]) -> tuple[CronExecution, ...]:
    """Return newest-first rows, preserving Hermes CLI order for time ties."""

    return tuple(
        sorted(rows, key=lambda record: record.claimed_at, reverse=True)
    )


def parse_cron_runs(output: str, job_id: str) -> tuple[CronExecution, ...]:
    """Parse safe headers while discarding all execution detail lines."""

    if not isinstance(output, str) or not _is_job_id(job_id):
        raise ExecutionDiscoveryError()

    lines = output.splitlines()
    first_nonempty = next(
        (index for index, line in enumerate(lines) if line.strip()), None
    )
    if first_nonempty is None:
        raise ExecutionDiscoveryError()

    first_line = lines[first_nonempty]
    if first_line == NO_EXECUTIONS_LINE:
        if any(line.strip() for line in lines[first_nonempty + 1 :]):
            raise ExecutionDiscoveryError()
        return ()
    if RUN_LINE.fullmatch(first_line) is None:
        raise ExecutionDiscoveryError()

    records: list[CronExecution] = []
    execution_ids: set[str] = set()
    for line in lines[first_nonempty:]:
        match = RUN_LINE.fullmatch(line)
        if match is None:
            if line and not line[0].isspace():
                raise ExecutionDiscoveryError()
            continue

        values = match.groupdict()
        claimed_at = _aware_datetime(values["claimed_at"])
        invalid = (
            values["job_id"] != job_id
            or values["id"] in execution_ids
            or claimed_at is None
        )
        if invalid:
            raise ExecutionDiscoveryError()

        execution_ids.add(values["id"])
        records.append(
            CronExecution(
                execution_id=values["id"],
                job_id=values["job_id"],
                status=values["status"],
                source=values["source"],
                claimed_at=claimed_at,
            )
        )

    if len(records) > MAX_EXECUTION_ROWS:
        raise ExecutionDiscoveryError()
    return _normalize_rows(records)


def _absolute_cli_path(value: object) -> Path | None:
    try:
        path = Path(value)
        absolute = path.is_absolute()
    except Exception:
        return None
    return path if absolute else None


def _successful_stdout(result: object) -> str | None:
    if not isinstance(result, subprocess.CompletedProcess):
        return None
    try:
        returncode = result.returncode
    except Exception:
        return None
    if type(returncode) is not int or returncode != 0:
        return None
    try:
        stdout = result.stdout
    except Exception:
        return None
    return stdout if isinstance(stdout, str) else None


def load_cron_runs(
    job_id: str,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    hermes_cli: Path = DEFAULT_HERMES_CLI,
) -> tuple[CronExecution, ...]:
    """Load the bounded Hermes execution window through an absolute CLI path."""

    if not _is_job_id(job_id):
        raise ExecutionDiscoveryError()
    cli_path = _absolute_cli_path(hermes_cli)
    if cli_path is None:
        raise ExecutionDiscoveryError()

    command = [
        str(cli_path),
        "cron",
        "runs",
        job_id,
        "--limit",
        str(MAX_EXECUTION_ROWS),
    ]
    process_failed = False
    result: object = None
    try:
        result = run_command(
            command,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except Exception:
        process_failed = True

    if process_failed:
        raise ExecutionDiscoveryError()
    stdout = _successful_stdout(result)
    if stdout is None:
        raise ExecutionDiscoveryError()
    return parse_cron_runs(stdout, job_id)


DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class _ReportInventory:
    archives: tuple[VerifiedArchive, ...]
    unarchived_dates: frozenset[date]


def _open_directory(
    path: str | Path, *, parent_descriptor: int | None = None, missing: bool = False
) -> int | None:
    try:
        descriptor = os.open(
            path, DIRECTORY_FLAGS, dir_fd=parent_descriptor
        )
    except FileNotFoundError:
        if missing:
            return None
        raise
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("invalid directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_regular_bytes_at(
    directory_descriptor: int, name: str, maximum_size: int
) -> bytes:
    """Read a bounded regular leaf without reopening through a symlink."""

    metadata = os.stat(
        name, dir_fd=directory_descriptor, follow_symlinks=False
    )
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_size:
        raise OSError("invalid source")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_descriptor
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_size:
            raise OSError("invalid source")
        chunks: list[bytes] = []
        remaining = maximum_size
        while True:
            chunk = os.read(descriptor, min(65_536, remaining + 1))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
            if remaining < 0:
                raise OSError("oversized source")
        return b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _is_aware(value: object) -> bool:
    if not isinstance(value, datetime):
        return False
    try:
        return value.utcoffset() is not None
    except (OverflowError, ValueError):
        return False


def _snapshot_archives(
    archives: Sequence[VerifiedArchive],
) -> tuple[VerifiedArchive, ...]:
    snapshots: list[VerifiedArchive] = []
    previous: VerifiedArchive | None = None
    for archive in sorted(archives, key=lambda item: item.trade_date):
        snapshot = VerifiedArchive(
            trade_date=archive.trade_date,
            batch_id=archive.batch_id,
            report_sha256=archive.report_sha256,
            state=archive.state,
            items=archive.items,
            report_path=archive.report_path,
            archived_at=archive.archived_at,
            previous=previous,
        )
        snapshots.append(snapshot)
        previous = snapshot
    return tuple(snapshots)


def _coerce_results_root(results_root: object) -> Path:
    try:
        return Path(results_root)
    except Exception:
        pass
    raise ReportDiscoveryError()


def _load_report_inventory(results_root: object) -> _ReportInventory:
    """Build one fully verified, descriptor-anchored report inventory."""

    root_descriptor: int | None = None
    hermes_descriptor: int | None = None
    batches_descriptor: int | None = None
    reports_descriptor: int | None = None
    try:
        root = _coerce_results_root(results_root)
        root_descriptor = _open_directory(root, missing=True)
        if root_descriptor is None:
            return _ReportInventory((), frozenset())
        hermes_descriptor = _open_directory(
            "hermes", parent_descriptor=root_descriptor, missing=True
        )
        if hermes_descriptor is None:
            return _ReportInventory((), frozenset())
        batches_descriptor = _open_directory(
            "report_batches", parent_descriptor=hermes_descriptor, missing=True
        )
        if batches_descriptor is None:
            return _ReportInventory((), frozenset())

        batches: list[DailyReportBatch] = []
        seen_dates: set[date] = set()
        for name in sorted(os.listdir(batches_descriptor)):
            if not name.endswith(".json"):
                continue
            raw = _read_regular_bytes_at(
                batches_descriptor, name, MAX_BATCH_BYTES
            )
            batch = DailyReportBatch.model_validate_json(raw.decode("utf-8"))
            expected_name = f"{batch.request.trade_date.isoformat()}.json"
            if name != expected_name or batch.request.trade_date in seen_dates:
                raise ValueError("invalid canonical batch")
            seen_dates.add(batch.request.trade_date)
            batches.append(batch)

        unarchived_dates: set[date] = set()
        archives: list[VerifiedArchive] = []
        for batch in sorted(batches, key=lambda item: item.request.trade_date):
            archive = batch.archive
            if archive is None:
                unarchived_dates.add(batch.request.trade_date)
                continue
            expected_filename = f"{batch.request.trade_date.isoformat()}.md"
            if archive.filename != expected_filename or not _is_aware(
                archive.archived_at
            ):
                raise ValueError("invalid archive metadata")
            if reports_descriptor is None:
                reports_descriptor = _open_directory(
                    "reports", parent_descriptor=hermes_descriptor
                )
            report_bytes = _read_regular_bytes_at(
                reports_descriptor, expected_filename, MAX_REPORT_BYTES
            )
            if hashlib.sha256(report_bytes).hexdigest() != archive.sha256:
                raise ValueError("report digest mismatch")
            archives.append(
                VerifiedArchive(
                    trade_date=batch.request.trade_date,
                    batch_id=batch.batch_id,
                    report_sha256=archive.sha256,
                    state=archive.state,
                    items=tuple(
                        ReportCardItem(
                            symbol=item.symbol,
                            status=item.status,
                            processed_signal=item.processed_signal,
                            final_trade_decision=item.final_trade_decision,
                            error_code=item.error_code,
                        )
                        for item in archive.items
                    ),
                    report_path=(
                        root / "hermes" / "reports" / expected_filename
                    ),
                    archived_at=archive.archived_at,
                )
            )
        return _ReportInventory(
            _snapshot_archives(archives), frozenset(unarchived_dates)
        )
    except (
        OSError,
        RuntimeError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
        TypeError,
    ):
        pass
    finally:
        for descriptor in (
            reports_descriptor,
            batches_descriptor,
            hermes_descriptor,
            root_descriptor,
        ):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    raise ReportDiscoveryError()


def load_verified_archives(results_root: object) -> tuple[VerifiedArchive, ...]:
    """Load only complete, hash-verified daily report archive snapshots."""

    return _load_report_inventory(results_root).archives


def discover_report_events(
    state: NotificationState, archives: Sequence[VerifiedArchive]
) -> tuple[NotificationEvent, ...]:
    """Create undelivered report events in immutable archive order."""

    seen = set(state.seen_report_event_ids)
    deliveries = state.deliveries
    events: list[NotificationEvent] = []
    for archive in sorted(archives, key=lambda item: item.trade_date):
        if archive.event_id in seen or archive.event_id in deliveries:
            continue
        events.append(
            NotificationEvent(
                event_id=archive.event_id,
                kind="report",
                created_at=archive.archived_at,
                trade_date=archive.trade_date,
                report_sha256=archive.report_sha256,
                batch_state=archive.state,
            )
        )
    return tuple(events)


def discover_missing_archive_events(
    results_root: Path,
    completed_archive_rows: Sequence[CronExecution],
    verified_archives: Sequence[VerifiedArchive],
    state: NotificationState,
    *,
    job_name: str,
    daily_archive_job_id: str,
) -> tuple[NotificationEvent, ...]:
    """Report completed archive jobs whose exact-date batch is unarchived."""

    if not isinstance(job_name, str) or not job_name or not _is_job_id(
        daily_archive_job_id
    ):
        raise ReportDiscoveryError()
    rows = tuple(completed_archive_rows)
    if any(
        not isinstance(archive, VerifiedArchive)
        for archive in verified_archives
    ):
        raise ReportDiscoveryError()
    if any(
        not _valid_execution(row, daily_archive_job_id)
        or row.status != "completed"
        for row in rows
    ):
        raise ReportDiscoveryError()
    inventory = _load_report_inventory(results_root)
    return _discover_missing_archive_events_from_inventory(
        inventory,
        rows,
        state,
        job_name=job_name,
        daily_archive_job_id=daily_archive_job_id,
    )


def discover_execution_events(
    state: NotificationState,
    job_name: str,
    job_id: str,
    rows: Sequence[CronExecution],
    *,
    daily_archive_job_id: str,
) -> tuple[
    NotificationState,
    tuple[NotificationEvent, ...],
    tuple[CronExecution, ...],
]:
    """Discover new failures and completed daily-archive executions."""

    if not _is_job_id(job_id) or not _is_job_id(daily_archive_job_id):
        raise ExecutionDiscoveryError()
    observed = tuple(rows)
    if (
        len(observed) > MAX_EXECUTION_ROWS
        or any(not _valid_execution(row, job_id) for row in observed)
        or len({row.execution_id for row in observed}) != len(observed)
    ):
        raise ExecutionDiscoveryError()

    newest_first = _normalize_rows(observed)
    cursor = state.execution_cursors.get(job_id)
    observed_ids = [row.execution_id for row in newest_first]
    if newest_first and cursor is not None and cursor not in observed_ids:
        raise ExecutionDiscoveryError()

    cursor_index = (
        observed_ids.index(cursor)
        if cursor is not None and newest_first
        else len(newest_first)
    )
    new_rows_oldest_first = tuple(reversed(newest_first[:cursor_index]))

    failure_events: list[NotificationEvent] = []
    completed_archive_rows: list[CronExecution] = []
    advanced_cursor = cursor
    cursor_blocked = False
    for row in new_rows_oldest_first:
        if not cursor_blocked:
            if row.status in NONTERMINAL_EXECUTION_STATUSES:
                cursor_blocked = True
            else:
                advanced_cursor = row.execution_id
        if row.status == "failed":
            event_id = f"failure:{job_id}:{row.execution_id}"
            if event_id not in state.deliveries:
                failure_events.append(
                    NotificationEvent(
                        event_id=event_id,
                        kind="execution_failure",
                        created_at=row.claimed_at,
                        job_name=job_name,
                        job_id=job_id,
                        execution_id=row.execution_id,
                    )
                )
        elif row.status == "completed" and job_id == daily_archive_job_id:
            completed_archive_rows.append(row)

    execution_cursors = dict(state.execution_cursors)
    seen_execution_ids = {
        existing_job_id: list(execution_ids)
        for existing_job_id, execution_ids in state.seen_execution_ids.items()
    }
    execution_cursors[job_id] = advanced_cursor
    seen_execution_ids[job_id] = observed_ids
    updated_state = state.model_copy(
        update={
            "execution_cursors": execution_cursors,
            "seen_execution_ids": seen_execution_ids,
        }
    )

    return (
        updated_state,
        tuple(failure_events),
        tuple(completed_archive_rows),
    )


SAFE_DELIVERY_RESULTS = frozenset(
    {
        "timeout",
        "connection_error",
        "response_too_large",
        "http_error",
        "redirect_rejected",
        "rate_limited",
        "invalid_response",
        "feishu_error",
    }
)


class _CardClient(Protocol):
    def send(self, payload: dict[str, Any]) -> None: ...


def _require_aware_now(now: object) -> datetime:
    try:
        aware = _is_aware(now)
    except Exception:
        aware = False
    if not aware:
        raise ValueError("notification time unavailable")
    try:
        now - timedelta(days=90)
        now + timedelta(hours=24)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("notification time unavailable") from None
    return now


def _configured_jobs(config: object) -> tuple[tuple[str, str], ...]:
    try:
        jobs = config.jobs
        items = tuple(sorted(jobs.items()))
        invalid = (
            {name for name, _job_id in items} != EXPECTED_JOB_NAMES
            or len(items) != len(EXPECTED_JOB_NAMES)
            or len({job_id for _name, job_id in items}) != len(items)
            or any(
                not isinstance(name, str) or not _is_job_id(job_id)
                for name, job_id in items
            )
        )
    except Exception:
        raise ExecutionDiscoveryError() from None
    if invalid:
        raise ExecutionDiscoveryError()
    return items


def _normalize_report_inventory(value: object) -> _ReportInventory:
    try:
        if isinstance(value, _ReportInventory):
            archives = value.archives
            unarchived_dates = value.unarchived_dates
        elif isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            archives = tuple(value)
            unarchived_dates = frozenset()
        else:
            raise TypeError("invalid inventory")
        if any(not isinstance(item, VerifiedArchive) for item in archives):
            raise TypeError("invalid archive")
        if any(type(item) is not date for item in unarchived_dates):
            raise TypeError("invalid unarchived date")
        if any(
            type(archive.trade_date) is not date
            or re.fullmatch(r"[0-9a-f]{64}", archive.report_sha256) is None
            or not _is_aware(archive.archived_at)
            for archive in archives
        ):
            raise ValueError("invalid archive")
        snapshots = _snapshot_archives(archives)
        if (
            len({archive.trade_date for archive in snapshots}) != len(snapshots)
            or len({archive.event_id for archive in snapshots}) != len(snapshots)
        ):
            raise ValueError("duplicate archive")
        return _ReportInventory(snapshots, frozenset(unarchived_dates))
    except Exception:
        raise ReportDiscoveryError() from None


def _load_inventory(
    archive_loader: Callable[[], object],
) -> _ReportInventory:
    try:
        value = archive_loader()
    except Exception:
        raise ReportDiscoveryError() from None
    return _normalize_report_inventory(value)


def _load_execution_histories(
    config: object,
    execution_loader: Callable[[str], Sequence[CronExecution]],
) -> tuple[tuple[str, str, tuple[CronExecution, ...]], ...]:
    histories: list[tuple[str, str, tuple[CronExecution, ...]]] = []
    for job_name, job_id in _configured_jobs(config):
        try:
            rows = tuple(execution_loader(job_id))
        except Exception:
            raise ExecutionDiscoveryError() from None
        if (
            len(rows) > MAX_EXECUTION_ROWS
            or any(not _valid_execution(row, job_id) for row in rows)
            or len({row.execution_id for row in rows}) != len(rows)
        ):
            raise ExecutionDiscoveryError()
        histories.append((job_name, job_id, _normalize_rows(rows)))
    return tuple(histories)


def _baseline_state(
    histories: Sequence[tuple[str, str, tuple[CronExecution, ...]]],
    inventory: _ReportInventory,
    now: datetime,
) -> NotificationState:
    execution_ids: dict[str, list[str]] = {}
    cursors: dict[str, str | None] = {}
    for _job_name, job_id, rows in histories:
        ids = [row.execution_id for row in rows]
        execution_ids[job_id] = ids
        prefix_length = 0
        while (
            prefix_length < len(rows)
            and rows[prefix_length].status in NONTERMINAL_EXECUTION_STATUSES
        ):
            prefix_length += 1
        if any(
            row.status in NONTERMINAL_EXECUTION_STATUSES
            for row in rows[prefix_length:]
        ):
            raise ExecutionDiscoveryError()
        cursors[job_id] = (
            rows[prefix_length].execution_id
            if prefix_length < len(rows)
            else None
        )
    state = initialized_state(
        now,
        execution_ids,
        [archive.event_id for archive in inventory.archives],
    )
    return state.model_copy(update={"execution_cursors": cursors})


def _initialize_payload(
    state: NotificationState, *, already_initialized: bool
) -> dict[str, object]:
    return {
        "ok": True,
        "mode": "initialize",
        "already_initialized": already_initialized,
        "execution_count": sum(
            len(execution_ids)
            for execution_ids in state.seen_execution_ids.values()
        ),
        "report_count": len(state.seen_report_event_ids),
    }


def initialize_notifier(
    store: object,
    config: object,
    execution_loader: Callable[[str], Sequence[CronExecution]],
    archive_loader: Callable[[], object],
    now: datetime,
) -> tuple[NotificationState, dict[str, object]]:
    """Persist a no-send baseline from one coherent source snapshot."""

    checked_now = _require_aware_now(now)
    with store.lock():
        existing = store.load_optional()
        if existing is not None:
            return existing, _initialize_payload(
                existing, already_initialized=True
            )
        histories = _load_execution_histories(config, execution_loader)
        inventory = _load_inventory(archive_loader)
        state = _baseline_state(histories, inventory, checked_now)
        store.save(state)
        return state, _initialize_payload(state, already_initialized=False)


def _discover_missing_archive_events_from_inventory(
    inventory: _ReportInventory,
    completed_archive_rows: Sequence[CronExecution],
    state: NotificationState,
    *,
    job_name: str,
    daily_archive_job_id: str,
) -> tuple[NotificationEvent, ...]:
    if not isinstance(job_name, str) or not job_name or not _is_job_id(
        daily_archive_job_id
    ):
        raise ReportDiscoveryError()
    rows = tuple(completed_archive_rows)
    if any(
        not _valid_execution(row, daily_archive_job_id)
        or row.status != "completed"
        for row in rows
    ):
        raise ReportDiscoveryError()
    archived_dates = {archive.trade_date for archive in inventory.archives}
    events: list[NotificationEvent] = []
    known_event_ids = set(state.deliveries)
    seen_execution_ids: set[str] = set()
    for row in sorted(rows, key=lambda item: item.claimed_at):
        if row.execution_id in seen_execution_ids:
            continue
        seen_execution_ids.add(row.execution_id)
        trade_date = row.claimed_at.astimezone(SHANGHAI).date()
        event_id = f"missing_archive:{row.job_id}:{row.execution_id}"
        if (
            trade_date not in inventory.unarchived_dates
            or trade_date in archived_dates
            or event_id in known_event_ids
        ):
            continue
        events.append(
            NotificationEvent(
                event_id=event_id,
                kind="missing_archive",
                created_at=row.claimed_at,
                trade_date=trade_date,
                batch_state="unarchived",
                job_name=job_name,
                job_id=row.job_id,
                execution_id=row.execution_id,
            )
        )
    return tuple(events)


def discover_all_events(
    state: NotificationState,
    config: object,
    execution_loader: Callable[[str], Sequence[CronExecution]],
    archive_loader: Callable[[], object],
) -> tuple[NotificationState, tuple[NotificationEvent, ...], _ReportInventory]:
    """Discover every source in memory before returning updated cursors."""

    histories = _load_execution_histories(config, execution_loader)
    inventory = _load_inventory(archive_loader)
    working = state
    events: list[NotificationEvent] = []
    completed_archive_rows: list[CronExecution] = []
    archive_job_id = dict(_configured_jobs(config))["daily_archive"]
    for job_name, job_id, rows in histories:
        working, failures, completed = discover_execution_events(
            working,
            job_name,
            job_id,
            rows,
            daily_archive_job_id=archive_job_id,
        )
        events.extend(failures)
        completed_archive_rows.extend(completed)
    events.extend(discover_report_events(working, inventory.archives))
    events.extend(
        _discover_missing_archive_events_from_inventory(
            inventory,
            completed_archive_rows,
            working,
            job_name="daily_archive",
            daily_archive_job_id=archive_job_id,
        )
    )
    unique_events = {event.event_id: event for event in events}
    return (
        working,
        tuple(unique_events[event_id] for event_id in sorted(unique_events)),
        inventory,
    )


def add_pending_events(
    state: NotificationState,
    events: Sequence[NotificationEvent],
    now: datetime,
) -> NotificationState:
    checked_now = _require_aware_now(now)
    deliveries = dict(state.deliveries)
    seen_report_event_ids = set(state.seen_report_event_ids)
    for event in sorted(events, key=lambda item: item.event_id):
        if event.event_id not in deliveries:
            deliveries[event.event_id] = DeliveryRecord(
                event=event, next_attempt_at=checked_now
            )
        if event.kind == "report":
            seen_report_event_ids.add(event.event_id)
    return state.model_copy(
        update={
            "deliveries": deliveries,
            "seen_report_event_ids": sorted(seen_report_event_ids),
        }
    )


def due_event_ids(state: NotificationState, now: datetime) -> tuple[str, ...]:
    checked_now = _require_aware_now(now)
    return tuple(
        event_id
        for event_id, record in sorted(state.deliveries.items())
        if record.delivered_at is None
        and record.next_attempt_at <= checked_now
    )


def begin_attempt(
    state: NotificationState, event_id: str, now: datetime
) -> NotificationState:
    checked_now = _require_aware_now(now)
    record = state.deliveries[event_id]
    if (
        type(record.attempt_count) is not int
        or not 0 <= record.attempt_count < MAX_ATTEMPT_COUNT
    ):
        raise ValueError("invalid delivery attempt count")
    attempt_count = record.attempt_count + 1
    deliveries = dict(state.deliveries)
    deliveries[event_id] = record.model_copy(
        update={
            "attempt_count": attempt_count,
            "next_attempt_at": checked_now + retry_delay(attempt_count),
        }
    )
    return state.model_copy(update={"deliveries": deliveries})


def record_delivery_success(
    state: NotificationState, event_id: str, now: datetime
) -> NotificationState:
    checked_now = _require_aware_now(now)
    deliveries = dict(state.deliveries)
    deliveries[event_id] = deliveries[event_id].model_copy(
        update={"delivered_at": checked_now, "last_result": "delivered"}
    )
    return state.model_copy(update={"deliveries": deliveries})


def _record_failure_result(
    state: NotificationState,
    event_id: str,
    now: datetime,
    result: str,
    retry_after_seconds: object = None,
) -> NotificationState:
    checked_now = _require_aware_now(now)
    deliveries = dict(state.deliveries)
    record = deliveries[event_id]
    next_attempt_at = record.next_attempt_at
    if (
        type(retry_after_seconds) is int
        and 0 <= retry_after_seconds <= 86_400
    ):
        extension = checked_now + timedelta(seconds=retry_after_seconds)
        cap = checked_now + timedelta(hours=24)
        next_attempt_at = min(max(next_attempt_at, extension), cap)
    deliveries[event_id] = record.model_copy(
        update={
            "last_result": result,
            "next_attempt_at": next_attempt_at,
        }
    )
    return state.model_copy(update={"deliveries": deliveries})


def record_delivery_failure(
    state: NotificationState,
    event_id: str,
    now: datetime,
    error: FeishuDeliveryError,
) -> NotificationState:
    result = (
        error.result
        if isinstance(error.result, str)
        and error.result in SAFE_DELIVERY_RESULTS
        else "delivery_error"
    )
    return _record_failure_result(
        state,
        event_id,
        now,
        result,
        error.retry_after_seconds,
    )


def render_persisted_event(
    event: NotificationEvent,
    archives: _ReportInventory | Sequence[VerifiedArchive],
) -> dict[str, Any]:
    if event.kind == "execution_failure":
        return render_failure_card(event)
    if event.kind == "missing_archive":
        return render_missing_archive_card(event)
    inventory = _normalize_report_inventory(archives)
    matching = [
        archive
        for archive in inventory.archives
        if archive.event_id == event.event_id
        and archive.trade_date == event.trade_date
        and archive.report_sha256 == event.report_sha256
    ]
    if len(matching) != 1:
        raise ReportDiscoveryError()
    current = matching[0]
    previous = current.previous
    return render_report_card(
        current.to_card_data(event.event_id),
        previous.to_card_data(previous.event_id) if previous else None,
    )


def _run_payload(
    *,
    ok: bool,
    discovered: int,
    delivered: int,
    pending: int,
    result: str | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "ok": ok,
        "mode": "run",
        "discovered": discovered,
        "delivered": delivered,
        "pending": pending,
    }
    if result is not None:
        payload["result"] = result
    return payload


def _pending_count(state: NotificationState) -> int:
    return sum(
        record.delivered_at is None for record in state.deliveries.values()
    )


def _validate_runtime_state(
    state: object, now: datetime
) -> NotificationState:
    if not isinstance(state, NotificationState):
        raise NotificationStateError("notification state unavailable")
    cutoff = now - timedelta(days=90)
    try:
        for record in state.deliveries.values():
            if (
                type(record.attempt_count) is not int
                or not 0 <= record.attempt_count < MAX_ATTEMPT_COUNT
                or not _is_aware(record.next_attempt_at)
                or (
                    record.delivered_at is not None
                    and not _is_aware(record.delivered_at)
                )
            ):
                raise ValueError("invalid delivery state")
            record.next_attempt_at <= now
            if record.delivered_at is not None:
                record.delivered_at >= cutoff
    except Exception:
        raise NotificationStateError(
            "notification state unavailable"
        ) from None
    return state


def _execution_delivery_may_be_rediscovered(
    state: NotificationState, record: DeliveryRecord
) -> bool:
    event = record.event
    try:
        if event.job_id is None or event.execution_id is None:
            return True
        observed = state.seen_execution_ids[event.job_id]
        cursor = state.execution_cursors[event.job_id]
        if (
            cursor is None
            or len(observed) != len(set(observed))
            or observed.count(cursor) != 1
            or observed.count(event.execution_id) != 1
        ):
            return True
        return observed.index(event.execution_id) < observed.index(cursor)
    except (KeyError, TypeError, ValueError):
        return True


def prune_notifier_deliveries(
    state: NotificationState, now: datetime
) -> NotificationState:
    """Prune only delivery metadata proven unable to replay."""

    checked_now = _require_aware_now(now)
    cutoff = checked_now - timedelta(days=90)
    deliveries: dict[str, DeliveryRecord] = {}
    for event_id, record in state.deliveries.items():
        recent_or_pending = (
            record.delivered_at is None or record.delivered_at >= cutoff
        )
        execution_event_may_recur = (
            record.event.kind in {"execution_failure", "missing_archive"}
            and _execution_delivery_may_be_rediscovered(state, record)
        )
        if recent_or_pending or execution_event_may_recur:
            deliveries[event_id] = record
    return state.model_copy(update={"deliveries": deliveries})


def run_notifier_once(
    store: object,
    config: object,
    client: _CardClient,
    execution_loader: Callable[[str], Sequence[CronExecution]],
    archive_loader: Callable[[], object],
    now: datetime,
) -> tuple[int, dict[str, object]]:
    """Discover and attempt due notifications under one nonblocking lock."""

    try:
        checked_now = _require_aware_now(now)
    except ValueError:
        return 1, _run_payload(
            ok=False,
            discovered=0,
            delivered=0,
            pending=0,
            result="invalid_time",
        )

    discovered_count = 0
    delivered_count = 0
    last_state: NotificationState | None = None
    exception_source = "state"
    lock_context: object

    def state_error_result() -> tuple[int, dict[str, object]]:
        return 1, _run_payload(
            ok=False,
            discovered=discovered_count,
            delivered=delivered_count,
            pending=_pending_count(last_state) if last_state else 0,
            result="state_error",
        )

    def run_locked_body() -> tuple[int, dict[str, object]]:
        nonlocal discovered_count
        nonlocal delivered_count
        nonlocal exception_source
        nonlocal last_state

        exception_source = "state"
        state = store.load_optional()
        if state is None:
            return 1, _run_payload(
                ok=False,
                discovered=0,
                delivered=0,
                pending=0,
                result="uninitialized",
            )
        state = _validate_runtime_state(state, checked_now)
        last_state = state
        exception_source = "discovery"
        try:
            discovered_state, events, inventory = discover_all_events(
                state,
                config,
                execution_loader,
                archive_loader,
            )
        except (ExecutionDiscoveryError, ReportDiscoveryError):
            return 1, _run_payload(
                ok=False,
                discovered=0,
                delivered=0,
                pending=_pending_count(state),
                result="discovery_error",
            )
        discovered_count = len(events)
        state = add_pending_events(discovered_state, events, checked_now)
        exception_source = "state"
        store.save(state)
        last_state = state

        delivery_failed = False
        exception_source = "delivery"
        for event_id in due_event_ids(state, checked_now):
            state = begin_attempt(state, event_id, checked_now)
            exception_source = "state"
            store.save(state)
            last_state = state
            record = state.deliveries[event_id]
            exception_source = "render"
            try:
                card = render_persisted_event(record.event, inventory)
            except ReportDiscoveryError:
                exception_source = "delivery"
                state = _record_failure_result(
                    state,
                    event_id,
                    checked_now,
                    "report_unavailable",
                )
                exception_source = "state"
                store.save(state)
                last_state = state
                delivery_failed = True
                exception_source = "delivery"
                continue
            exception_source = "client"
            try:
                client.send(card)
            except FeishuDeliveryError as error:
                exception_source = "delivery"
                state = record_delivery_failure(
                    state, event_id, checked_now, error
                )
                exception_source = "state"
                store.save(state)
                last_state = state
                delivery_failed = True
                exception_source = "delivery"
                continue
            exception_source = "delivery"
            state = record_delivery_success(state, event_id, checked_now)
            exception_source = "state"
            store.save(state)
            last_state = state
            delivered_count += 1
            exception_source = "delivery"

        state = prune_notifier_deliveries(state, checked_now)
        exception_source = "state"
        store.save(state)
        last_state = state
        code = 1 if delivery_failed else 0
        return code, _run_payload(
            ok=not delivery_failed,
            discovered=discovered_count,
            delivered=delivered_count,
            pending=_pending_count(state),
        )

    def release_preserving(error: BaseException) -> None:
        try:
            lock_context.__exit__(type(error), error, error.__traceback__)
        except BaseException:
            # The active body exception remains authoritative over cleanup.
            pass

    try:
        lock_context = store.lock()
        lock_context.__enter__()
    except NotificationAlreadyRunning:
        return 0, _run_payload(
            ok=True,
            discovered=0,
            delivered=0,
            pending=0,
            result="already_running",
        )
    except NotificationStateError:
        return state_error_result()

    try:
        result = run_locked_body()
    except NotificationStateError as error:
        release_preserving(error)
        if exception_source != "state":
            raise
        return state_error_result()
    except BaseException as error:
        release_preserving(error)
        raise

    try:
        lock_context.__exit__(None, None, None)
    except (NotificationAlreadyRunning, NotificationStateError):
        return state_error_result()
    return result


def send_test_card(
    config: object, client: _CardClient, now: datetime
) -> dict[str, object]:
    """Send one isolated configuration acceptance card."""

    del config
    checked_now = _require_aware_now(now)
    utc_now = checked_now.astimezone(timezone.utc)
    event_id = f"test:{utc_now.isoformat()}"
    client.send(render_test_card(event_id, checked_now))
    return {"ok": True, "mode": "test", "event_id": event_id}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _invalid_request(mode: str) -> dict[str, object]:
    return {
        "ok": False,
        "mode": mode,
        "error": {
            "code": "INVALID_NOTIFY_REQUEST",
            "message": "The Feishu notifier request is invalid.",
            "suggested_action": (
                "Use initialize, run, or explicitly confirmed test mode."
            ),
        },
    }


def _cli_failure(mode: str) -> dict[str, object]:
    return {
        "ok": False,
        "mode": mode,
        "error": {
            "code": "FEISHU_NOTIFIER_FAILED",
            "message": "The Feishu notifier could not complete.",
            "suggested_action": (
                "Inspect the safe notifier Cron result and private configuration."
            ),
        },
    }


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _safe_mode(argv: object) -> str:
    if (
        type(argv) is tuple
        and argv
        and type(argv[0]) is str
        and argv[0] in {"initialize", "run", "test"}
    ):
        return argv[0]
    return "unknown"


def _normalize_argv(argv: Sequence[str] | None) -> tuple[str, ...] | None:
    try:
        source = sys.argv[1:] if argv is None else argv
        tokens = tuple(source)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return None
    return tokens if all(type(token) is str for token in tokens) else None


def _serialize_payload(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, allow_nan=False)


def _write_line(line: str) -> bool:
    output = line + "\n"
    try:
        written = sys.stdout.write(output)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return False
    return type(written) is int and written == len(output)


def _emit_failure(mode: str) -> int:
    try:
        line = _serialize_payload(_cli_failure(mode))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return 1
    _write_line(line)
    return 1


def _emit_payload(payload: dict[str, object], mode: str, code: int) -> int:
    try:
        line = _serialize_payload(payload)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _emit_failure(mode)
    return code if _write_line(line) else 1


def _initialize_cli_result(
    result: object,
) -> tuple[int, dict[str, object]]:
    if (
        type(result) is not tuple
        or len(result) != 2
        or type(result[0]) is not NotificationState
        or type(result[1]) is not dict
    ):
        raise ValueError("invalid notifier result")
    return 0, result[1]


def _run_cli_result(result: object) -> tuple[int, dict[str, object]]:
    if (
        type(result) is not tuple
        or len(result) != 2
        or type(result[0]) is not int
        or type(result[1]) is not dict
    ):
        raise ValueError("invalid notifier result")
    return result[0], result[1]


def _runtime_loaders() -> tuple[
    Callable[[str], Sequence[CronExecution]], Callable[[], object]
]:
    return (
        lambda job_id: load_cron_runs(job_id, hermes_cli=HERMES_CLI),
        lambda: _load_report_inventory(RESULTS_ROOT),
    )


def main(
    argv: Sequence[str] | None = None, *, config: FeishuNotifierConfig
) -> int:
    """Run one explicitly selected Feishu notifier mode."""

    tokens = _normalize_argv(argv)
    mode = _safe_mode(tokens)
    try:
        valid_requests = {
            ("initialize",),
            ("run",),
            ("test", "--confirm-external-send"),
        }
        if tokens not in valid_requests:
            raise ValueError("invalid request")
        parser = _SafeArgumentParser(add_help=False)
        parser.add_argument("mode", choices=("initialize", "run", "test"))
        parser.add_argument("--confirm-external-send", action="store_true")
        arguments = parser.parse_args(tokens)
        expected_tokens = (
            ("test", "--confirm-external-send")
            if arguments.mode == "test"
            else (arguments.mode,)
        )
        if tokens != expected_tokens:
            raise ValueError("invalid confirmation")
    except (KeyboardInterrupt, SystemExit):
        raise
    except (TypeError, ValueError):
        return _emit_payload(_invalid_request(mode), mode, 1)

    try:
        now = _utc_now()
        if arguments.mode == "initialize":
            store = NotificationStateStore(STATE_ROOT)
            execution_loader, archive_loader = _runtime_loaders()
            result = initialize_notifier(
                store, config, execution_loader, archive_loader, now
            )
            code, payload = _initialize_cli_result(result)
        elif arguments.mode == "run":
            store = NotificationStateStore(STATE_ROOT)
            client = FeishuClient(config)
            execution_loader, archive_loader = _runtime_loaders()
            result = run_notifier_once(
                store, config, client, execution_loader, archive_loader, now
            )
            code, payload = _run_cli_result(result)
        else:
            client = FeishuClient(config)
            code, payload = 0, send_test_card(config, client, now)
        if (
            type(code) is not int
            or code not in {0, 1}
            or type(payload) is not dict
        ):
            raise ValueError("invalid notifier result")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _emit_failure(arguments.mode)

    return _emit_payload(payload, arguments.mode, code)
