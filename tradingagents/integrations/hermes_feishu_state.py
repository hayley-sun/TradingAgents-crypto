"""Durable delivery state for Hermes Feishu notifications."""

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator


EventKind = Literal["report", "execution_failure", "missing_archive"]

REQUIRED_FIELDS = {
    "report": {"trade_date", "report_sha256", "batch_state"},
    "execution_failure": {"job_name", "job_id", "execution_id"},
    "missing_archive": {
        "trade_date",
        "batch_state",
        "job_name",
        "job_id",
        "execution_id",
    },
}
RETRY_MINUTES = (5, 10, 20, 40, 60)
STATE_ERROR_MESSAGE = "notification state unavailable"
ALREADY_RUNNING_MESSAGE = "notification notifier already running"


class NotificationStateError(RuntimeError):
    """Raised when notification state cannot be read or written."""


class NotificationAlreadyRunning(RuntimeError):
    """Raised when another notifier process holds the state lock."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NotificationEvent(_FrozenModel):
    event_id: str
    kind: EventKind
    created_at: datetime
    trade_date: date | None = None
    report_sha256: str | None = None
    batch_state: str | None = None
    job_name: str | None = None
    job_id: str | None = None
    execution_id: str | None = None

    @model_validator(mode="after")
    def require_exact_kind_fields(self) -> Self:
        kind_fields = set().union(*REQUIRED_FIELDS.values())
        populated_fields = {
            field_name
            for field_name in kind_fields
            if getattr(self, field_name) is not None
        }
        if populated_fields != REQUIRED_FIELDS[self.kind]:
            raise ValueError("event fields must match event kind")
        return self


class DeliveryRecord(_FrozenModel):
    event: NotificationEvent
    attempt_count: int = 0
    next_attempt_at: datetime
    delivered_at: datetime | None = None
    last_result: str | None = None


class NotificationState(_FrozenModel):
    schema_version: Literal[1] = 1
    initialized_at: datetime
    execution_cursors: dict[str, str | None]
    seen_execution_ids: dict[str, list[str]]
    seen_report_event_ids: list[str]
    deliveries: dict[str, DeliveryRecord]


def retry_delay(attempt_count: int) -> timedelta:
    if attempt_count < 1:
        raise ValueError("attempt count must be positive")
    return timedelta(minutes=RETRY_MINUTES[min(attempt_count - 1, 4)])


def initialized_state(
    now: datetime,
    execution_ids: dict[str, list[str]],
    report_event_ids: list[str],
) -> NotificationState:
    return NotificationState(
        initialized_at=now,
        execution_cursors={
            job: ids[0] if ids else None for job, ids in execution_ids.items()
        },
        seen_execution_ids=execution_ids,
        seen_report_event_ids=sorted(set(report_event_ids)),
        deliveries={},
    )


def prune_delivered(
    state: NotificationState, now: datetime
) -> NotificationState:
    cutoff = now - timedelta(days=90)
    deliveries = {
        event_id: delivery
        for event_id, delivery in state.deliveries.items()
        if delivery.delivered_at is None or delivery.delivered_at >= cutoff
    }
    return state.model_copy(update={"deliveries": deliveries})


class NotificationStateStore:
    """Atomic, owner-only storage for notification delivery state."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    @property
    def path(self) -> Path:
        return self.root / "state.json"

    @property
    def lock_path(self) -> Path:
        return self.root / ".state.lock"

    def _prepare_root(self) -> None:
        self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)

    def load(self) -> NotificationState:
        state = self.load_optional()
        if state is None:
            raise NotificationStateError(STATE_ERROR_MESSAGE)
        return state

    def load_optional(self) -> NotificationState | None:
        try:
            with self.path.open(encoding="ascii") as state_file:
                return NotificationState.model_validate(json.load(state_file))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, ValidationError) as error:
            raise NotificationStateError(STATE_ERROR_MESSAGE) from error

    def save(self, state: NotificationState) -> None:
        temporary_path: Path | None = None
        try:
            self._prepare_root()
            payload = NotificationState.model_validate(
                state.model_dump(mode="json")
            ).model_dump(mode="json")
            with NamedTemporaryFile(
                mode="w",
                encoding="ascii",
                dir=self.root,
                prefix=".state.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                os.chmod(temporary_path, 0o600)
                json.dump(payload, temporary_file, ensure_ascii=True, indent=2)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
            os.chmod(self.path, 0o600)
        except (OSError, TypeError, ValueError, ValidationError) as error:
            raise NotificationStateError(STATE_ERROR_MESSAGE) from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    @contextmanager
    def lock(self) -> Iterator[None]:
        try:
            self._prepare_root()
            with self.lock_path.open("a", encoding="ascii") as lock_file:
                os.chmod(self.lock_path, 0o600)
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise NotificationAlreadyRunning(ALREADY_RUNNING_MESSAGE) from error
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except NotificationAlreadyRunning:
            raise
        except OSError as error:
            raise NotificationStateError(STATE_ERROR_MESSAGE) from error
