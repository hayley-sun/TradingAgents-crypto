"""Deterministic paper-decision review and per-symbol learning storage."""

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import fcntl
from pydantic import ValidationError

from tradingagents.integrations.schemas import (
    AnalysisSession,
    PaperDecisionReview,
    PriceReference,
    ReportLearningIndexEntry,
    ReportLearningRecord,
    SymbolLearningEntry,
    SymbolLearningIndex,
    is_valid_review_id,
    utc_now,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_LESSON_LIMIT = 5
RECENT_REPORT_LIMIT = 3
MATURE_REPORT_LIMIT = 2
GRAPH_LESSON_TOTAL_MAX_CHARS = 12000
GRAPH_LESSON_LIMIT = REPORT_LESSON_LIMIT
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,20}$")
_FINAL_ACTION_PATTERN = re.compile(
    r"FINAL\s+TRANSACTION\s+PROPOSAL\s*:\s*\**\s*(BUY|SELL|HOLD)\b",
    re.IGNORECASE,
)
_ACTION_PATTERN = re.compile(r"\b(BUY|SELL|HOLD)\b", re.IGNORECASE)


class ReviewStorageError(RuntimeError):
    """Raised when the canonical review record cannot be read or written."""


class LearningStorageError(RuntimeError):
    """Raised when the derived per-symbol learning index cannot be updated."""


def _results_dir() -> Path:
    configured = os.getenv("TRADINGAGENTS_RESULTS_DIR")
    return Path(configured) if configured else PROJECT_ROOT / "results"


def _normalize_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper() if isinstance(symbol, str) else ""
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError("invalid symbol")
    return normalized


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


