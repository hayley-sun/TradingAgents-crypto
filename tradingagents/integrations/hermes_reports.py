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
    DailyReportArchive,
    DailyReportArchiveItem,
    DailyReportBatch,
    DailyReportBatchItem,
    DailyReportRequest,
    ToolError,
    utc_now,
)


PAPER_TRADING_DISCLAIMER = (
    "Research and paper-trading output only. Do not use this output to place real trades."
)


class ReportBatchStorageError(RuntimeError):
    """Raised when a daily-report batch cannot be read or written."""


class ReportBatchConflict(ValueError):
    """Raised when one trade date is requested with different settings."""


class ReportBatchActive(ValueError):
    """Raised when an archive is requested before all batch items are terminal."""


class ReportArchiveConflict(ValueError):
    """Raised when an immutable archive is retried with different content."""


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


@dataclass(frozen=True)
class PriorReportSnapshot:
    trade_date: date
    items: tuple[DailyReportArchiveItem, ...]


@dataclass(frozen=True)
class ReportArchiveResult:
    path: Path
    sha256: str
    state: str


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


def _atomic_text_write(destination: Path, value: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=destination.parent,
            prefix=f".{destination.stem}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            os.chmod(temporary_path, 0o600)
            temporary_file.write(value)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        os.chmod(destination, 0o600)
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


def _failed_session_error() -> ToolError:
    return ToolError(
        code="ANALYSIS_FAILED",
        message="A daily report analysis did not complete successfully.",
        suggested_action="Inspect the safe analysis status and create a later daily report batch.",
    )


class ReportBatchStore:
    """Filesystem-backed, idempotent daily report batches."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    @property
    def reports_root(self) -> Path:
        return self.root.parent / "reports"

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
                    error=session.error
                    if session.error is not None
                    else _failed_session_error()
                    if session.status == "failed"
                    else None,
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

    def archive(
        self,
        batch: DailyReportBatch,
        session_loader: Callable[[str], AnalysisSession | None],
        narrative: str,
        scheduled_review_version: int | None = None,
    ) -> ReportArchiveResult:
        if not isinstance(narrative, str) or not narrative.strip() or len(narrative) > 20000:
            raise ValueError("invalid report narrative")
        with self._exclusive_lock():
            persisted = self.load(batch.request.trade_date)
            if persisted is None or persisted.batch_id != batch.batch_id:
                raise ReportBatchStorageError("daily report batch unavailable")
            summary = self.summarize(persisted, session_loader)
            if summary.state == "active":
                raise ReportBatchActive("daily report batch is still active")

            archived_at = persisted.archive.archived_at if persisted.archive else utc_now()
            previous = self.previous_snapshot(persisted.request.trade_date)
            document = _render_report(summary, narrative.strip(), previous)
            digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
            destination = self.reports_root / f"{persisted.request.trade_date.isoformat()}.md"
            if persisted.archive is not None:
                if persisted.archive.sha256 != digest:
                    raise ReportArchiveConflict("daily report archive content conflicts")
                if not destination.is_file():
                    raise ReportBatchStorageError("daily report archive unavailable")
                return ReportArchiveResult(
                    path=destination,
                    sha256=persisted.archive.sha256,
                    state=persisted.archive.state,
                )

            if destination.exists():
                try:
                    existing_digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                except OSError as error:
                    raise ReportBatchStorageError("daily report archive unavailable") from error
                if existing_digest != digest:
                    raise ReportArchiveConflict("daily report archive content conflicts")
            else:
                try:
                    _atomic_text_write(destination, document)
                except OSError as error:
                    raise ReportBatchStorageError("daily report archive unavailable") from error
            archive = DailyReportArchive(
                filename=destination.name,
                sha256=digest,
                state=summary.state,
                archived_at=archived_at,
                items=_archive_items(summary),
                scheduled_review_version=scheduled_review_version,
            )
            self.save(persisted.model_copy(update={"archive": archive}))
            return ReportArchiveResult(path=destination, sha256=digest, state=summary.state)

    def previous_snapshot(self, trade_date: date) -> PriorReportSnapshot | None:
        candidates = []
        try:
            paths = self.root.glob("*.json") if self.root.exists() else ()
            for path in paths:
                try:
                    batch_date = date.fromisoformat(path.stem)
                except ValueError:
                    continue
                if batch_date >= trade_date:
                    continue
                batch = self.load(batch_date)
                if batch is not None and batch.archive is not None:
                    candidates.append(batch)
        except OSError as error:
            raise ReportBatchStorageError("daily report batch unavailable") from error
        if not candidates:
            return None
        latest = max(candidates, key=lambda candidate: candidate.request.trade_date)
        return PriorReportSnapshot(
            trade_date=latest.request.trade_date,
            items=tuple(latest.archive.items),
        )


def _archive_items(summary: ReportBatchSummary) -> list[DailyReportArchiveItem]:
    return [
        DailyReportArchiveItem(
            symbol=item.symbol,
            status=item.status,
            processed_signal=item.result.processed_signal if item.result else None,
            final_trade_decision=item.result.final_trade_decision if item.result else None,
            error_code=item.error.code if item.error else None,
        )
        for item in summary.items
    ]


def _render_report(
    summary: ReportBatchSummary,
    narrative: str,
    previous: PriorReportSnapshot | None,
) -> str:
    request = summary.batch.request
    lines = [
        "# TradingAgents Daily Crypto Research",
        "",
        "## Report Date",
        "",
        f"- Trade date: {request.trade_date.isoformat()}",
        f"- Batch state: {summary.state}",
        "",
        "## Batch Configuration",
        "",
        f"- Symbols: {', '.join(request.symbols)}",
        f"- Analysts: {', '.join(request.analysts)}",
        f"- Research depth: {request.research_depth}",
        f"- LLM provider: {request.llm_provider}",
        "",
        "## Per-Symbol Results",
        "",
    ]
    for item in summary.items:
        lines.extend((f"### {item.symbol}", f"- Status: {item.status}"))
        if item.result is not None:
            lines.extend(
                (
                    f"- Processed signal: {item.result.processed_signal}",
                    f"- Final decision: {item.result.final_trade_decision}",
                )
            )
        if item.error is not None:
            lines.append(f"- Error code: {item.error.code}")
        lines.append("")
    lines.extend(("## Narrative", "", narrative, "", "## Previous Report Comparison", ""))
    if previous is None:
        lines.append("- No previous archived report is available.")
    else:
        lines.append(f"- Previous trade date: {previous.trade_date.isoformat()}")
        for item in previous.items:
            lines.append(
                f"- {item.symbol}: {item.status}; signal={item.processed_signal or 'unavailable'}"
            )
    lines.extend(("", "## Research Boundary", "", PAPER_TRADING_DISCLAIMER, ""))
    return "\n".join(lines)
