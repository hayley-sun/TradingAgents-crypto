"""Persistent daily-report batches for the Hermes crypto integration."""

import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from tradingagents.integrations.schemas import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisSession,
    DailyReportBatch,
    DailyReportBatchItem,
    DailyReportRequest,
    ToolError,
    utc_now,
)


class ReportBatchStorageError(RuntimeError):
    """Raised when a daily-report batch cannot be read or written."""


class ReportBatchConflict(ValueError):
    """Raised when one trade date is requested with different settings."""


@dataclass(frozen=True)
class ReportBatchItemSummary:
    symbol: str
    session_id: str | None
    status: str
    result: AnalysisResult | None
    error: ToolError | None


@dataclass(frozen=True)
class ReportBatchSummary:
    batch: DailyReportBatch
    state: str
    items: tuple[ReportBatchItemSummary, ...]


def _atomic_json_write(destination: Path, value: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="ascii",
            dir=destination.parent,
            prefix=f".{destination.stem}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(value, temporary_file, ensure_ascii=True, indent=2)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _make_batch_id(request: DailyReportRequest) -> str:
    payload = json.dumps(
        request.model_dump(mode="json"), ensure_ascii=True, sort_keys=True
    )
    digest = hashlib.sha256(payload.encode("ascii")).hexdigest()
    return f"report_{digest[:32]}"


def _submission_error() -> ToolError:
    return ToolError(
        code="REPORT_SUBMISSION_FAILED",
        message="The daily report analysis could not be submitted.",
        suggested_action="Inspect the safe analysis status and start a new batch on a later date.",
    )


def _unreadable_session_error() -> ToolError:
    return ToolError(
        code="SESSION_UNREADABLE",
        message="A daily report analysis session could not be read.",
        suggested_action="Inspect the persisted session and create a later daily report batch.",
    )


def _missing_session_error() -> ToolError:
    return ToolError(
        code="SESSION_NOT_FOUND",
        message="A daily report analysis session is missing.",
        suggested_action="Inspect the persisted batch and create a later daily report batch.",
    )


class ReportBatchStore:
    """Filesystem-backed, idempotent daily report batches."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    def path_for(self, trade_date: date) -> Path:
        if not isinstance(trade_date, date):
            raise ValueError("invalid trade date")
        return self.root / f"{trade_date.isoformat()}.json"

    def load(self, trade_date: date) -> DailyReportBatch | None:
        path = self.path_for(trade_date)
        if not path.exists():
            return None
        try:
            with path.open(encoding="ascii") as batch_file:
                return DailyReportBatch.model_validate(json.load(batch_file))
        except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise ReportBatchStorageError("daily report batch unavailable") from error

    def save(self, batch: DailyReportBatch) -> None:
        try:
            _atomic_json_write(
                self.path_for(batch.request.trade_date), batch.model_dump(mode="json")
            )
        except OSError as error:
            raise ReportBatchStorageError("daily report batch unavailable") from error

    @contextmanager
    def _exclusive_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".report-batches.lock").open("a", encoding="ascii") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def create_or_load(
        self,
        request: DailyReportRequest,
        starter: Callable[[AnalysisRequest], str | ToolError],
    ) -> DailyReportBatch:
        with self._exclusive_lock():
            existing = self.load(request.trade_date)
            if existing is not None:
                if existing.request != request:
                    raise ReportBatchConflict("daily report request conflicts with existing batch")
                return existing

            batch = DailyReportBatch(
                batch_id=_make_batch_id(request),
                request=request,
                created_at=utc_now(),
                items=[],
            )
            self.save(batch)
            for symbol in request.symbols:
                item = self._submit_item(request.for_symbol(symbol), starter)
                batch = batch.model_copy(update={"items": [*batch.items, item]})
                self.save(batch)
            return batch

    def _submit_item(
        self,
        request: AnalysisRequest,
        starter: Callable[[AnalysisRequest], str | ToolError],
    ) -> DailyReportBatchItem:
        try:
            submitted = starter(request)
        except Exception:
            return DailyReportBatchItem(
                symbol=request.symbol, submission_error=_submission_error()
            )
        if isinstance(submitted, ToolError):
            return DailyReportBatchItem(
                symbol=request.symbol, submission_error=submitted
            )
        try:
            return DailyReportBatchItem(symbol=request.symbol, session_id=submitted)
        except (TypeError, ValidationError, ValueError):
            return DailyReportBatchItem(
                symbol=request.symbol, submission_error=_submission_error()
            )

    def summarize(
        self,
        batch: DailyReportBatch,
        session_loader: Callable[[str], AnalysisSession | None],
    ) -> ReportBatchSummary:
        item_by_symbol = {item.symbol: item for item in batch.items}
        summaries = []
        has_active = False
        has_failure = False
        for symbol in batch.request.symbols:
            item = item_by_symbol.get(symbol)
            if item is None:
                has_failure = True
                summaries.append(
                    ReportBatchItemSummary(
                        symbol=symbol,
                        session_id=None,
                        status="submission_failed",
                        result=None,
                        error=_submission_error(),
                    )
                )
                continue
            if item.submission_error is not None:
                has_failure = True
                summaries.append(
                    ReportBatchItemSummary(
                        symbol=symbol,
                        session_id=None,
                        status="submission_failed",
                        result=None,
                        error=item.submission_error,
                    )
                )
                continue

            session, unreadable = self._load_session(item.session_id, session_loader)
            if session is None:
                has_failure = True
                summaries.append(
                    ReportBatchItemSummary(
                        symbol=symbol,
                        session_id=item.session_id,
                        status="unreadable" if unreadable else "missing",
                        result=None,
                        error=_unreadable_session_error()
                        if unreadable
                        else _missing_session_error(),
                    )
                )
                continue
            if session.status in {"queued", "running"}:
                has_active = True
            elif session.status == "failed":
                has_failure = True
            summaries.append(
                ReportBatchItemSummary(
                    symbol=symbol,
                    session_id=item.session_id,
                    status=session.status,
                    result=session.result,
                    error=session.error,
                )
            )

        state = "active" if has_active else "degraded" if has_failure else "ready"
        return ReportBatchSummary(batch=batch, state=state, items=tuple(summaries))

    @staticmethod
    def _load_session(
        session_id: str | None,
        session_loader: Callable[[str], AnalysisSession | None],
    ) -> tuple[AnalysisSession | None, bool]:
        if session_id is None:
            return None, False
        try:
            return session_loader(session_id), False
        except (OSError, ValueError, json.JSONDecodeError, ValidationError):
            return None, True