class ReviewStore:
    """Filesystem-backed storage for immutable, opaque paper-decision reviews."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    @classmethod
    def from_environment(cls) -> "ReviewStore":
        return cls(_results_dir() / "hermes" / "reviews")

    def path_for(self, review_id: str) -> Path:
        if not is_valid_review_id(review_id):
            raise ValueError("invalid review id")
        return self.root / f"{review_id}.json"

    def load(self, review_id: str) -> PaperDecisionReview | None:
        path = self.path_for(review_id)
        if not path.exists():
            return None
        with path.open(encoding="ascii") as review_file:
            return PaperDecisionReview.model_validate(json.load(review_file))

    def save(self, review: PaperDecisionReview) -> None:
        _atomic_json_write(self.path_for(review.review_id), review.model_dump(mode="json"))


class LearningStore:
    """Filesystem-backed, durable learning indexes isolated by symbol."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    @classmethod
    def from_environment(cls) -> "LearningStore":
        return cls(_results_dir() / "hermes" / "memories")

    def path_for(self, symbol: str) -> Path:
        return self.root / f"{_normalize_symbol(symbol)}.json"

    def load(self, symbol: str) -> SymbolLearningIndex | None:
        path = self.path_for(symbol)
        if not path.exists():
            return None
        with path.open(encoding="ascii") as learning_file:
            return SymbolLearningIndex.model_validate(json.load(learning_file))

    @contextmanager
    def _exclusive_write_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".learning.lock").open("a", encoding="ascii") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def upsert(self, review: PaperDecisionReview) -> SymbolLearningIndex:
        with self._exclusive_write_lock():
            current = self.load(review.symbol)
            entry = self._legacy_entry(review)
            existing_entries = (
                []
                if current is None
                else (
                    current.entries
                    if current.schema_version == 1
                    else current.legacy_entries
                )
            )
            by_review_id = {item.review_id: item for item in existing_entries}
            by_review_id[entry.review_id] = entry
            legacy_entries = sorted(
                by_review_id.values(),
                key=lambda item: (item.review_date, item.review_id),
                reverse=True,
            )
            index = (
                SymbolLearningIndex(
                    symbol=review.symbol,
                    updated_at=utc_now(),
                    entries=legacy_entries,
                )
                if current is None or current.schema_version == 1
                else SymbolLearningIndex(
                    schema_version=2,
                    symbol=review.symbol,
                    updated_at=utc_now(),
                    entries=[],
                    report_entries=current.report_entries,
                    legacy_entries=legacy_entries,
                )
            )
            _atomic_json_write(
                self.path_for(index.symbol), index.model_dump(mode="json")
            )
            return index

    @staticmethod
    def _legacy_entry(review: PaperDecisionReview) -> SymbolLearningEntry:
        return SymbolLearningEntry(
            review_id=review.review_id,
            review_date=review.review_date,
            lesson=review.hermes_memory_entry,
            session_id=review.session_id,
        )

    def upsert_report(self, record: ReportLearningRecord) -> SymbolLearningIndex:
        reflected_revision = record.reflected_revision
        if reflected_revision < 1 or reflected_revision > len(record.revisions):
            raise ValueError("report has no reflected revision")
        snapshot = record.revisions[reflected_revision - 1]
        if snapshot.revision != reflected_revision:
            raise ValueError("reflected snapshot is out of range")
        if snapshot.reflection_state != "ready":
            raise ValueError("reflected snapshot is not ready")
        if snapshot.lesson is None:
            raise ValueError("reflected snapshot has no lesson")

        horizons_by_review_id = {
            outcome.review_id: outcome.horizon_days for outcome in record.outcomes
        }
        try:
            maturity_days = max(
                horizons_by_review_id[review_id]
                for review_id in snapshot.outcome_review_ids
            )
        except (KeyError, ValueError) as error:
            raise ValueError("reflected snapshot outcomes are invalid") from error

        entry = ReportLearningIndexEntry(
            session_id=record.session_id,
            trade_date=record.trade_date,
            maturity_days=maturity_days,
            reflected_revision=reflected_revision,
            updated_at=snapshot.updated_at,
            lesson=snapshot.lesson,
        )
        with self._exclusive_write_lock():
            current = self.load(record.symbol)
            existing_entry = (
                next(
                    (
                        item
                        for item in current.report_entries
                        if item.session_id == entry.session_id
                    ),
                    None,
                )
                if current is not None and current.schema_version == 2
                else None
            )
            if existing_entry is not None:
                if entry.reflected_revision < existing_entry.reflected_revision:
                    return current
                if entry.reflected_revision == existing_entry.reflected_revision:
                    existing_content = (
                        existing_entry.trade_date,
                        existing_entry.maturity_days,
                        existing_entry.lesson,
                    )
                    incoming_content = (
                        entry.trade_date,
                        entry.maturity_days,
                        entry.lesson,
                    )
                    if incoming_content != existing_content:
                        raise LearningStorageError(
                            "report learning index conflicts"
                        )
                    return current
            legacy_entries = (
                []
                if current is None
                else (
                    current.entries
                    if current.schema_version == 1
                    else current.legacy_entries
                )
            )
            report_entries = (
                []
                if current is None or current.schema_version == 1
                else current.report_entries
            )
            by_session_id = {item.session_id: item for item in report_entries}
            by_session_id[entry.session_id] = entry
            sorted_reports = sorted(
                by_session_id.values(),
                key=lambda item: (item.trade_date, item.session_id),
                reverse=True,
            )
            index = SymbolLearningIndex(
                schema_version=2,
                symbol=record.symbol,
                updated_at=utc_now(),
                entries=[],
                report_entries=sorted_reports,
                legacy_entries=legacy_entries,
            )
            _atomic_json_write(
                self.path_for(index.symbol), index.model_dump(mode="json")
            )
            return index

    @staticmethod
    def _legacy_lessons(
        entries: list[SymbolLearningEntry], excluded_session_ids: set[str] | None = None
    ) -> list[str]:
        lessons = []
        seen_session_ids = set(excluded_session_ids or ())
        included_unknown_session = False
        for entry in entries:
            if entry.session_id is None:
                if included_unknown_session:
                    continue
                included_unknown_session = True
            elif entry.session_id in seen_session_ids:
                continue
            else:
                seen_session_ids.add(entry.session_id)
            lessons.append(entry.lesson)
        return lessons

    def lessons_for(self, symbol: str, limit: int = REPORT_LESSON_LIMIT) -> list[str]:
        if limit < 1:
            return []
        index = self.load(symbol)
        if index is None:
            return []

        candidates: list[str] = []
        report_session_ids: set[str] = set()
        if index.schema_version == 2:
            reports = sorted(
                index.report_entries,
                key=lambda item: (item.trade_date, item.session_id),
                reverse=True,
            )
            selected_session_ids: set[str] = set()
            for report in reports[:RECENT_REPORT_LIMIT]:
                candidates.append(report.lesson)
                selected_session_ids.add(report.session_id)
            mature_reports = [
                report for report in reports if report.maturity_days == 15
            ][:MATURE_REPORT_LIMIT]
            for report in mature_reports:
                if report.session_id not in selected_session_ids:
                    candidates.append(report.lesson)
                    selected_session_ids.add(report.session_id)
            candidates.extend(
                report.lesson
                for report in reports
                if report.session_id not in selected_session_ids
            )
            report_session_ids = {report.session_id for report in reports}
            legacy_entries = index.legacy_entries
        else:
            legacy_entries = index.entries
        candidates.extend(
            self._legacy_lessons(legacy_entries, report_session_ids)
        )

        lessons = []
        total_chars = 0
        for lesson in candidates:
            if len(lessons) >= limit:
                break
            if total_chars + len(lesson) > GRAPH_LESSON_TOTAL_MAX_CHARS:
                break
            lessons.append(lesson)
            total_chars += len(lesson)
        return lessons


