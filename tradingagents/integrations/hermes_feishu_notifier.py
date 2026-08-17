"""Fail-closed discovery of Hermes Cron execution events."""

import hashlib
import os
import re
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Literal, TypeGuard
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from tradingagents.integrations.hermes_feishu_client import (
    ReportCardData,
    ReportCardItem,
)
from tradingagents.integrations.hermes_feishu_state import (
    NotificationEvent,
    NotificationState,
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


def _read_regular_bytes(path: Path, maximum_size: int) -> bytes:
    """Read a bounded regular file without following a leaf symlink."""

    descriptor: int | None = None
    try:
        initial_metadata = os.lstat(path)
        if not stat.S_ISREG(initial_metadata.st_mode):
            raise OSError("symlinked source")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
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


def _report_paths(results_root: Path) -> tuple[Path, Path]:
    root = Path(results_root)
    hermes_root = root / "hermes"
    if hermes_root.is_symlink():
        raise OSError("invalid Hermes directory")
    return (
        hermes_root / "report_batches",
        hermes_root / "reports",
    )


def _is_aware(value: object) -> bool:
    if not isinstance(value, datetime):
        return False
    try:
        return value.utcoffset() is not None
    except (OverflowError, ValueError):
        return False


def _load_report_batches(results_root: Path) -> tuple[DailyReportBatch, ...]:
    """Fully validate every persisted batch JSON before exposing any batch."""

    try:
        batches_root, _ = _report_paths(results_root)
        if batches_root.is_symlink():
            raise OSError("invalid batch directory")
        if not batches_root.exists():
            return ()
        if not batches_root.is_dir():
            raise OSError("invalid batch directory")
        paths = sorted(
            (path for path in batches_root.iterdir() if path.suffix == ".json"),
            key=lambda path: path.name,
        )
        batches: list[DailyReportBatch] = []
        seen_dates: set[date] = set()
        for path in paths:
            raw = _read_regular_bytes(path, MAX_BATCH_BYTES)
            text = raw.decode("utf-8")
            batch = DailyReportBatch.model_validate_json(text)
            expected_name = f"{batch.request.trade_date.isoformat()}.json"
            if path.name != expected_name or batch.request.trade_date in seen_dates:
                raise ValueError("invalid canonical batch")
            if not _is_aware(batch.created_at):
                raise ValueError("naive batch timestamp")
            seen_dates.add(batch.request.trade_date)
            batches.append(batch)
        return tuple(sorted(batches, key=lambda batch: batch.request.trade_date))
    except (
        OSError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
        TypeError,
    ):
        pass
    raise ReportDiscoveryError()


def load_verified_archives(results_root: Path) -> tuple[VerifiedArchive, ...]:
    """Load only complete, hash-verified daily report archive snapshots."""

    try:
        batches = _load_report_batches(results_root)
        _, reports_root = _report_paths(results_root)
        verified: list[VerifiedArchive] = []
        for batch in batches:
            archive = batch.archive
            if archive is None:
                continue
            expected_filename = f"{batch.request.trade_date.isoformat()}.md"
            if archive.filename != expected_filename or not _is_aware(
                archive.archived_at
            ):
                raise ValueError("invalid archive metadata")
            if reports_root.is_symlink() or not reports_root.is_dir():
                raise OSError("invalid reports directory")
            report_path = reports_root / expected_filename
            report_bytes = _read_regular_bytes(report_path, MAX_REPORT_BYTES)
            digest = hashlib.sha256(report_bytes).hexdigest()
            if digest != archive.sha256:
                raise ValueError("report digest mismatch")
            items = tuple(
                ReportCardItem(
                    symbol=item.symbol,
                    status=item.status,
                    processed_signal=item.processed_signal,
                    final_trade_decision=item.final_trade_decision,
                    error_code=item.error_code,
                )
                for item in archive.items
            )
            verified.append(
                VerifiedArchive(
                    trade_date=batch.request.trade_date,
                    batch_id=batch.batch_id,
                    report_sha256=archive.sha256,
                    state=archive.state,
                    items=items,
                    report_path=report_path,
                    archived_at=archive.archived_at,
                )
            )
        ordered = sorted(verified, key=lambda archive: archive.trade_date)
        snapshots: list[VerifiedArchive] = []
        previous: VerifiedArchive | None = None
        for archive in ordered:
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
    except ReportDiscoveryError as error:
        error.__context__ = None
        raise
    except (OSError, ValueError, TypeError):
        pass
    raise ReportDiscoveryError()


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
    try:
        batches = _load_report_batches(results_root)
        # Reloading verifies the currently persisted report bytes before alerts.
        current_archives = load_verified_archives(results_root)
    except ReportDiscoveryError:
        raise

    unarchived_dates = {
        batch.request.trade_date for batch in batches if batch.archive is None
    }
    archived_dates = {archive.trade_date for archive in current_archives}
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
            trade_date not in unarchived_dates
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
