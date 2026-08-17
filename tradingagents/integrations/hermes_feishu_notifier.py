"""Fail-closed discovery of Hermes Cron execution events."""

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, TypeGuard

from tradingagents.integrations.hermes_feishu_state import (
    NotificationEvent,
    NotificationState,
)


ExecutionStatus = Literal[
    "claimed", "running", "completed", "failed", "unknown"
]
EXECUTION_STATUSES = frozenset(
    {"claimed", "running", "completed", "failed", "unknown"}
)
EXECUTION_ERROR_MESSAGE = "Hermes execution history unavailable"
NO_EXECUTIONS_LINE = "No cron execution attempts recorded."
DEFAULT_HERMES_CLI = Path("/home/ubuntu/.local/bin/hermes")
MAX_EXECUTION_ROWS = 500
RUN_LINE = re.compile(
    r"^(?P<id>[0-9a-f]{32})  "
    r"(?P<status>claimed|running|completed|failed|unknown)\s+"
    r"job=(?P<job_id>[0-9a-f]{12})  "
    r"source=(?P<source>[^\s]+)  (?P<claimed_at>[^\s]+)$"
)
RUN_HEADER_SHAPE = re.compile(r"(?:^|\s)job=\S+\s+source=\S+(?:\s|$)")
EXECUTION_ID = re.compile(r"^[0-9a-f]{32}$")
JOB_ID = re.compile(r"^[0-9a-f]{12}$")


class ExecutionDiscoveryError(RuntimeError):
    """Raised without carrying unsafe execution output or process details."""

    def __init__(self, *_ignored: object) -> None:
        super().__init__(EXECUTION_ERROR_MESSAGE)


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
            if RUN_HEADER_SHAPE.search(line) is not None:
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


def load_cron_runs(
    job_id: str,
    *,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    hermes_cli: Path = DEFAULT_HERMES_CLI,
) -> tuple[CronExecution, ...]:
    """Load the bounded Hermes execution window through an absolute CLI path."""

    if not _is_job_id(job_id):
        raise ExecutionDiscoveryError()
    cli_path = Path(hermes_cli)
    if not cli_path.is_absolute():
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
    result: subprocess.CompletedProcess[str] | None = None
    try:
        result = run_command(
            command,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        process_failed = True

    if process_failed or result is None or result.returncode != 0:
        raise ExecutionDiscoveryError()
    return parse_cron_runs(result.stdout, job_id)


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
    for row in new_rows_oldest_first:
        if row.status == "failed":
            failure_events.append(
                NotificationEvent(
                    event_id=f"failure:{job_id}:{row.execution_id}",
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
    if newest_first:
        execution_cursors[job_id] = newest_first[0].execution_id
    elif job_id not in execution_cursors:
        execution_cursors[job_id] = None
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