def make_review_id(session_id: str, review_date: date) -> str:
    digest = hashlib.sha256(
        f"{session_id}:{review_date.isoformat()}".encode("ascii")
    ).hexdigest()
    return f"review_{digest[:32]}"


def extract_paper_action(session: AnalysisSession) -> str:
    """Extract a deterministic BUY, SELL, HOLD, or UNPARSEABLE decision."""
    if session.result is None:
        return "UNPARSEABLE"

    final_matches = _FINAL_ACTION_PATTERN.findall(session.result.final_trade_decision)
    if final_matches:
        return final_matches[-1].upper()

    signal_matches = _ACTION_PATTERN.findall(session.result.processed_signal)
    if signal_matches:
        return signal_matches[-1].upper()
    return "UNPARSEABLE"


def classify_direction(action: str, raw_return_pct: float) -> str:
    if action in {"HOLD", "UNPARSEABLE"}:
        return "not_scored"
    if raw_return_pct == 0:
        return "flat"
    is_correct = (action == "BUY" and raw_return_pct > 0) or (
        action == "SELL" and raw_return_pct < 0
    )
    return "correct" if is_correct else "incorrect"


def _memory_entry(
    symbol: str,
    trade_date: date,
    review_date: date,
    horizon_days: int,
    action: str,
    raw_return_pct: float,
    verdict: str,
) -> str:
    return (
        f"Paper-trading research lesson for {symbol}: the {trade_date.isoformat()} "
        f"analysis proposed {action}; at T+{horizon_days}, USD reference movement through "
        f"{review_date.isoformat()} was {raw_return_pct:+.2f}%, so the directional "
        f"verdict was {verdict}. This is research and paper trading only, never a real order."
    )


def _paired_price_references(
    resolver: Callable[[str, date, date], tuple[PriceReference, PriceReference]],
    symbol: str,
    trade_date: date,
    review_date: date,
) -> tuple[PriceReference, PriceReference]:
    try:
        entry_price, review_price = resolver(symbol, trade_date, review_date)
    except (TypeError, ValueError, ValidationError) as error:
        raise ValueError("USD reference prices are unavailable") from error

    if (
        not isinstance(entry_price, PriceReference)
        or not isinstance(review_price, PriceReference)
        or entry_price.date != trade_date
        or review_price.date != review_date
        or entry_price.source != review_price.source
    ):
        raise ValueError("USD reference prices are unavailable")
    return entry_price, review_price


def review_completed_session(
    session: AnalysisSession,
    review_date: date,
    price_reference_resolver: Callable[
        [str, date, date], tuple[PriceReference, PriceReference]
    ],
    review_store: ReviewStore,
    learning_store: LearningStore,
    current_date: date | None = None,
) -> PaperDecisionReview:
    """Create or retrieve one deterministic review for a completed analysis session."""
    if session.status != "completed" or session.result is None:
        raise ValueError("session is not completed")

    trade_date = session.request.trade_date
    today = current_date or utc_now().date()
    if review_date <= trade_date or review_date > today:
        raise ValueError("review date is outside the allowed range")

    review_id = make_review_id(session.session_id, review_date)
    try:
        existing = review_store.load(review_id)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise ReviewStorageError("review storage is unavailable") from error
    if existing is not None:
        try:
            learning_store.upsert(existing)
        except (OSError, json.JSONDecodeError, ValidationError) as error:
            raise LearningStorageError("learning storage is unavailable") from error
        return existing

    entry_price, observed_price = _paired_price_references(
        price_reference_resolver,
        session.request.symbol,
        trade_date,
        review_date,
    )
    raw_return_pct = round(
        ((observed_price.usd_price - entry_price.usd_price) / entry_price.usd_price) * 100,
        8,
    )
    action = extract_paper_action(session)
    verdict = classify_direction(action, raw_return_pct)
    horizon_days = (review_date - trade_date).days
    review = PaperDecisionReview(
        review_id=review_id,
        session_id=session.session_id,
        symbol=session.request.symbol,
        trade_date=trade_date,
        review_date=review_date,
        horizon_days=horizon_days,
        action=action,
        entry_price=entry_price,
        review_price=observed_price,
        raw_return_pct=raw_return_pct,
        verdict=verdict,
        created_at=utc_now(),
        hermes_memory_entry=_memory_entry(
            session.request.symbol,
            trade_date,
            review_date,
            horizon_days,
            action,
            raw_return_pct,
            verdict,
        ),
    )
    try:
        review_store.save(review)
    except OSError as error:
        raise ReviewStorageError("review storage is unavailable") from error
    try:
        learning_store.upsert(review)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        raise LearningStorageError("learning storage is unavailable") from error
    return review
