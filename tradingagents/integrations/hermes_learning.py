"""Deterministic paper-decision review and per-symbol learning storage."""

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Callable
from datetime import date
from pathlib import Path

from tradingagents.integrations.schemas import (
    AnalysisSession,
    PaperDecisionReview,
    PriceReference,
    SymbolLearningEntry,
    SymbolLearningIndex,
    is_valid_review_id,
    utc_now,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_SYMBOL_LESSONS = 20
GRAPH_LESSON_LIMIT = 5
_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]{2,20}$")
_FINAL_ACTION_PATTERN = re.compile(
    r"FINAL\s+TRANSACTION\s+PROPOSAL\s*:\s*\**\s*(BUY|SELL|HOLD)\b",
    re.IGNORECASE,
)
_ACTION_PATTERN = re.compile(r"\b(BUY|SELL|HOLD)\b", re.IGNORECASE)


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
    """Filesystem-backed, bounded learning indexes isolated by symbol."""

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

    def upsert(self, review: PaperDecisionReview) -> SymbolLearningIndex:
        current = self.load(review.symbol)
        entry = SymbolLearningEntry(
            review_id=review.review_id,
            review_date=review.review_date,
            lesson=review.hermes_memory_entry,
        )
        existing_entries = current.entries if current is not None else []
        by_review_id = {item.review_id: item for item in existing_entries}
        by_review_id[entry.review_id] = entry
        entries = sorted(
            by_review_id.values(),
            key=lambda item: (item.review_date, item.review_id),
            reverse=True,
        )[:MAX_SYMBOL_LESSONS]
        index = SymbolLearningIndex(
            symbol=review.symbol,
            updated_at=utc_now(),
            entries=entries,
        )
        _atomic_json_write(self.path_for(index.symbol), index.model_dump(mode="json"))
        return index

    def lessons_for(self, symbol: str, limit: int = GRAPH_LESSON_LIMIT) -> list[str]:
        if limit < 1:
            return []
        index = self.load(symbol)
        if index is None:
            return []
        return [entry.lesson for entry in index.entries[:limit]]


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


def _valid_price(value: float) -> float:
    price = float(value)
    if not math.isfinite(price) or price <= 0:
        raise ValueError("USD reference price is unavailable")
    return price


def _memory_entry(
    symbol: str,
    trade_date: date,
    review_date: date,
    action: str,
    raw_return_pct: float,
    verdict: str,
) -> str:
    return (
        f"Paper-trading research lesson for {symbol}: the {trade_date.isoformat()} "
        f"analysis proposed {action}; CoinGecko USD reference movement through "
        f"{review_date.isoformat()} was {raw_return_pct:+.2f}%, so the directional "
        f"verdict was {verdict}. This is research and paper trading only, never a real order."
    )


def review_completed_session(
    session: AnalysisSession,
    review_date: date,
    price_lookup: Callable[[str, date], float],
    review_store: ReviewStore,
    learning_store: LearningStore,
    current_date: date | None = None,
) -> PaperDecisionReview:
    """Create or retrieve one deterministic review for a completed analysis session."""
    if session.status != "completed" or session.result is None:
        raise ValueError("session is not completed")

    trade_date = session.request.trade_date
    today = current_date or date.today()
    if review_date <= trade_date or review_date > today:
        raise ValueError("review date is outside the allowed range")

    review_id = make_review_id(session.session_id, review_date)
    existing = review_store.load(review_id)
    if existing is not None:
        learning_store.upsert(existing)
        return existing

    entry_price = _valid_price(price_lookup(session.request.symbol, trade_date))
    observed_price = _valid_price(price_lookup(session.request.symbol, review_date))
    raw_return_pct = round(((observed_price - entry_price) / entry_price) * 100, 8)
    action = extract_paper_action(session)
    verdict = classify_direction(action, raw_return_pct)
    review = PaperDecisionReview(
        review_id=review_id,
        session_id=session.session_id,
        symbol=session.request.symbol,
        trade_date=trade_date,
        review_date=review_date,
        action=action,
        entry_price=PriceReference(
            date=trade_date,
            usd_price=entry_price,
            source="coingecko",
        ),
        review_price=PriceReference(
            date=review_date,
            usd_price=observed_price,
            source="coingecko",
        ),
        raw_return_pct=raw_return_pct,
        verdict=verdict,
        created_at=utc_now(),
        hermes_memory_entry=_memory_entry(
            session.request.symbol,
            trade_date,
            review_date,
            action,
            raw_return_pct,
            verdict,
        ),
    )
    review_store.save(review)
    learning_store.upsert(review)
    return review
