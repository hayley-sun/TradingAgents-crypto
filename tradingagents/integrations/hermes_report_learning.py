"""Durable report-level facts aggregated from paper-decision reviews."""

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import fcntl
from pydantic import ValidationError

from tradingagents.integrations.hermes_learning import extract_paper_action
from tradingagents.integrations.schemas import (
    AnalysisSession,
    PaperDecisionReview,
    ReportLearningOutcome,
    ReportLearningRecord,
    ReportLearningRevision,
    ReportSourceMetadata,
    is_valid_session_id,
    utc_now,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_MEMORY_MARKER = "[TradingAgents paper report: {session_id}]"
MAX_REPORT_REVISIONS = 3
_ALLOWED_HORIZONS = (1, 7, 15)
_SOURCE_FIELD_NAMES = (
    "market_report",
    "sentiment_report",
    "news_report",
    "fundamentals_report",
    "investment_plan",
    "trader_investment_plan",
    "final_trade_decision",
    "processed_signal",
)


class ReportLearningError(RuntimeError):
    """Raised when report-level learning facts cannot be persisted safely."""


class ReportLearningConflict(ReportLearningError):
    """Raised when incoming facts conflict with a persisted report identity."""


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


class ReportLearningStore:
    """Filesystem-backed report facts keyed by opaque analysis session ID."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    @classmethod
    def from_environment(cls) -> "ReportLearningStore":
        configured = os.getenv("TRADINGAGENTS_RESULTS_DIR")
        results_root = Path(configured) if configured else PROJECT_ROOT / "results"
        return cls(results_root / "hermes" / "report_memories")

    def path_for(self, session_id: str) -> Path:
        if not is_valid_session_id(session_id):
            raise ValueError("invalid session id")
        return self.root / f"{session_id}.json"

    def load(self, session_id: str) -> ReportLearningRecord | None:
        path = self.path_for(session_id)
        if not path.exists():
            return None
        try:
            with path.open(encoding="ascii") as record_file:
                return ReportLearningRecord.model_validate(json.load(record_file))
        except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise ReportLearningError("report learning record unavailable") from error

    def records(self) -> list[ReportLearningRecord]:
        if not self.root.exists():
            return []
        try:
            session_ids = sorted(
                path.stem
                for path in self.root.glob("hermes_*.json")
                if path.is_file()
            )
            return [
                record
                for session_id in session_ids
                if (record := self.load(session_id)) is not None
            ]
        except OSError as error:
            raise ReportLearningError("report learning records unavailable") from error

    def _save_unlocked(self, record: ReportLearningRecord) -> None:
        try:
            _atomic_json_write(
                self.path_for(record.session_id), record.model_dump(mode="json")
            )
        except OSError as error:
            raise ReportLearningError("report learning record unavailable") from error

    def save(self, record: ReportLearningRecord) -> None:
        with self.locked():
            self._save_unlocked(record)

    @contextmanager
    def locked(self) -> Iterator[None]:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / ".report-learning.lock").open(
                "a", encoding="ascii"
            ) as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise ReportLearningError("report learning store unavailable") from error

    def update(
        self,
        session_id: str,
        updater: Callable[
            [ReportLearningRecord | None], ReportLearningRecord
        ],
    ) -> ReportLearningRecord:
        """Apply one locked read-modify-write operation for a report record."""
        with self.locked():
            current = self.load(session_id)
            updated = updater(current)
            if updated.session_id != session_id:
                raise ValueError("report learning update changed session id")
            if updated == current:
                return updated
            self._save_unlocked(updated)
            return updated


def _source_values(session: AnalysisSession) -> dict[str, str]:
    if session.result is None:
        raise ValueError("session is not completed")
    result = session.result
    return {
        "market_report": result.reports.get("market", ""),
        "sentiment_report": result.reports.get("sentiment", ""),
        "news_report": result.reports.get("news", ""),
        "fundamentals_report": result.reports.get("fundamentals", ""),
        "investment_plan": result.investment_plan,
        "trader_investment_plan": result.trader_investment_plan,
        "final_trade_decision": result.final_trade_decision,
        "processed_signal": result.processed_signal,
    }


def _source_metadata(source_values: dict[str, str]) -> list[ReportSourceMetadata]:
    return [
        ReportSourceMetadata(
            name=name,
            sha256=hashlib.sha256(source_values[name].encode("utf-8")).hexdigest(),
            truncated=False,
        )
        for name in _SOURCE_FIELD_NAMES
    ]


def _source_digest(source_values: dict[str, str]) -> str:
    canonical = json.dumps(
        source_values,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _validate_review_identity(
    session: AnalysisSession, review: PaperDecisionReview
) -> None:
    if session.status != "completed" or session.result is None:
        raise ValueError("session is not completed")
    expected_identity = (
        session.session_id,
        session.request.symbol,
        session.request.trade_date,
        extract_paper_action(session),
    )
    review_identity = (
        review.session_id,
        review.symbol,
        review.trade_date,
        review.action,
    )
    if review_identity != expected_identity:
        raise ReportLearningConflict("review identity conflicts with report")
    if review.horizon_days not in _ALLOWED_HORIZONS:
        raise ValueError("review horizon is not supported")


def _validate_review_dates(review: PaperDecisionReview) -> None:
    expected_review_date = review.trade_date + timedelta(days=review.horizon_days)
    if (
        review.review_date != expected_review_date
        or review.entry_price.date != review.trade_date
        or review.review_price.date != review.review_date
    ):
        raise ValueError("review date does not match its horizon")


def _is_pristine_pending_revision(revision: ReportLearningRevision) -> bool:
    return (
        revision.reflection_state == "pending"
        and revision.memory_state == "blocked"
        and revision.reflection_attempt_count == 0
        and revision.last_error_code is None
        and revision.reflection is None
        and revision.lesson is None
        and revision.hermes_memory_entry is None
        and revision.verified_at is None
    )


def record_review_fact(
    store: ReportLearningStore,
    session: AnalysisSession,
    review: PaperDecisionReview,
) -> ReportLearningRecord:
    """Append one immutable paper-review fact to its report-level record."""
    _validate_review_identity(session, review)
    source_values = _source_values(session)
    source_digest = _source_digest(source_values)
    source_fields = _source_metadata(source_values)
    action = extract_paper_action(session)
    outcome = ReportLearningOutcome(
        review_id=review.review_id,
        horizon_days=review.horizon_days,
        review_date=review.review_date,
        raw_return_pct=review.raw_return_pct,
        verdict=review.verdict,
    )

    def aggregate(current: ReportLearningRecord | None) -> ReportLearningRecord:
        if current is not None:
            persisted_identity = (
                current.session_id,
                current.symbol,
                current.trade_date,
                current.action,
            )
            incoming_identity = (
                session.session_id,
                session.request.symbol,
                session.request.trade_date,
                action,
            )
            if persisted_identity != incoming_identity:
                raise ReportLearningConflict("report learning identity changed")
            if current.source_digest != source_digest:
                raise ReportLearningConflict("report learning source changed")
            existing_outcome = next(
                (
                    item
                    for item in current.outcomes
                    if item.review_id == review.review_id
                ),
                None,
            )
            if existing_outcome is not None:
                if existing_outcome != outcome:
                    raise ReportLearningConflict(
                        "report learning review outcome changed"
                    )
                return current
            if len(current.outcomes) >= MAX_REPORT_REVISIONS:
                raise ReportLearningConflict("report learning outcome limit reached")
            if any(
                outcome.horizon_days == review.horizon_days
                for outcome in current.outcomes
            ):
                raise ReportLearningConflict("report learning horizon already recorded")

        _validate_review_dates(review)
        outcomes = sorted(
            [*(current.outcomes if current is not None else []), outcome],
            key=lambda item: item.horizon_days,
        )
        now = utc_now()
        existing_revisions = current.revisions if current is not None else []
        revisions: list[ReportLearningRevision] = []
        for position in range(1, len(outcomes) + 1):
            outcome_review_ids = [
                item.review_id for item in outcomes[:position]
            ]
            existing = (
                existing_revisions[position - 1]
                if position <= len(existing_revisions)
                else None
            )
            if (
                existing is not None
                and existing.outcome_review_ids == outcome_review_ids
            ):
                revisions.append(existing)
                continue
            if existing is not None and not _is_pristine_pending_revision(existing):
                raise ReportLearningConflict(
                    "processed report learning revision cannot be rebuilt"
                )
            revisions.append(
                ReportLearningRevision(
                    revision=position,
                    outcome_review_ids=outcome_review_ids,
                    reflection_state="pending",
                    memory_state="blocked",
                    source_fields=[
                        field.model_copy(deep=True) for field in source_fields
                    ],
                    created_at=now,
                    updated_at=now,
                )
            )

        revision_number = len(outcomes)
        return ReportLearningRecord(
            session_id=session.session_id,
            symbol=session.request.symbol,
            trade_date=session.request.trade_date,
            action=action,
            source_digest=source_digest,
            desired_revision=revision_number,
            reflected_revision=(current.reflected_revision if current else 0),
            confirmed_revision=(current.confirmed_revision if current else 0),
            outcomes=outcomes,
            revisions=revisions,
            created_at=current.created_at if current is not None else now,
            updated_at=now,
        )

    return store.update(session.session_id, aggregate)
