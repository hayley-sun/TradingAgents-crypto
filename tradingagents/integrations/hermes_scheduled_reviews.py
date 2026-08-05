"""Durable schedules for automated Hermes paper-decision reviews."""

import json
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import fcntl
from pydantic import ValidationError

from tradingagents.integrations.hermes_learning import make_review_id
from tradingagents.integrations.schemas import (
    DailyReportBatch,
    PaperDecisionReview,
    ScheduledReviewItem,
    ScheduledReviewPlan,
    utc_now,
)


HORIZON_DAYS = (1, 7, 15)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ScheduledReviewStorageError(RuntimeError):
    """Raised when a scheduled-review plan cannot be persisted safely."""


class ScheduledReviewConfirmationError(RuntimeError):
    """Raised when scheduled review persistence is not consistent with memory."""


@dataclass(frozen=True)
class ScheduledReviewProcessReport:
    due_count: int
    reviewed_count: int
    retryable_count: int
    skipped_count: int


@dataclass(frozen=True)
class ScheduledMemoryWork:
    trade_date: date
    review_date: date
    symbol: str
    horizon_days: int
    review_id: str
    hermes_memory_entry: str


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


class ScheduledReviewStore:
    """Filesystem-backed, idempotent scheduled-review plans."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    @classmethod
    def from_environment(cls) -> "ScheduledReviewStore":
        configured = os.getenv("TRADINGAGENTS_RESULTS_DIR")
        results_root = Path(configured) if configured else PROJECT_ROOT / "results"
        return cls(results_root / "hermes" / "review_schedules")

    def path_for(self, trade_date: date) -> Path:
        if not isinstance(trade_date, date):
            raise ValueError("invalid trade date")
        return self.root / f"{trade_date.isoformat()}.json"

    def load(self, trade_date: date) -> ScheduledReviewPlan | None:
        path = self.path_for(trade_date)
        if not path.exists():
            return None
        try:
            with path.open(encoding="ascii") as plan_file:
                return ScheduledReviewPlan.model_validate(json.load(plan_file))
        except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise ScheduledReviewStorageError(
                "scheduled review plan unavailable"
            ) from error

    def save(self, plan: ScheduledReviewPlan) -> None:
        try:
            _atomic_json_write(
                self.path_for(plan.trade_date), plan.model_dump(mode="json")
            )
        except OSError as error:
            raise ScheduledReviewStorageError(
                "scheduled review plan unavailable"
            ) from error

    @contextmanager
    def _exclusive_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".scheduled-reviews.lock").open(
            "a", encoding="ascii"
        ) as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def processing_lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".scheduled-review-processing.lock").open(
            "a", encoding="ascii"
        ) as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def create_or_load(self, batch: DailyReportBatch) -> ScheduledReviewPlan:
        if batch.archive is None:
            raise ValueError("daily report batch is not archived")
        with self._exclusive_lock():
            existing = self.load(batch.request.trade_date)
            if existing is not None:
                if existing.batch_id != batch.batch_id:
                    raise ScheduledReviewStorageError(
                        "scheduled review plan conflicts with report batch"
                    )
                return existing

            batch_items = {item.symbol: item for item in batch.items}
            archive_items = {item.symbol: item for item in batch.archive.items}
            items: list[ScheduledReviewItem] = []
            for symbol in batch.request.symbols:
                batch_item = batch_items.get(symbol)
                archive_item = archive_items.get(symbol)
                session_id = batch_item.session_id if batch_item is not None else None
                completed = (
                    archive_item is not None
                    and archive_item.status == "completed"
                    and session_id is not None
                )
                skip_reason = None
                if not completed:
                    skip_reason = (
                        archive_item.error_code
                        if archive_item is not None and archive_item.error_code
                        else archive_item.status.upper()
                        if archive_item is not None
                        else "REPORT_ITEM_MISSING"
                    )
                for horizon_days in HORIZON_DAYS:
                    review_date = batch.request.trade_date + timedelta(
                        days=horizon_days
                    )
                    items.append(
                        ScheduledReviewItem(
                            symbol=symbol,
                            session_id=session_id,
                            horizon_days=horizon_days,
                            review_date=review_date,
                            review_id=(
                                make_review_id(session_id, review_date)
                                if session_id is not None
                                else None
                            ),
                            state="review_pending" if completed else "skipped",
                            skip_reason=skip_reason,
                            updated_at=utc_now(),
                        )
                    )
            plan = ScheduledReviewPlan(
                batch_id=batch.batch_id,
                trade_date=batch.request.trade_date,
                created_at=utc_now(),
                items=items,
            )
            self.save(plan)
            return plan

    def plans(self) -> list[ScheduledReviewPlan]:
        if not self.root.exists():
            return []
        plans = []
        try:
            paths = sorted(self.root.glob("*.json"))
            for path in paths:
                try:
                    trade_date = date.fromisoformat(path.stem)
                except ValueError:
                    continue
                plan = self.load(trade_date)
                if plan is not None:
                    plans.append(plan)
        except OSError as error:
            raise ScheduledReviewStorageError(
                "scheduled review plans unavailable"
            ) from error
        return plans

    def update_item(self, review_id: str, **updates: Any) -> ScheduledReviewItem:
        with self._exclusive_lock():
            for plan in self.plans():
                for index, item in enumerate(plan.items):
                    if item.review_id != review_id:
                        continue
                    updated_item = ScheduledReviewItem.model_validate(
                        {**item.model_dump(mode="python"), **updates}
                    )
                    items = list(plan.items)
                    items[index] = updated_item
                    self.save(plan.model_copy(update={"items": items}))
                    return updated_item
        raise ScheduledReviewStorageError("scheduled review item unavailable")

    def find_item(self, review_id: str) -> tuple[ScheduledReviewPlan, ScheduledReviewItem]:
        for plan in self.plans():
            for item in plan.items:
                if item.review_id == review_id:
                    return plan, item
        raise ScheduledReviewStorageError("scheduled review item unavailable")


_SAFE_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")
_SKIPPED_REVIEW_ERRORS = {
    "SESSION_NOT_FOUND",
    "SESSION_NOT_COMPLETED",
    "SESSION_UNREADABLE",
}


def _error_code(result: dict[str, Any]) -> str:
    error = result.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code) else "SCHEDULED_REVIEW_FAILED"


def _successful_review_matches(
    result: dict[str, Any], item: ScheduledReviewItem
) -> bool:
    try:
        review = result["data"]["review"]
        return (
            result.get("ok") is True
            and review["review_id"] == item.review_id
            and review["session_id"] == item.session_id
            and review["review_date"] == item.review_date.isoformat()
        )
    except (KeyError, TypeError):
        return False


def process_due_reviews(
    store: ScheduledReviewStore,
    current_utc_date: date,
    reviewer: Callable[[str, date], dict[str, Any]],
) -> ScheduledReviewProcessReport:
    """Process only review dates whose UTC calendar day has fully elapsed."""
    if not isinstance(current_utc_date, date):
        raise ValueError("invalid current UTC date")
    due_count = 0
    reviewed_count = 0
    retryable_count = 0
    skipped_count = 0
    with store.processing_lock():
        due_items = sorted(
            (
                item
                for plan in store.plans()
                for item in plan.items
                if item.state == "review_pending"
                and item.review_date < current_utc_date
            ),
            key=lambda item: (item.review_date, item.symbol, item.horizon_days),
        )
        for item in due_items:
            due_count += 1
            try:
                result = reviewer(item.session_id, item.review_date)
            except Exception:
                result = {
                    "ok": False,
                    "error": {"code": "SCHEDULED_REVIEW_FAILED"},
                }
            attempts = item.attempt_count + 1
            if _successful_review_matches(result, item):
                store.update_item(
                    item.review_id,
                    state="memory_pending",
                    attempt_count=attempts,
                    last_error_code=None,
                    updated_at=utc_now(),
                )
                reviewed_count += 1
                continue

            code = _error_code(result)
            if code in _SKIPPED_REVIEW_ERRORS:
                store.update_item(
                    item.review_id,
                    state="skipped",
                    attempt_count=attempts,
                    last_error_code=code,
                    skip_reason=code,
                    updated_at=utc_now(),
                )
                skipped_count += 1
            else:
                store.update_item(
                    item.review_id,
                    attempt_count=attempts,
                    last_error_code=code,
                    updated_at=utc_now(),
                )
                retryable_count += 1
    return ScheduledReviewProcessReport(
        due_count=due_count,
        reviewed_count=reviewed_count,
        retryable_count=retryable_count,
        skipped_count=skipped_count,
    )


def list_pending_memory(
    store: ScheduledReviewStore,
    review_loader: Callable[[str], PaperDecisionReview | None],
    limit: int = 18,
) -> list[ScheduledMemoryWork]:
    """Return bounded canonical lessons without reading or changing Hermes memory."""
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("invalid pending memory limit")
    candidates = sorted(
        (
            (plan, item)
            for plan in store.plans()
            for item in plan.items
            if item.state == "memory_pending"
        ),
        key=lambda pair: (
            pair[1].review_date,
            pair[0].trade_date,
            pair[1].symbol,
            pair[1].horizon_days,
        ),
    )[:limit]
    work = []
    for plan, item in candidates:
        try:
            review = review_loader(item.review_id)
        except Exception as error:
            raise ScheduledReviewStorageError(
                "scheduled review memory work unavailable"
            ) from error
        if (
            review is None
            or review.review_id != item.review_id
            or review.session_id != item.session_id
            or review.review_date != item.review_date
            or review.symbol != item.symbol
        ):
            raise ScheduledReviewStorageError(
                "scheduled review memory work unavailable"
            )
        work.append(
            ScheduledMemoryWork(
                trade_date=plan.trade_date,
                review_date=item.review_date,
                symbol=item.symbol,
                horizon_days=item.horizon_days,
                review_id=item.review_id,
                hermes_memory_entry=review.hermes_memory_entry,
            )
        )
    return work


def confirm_scheduled_memory(
    store: ScheduledReviewStore,
    review_id: str,
    verifier: Callable[[str], Any],
) -> ScheduledReviewItem:
    """Confirm memory consistency and update only the project-owned schedule."""
    _, item = store.find_item(review_id)
    if item.state == "completed":
        return item
    if item.state != "memory_pending":
        raise ScheduledReviewConfirmationError(
            "scheduled review confirmation failed"
        )
    try:
        verification = verifier(review_id)
        if verification is None or verification is False:
            raise ValueError("review verification failed")
    except Exception as error:
        store.update_item(
            review_id,
            state="attention_required",
            last_error_code="REVIEW_CONSISTENCY_FAILED",
            updated_at=utc_now(),
        )
        raise ScheduledReviewConfirmationError(
            "scheduled review confirmation failed"
        ) from error
    now = utc_now()
    return store.update_item(
        review_id,
        state="completed",
        last_error_code=None,
        updated_at=now,
        verified_at=now,
    )
