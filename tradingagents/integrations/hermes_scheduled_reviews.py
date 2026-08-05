"""Durable schedules for automated Hermes paper-decision reviews."""

import json
import os
import tempfile
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import fcntl
from pydantic import ValidationError

from tradingagents.integrations.hermes_learning import make_review_id
from tradingagents.integrations.schemas import (
    DailyReportBatch,
    ScheduledReviewItem,
    ScheduledReviewPlan,
    utc_now,
)


HORIZON_DAYS = (1, 7, 15)


class ScheduledReviewStorageError(RuntimeError):
    """Raised when a scheduled-review plan cannot be persisted safely."""


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
