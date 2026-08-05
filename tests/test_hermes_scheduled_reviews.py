import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from tradingagents.integrations.hermes_scheduled_reviews import (
    ScheduledReviewStore,
    process_due_reviews,
)
from tradingagents.integrations.schemas import (
    DailyReportArchive,
    DailyReportArchiveItem,
    DailyReportBatch,
    DailyReportBatchItem,
    DailyReportRequest,
    ScheduledReviewItem,
    utc_now,
)


SESSION_IDS = {
    "BTC": "hermes_0000000000000001",
    "ETH": "hermes_0000000000000002",
    "SOL": "hermes_0000000000000003",
}


def archived_batch(statuses: dict[str, str] | None = None) -> DailyReportBatch:
    item_statuses = statuses or {symbol: "completed" for symbol in SESSION_IDS}
    request = DailyReportRequest(
        trade_date="2026-08-05",
        symbols=list(SESSION_IDS),
        analysts=["market", "news", "fundamentals"],
        research_depth=1,
        llm_provider="deepseek",
        quick_model="deepseek-v4-flash",
        deep_model="deepseek-v4-pro",
    )
    return DailyReportBatch(
        batch_id="report_0123456789abcdef",
        request=request,
        created_at=utc_now(),
        items=[
            DailyReportBatchItem(symbol=symbol, session_id=session_id)
            for symbol, session_id in SESSION_IDS.items()
        ],
        archive=DailyReportArchive(
            filename="2026-08-05.md",
            sha256="0" * 64,
            state="degraded" if "failed" in item_statuses.values() else "ready",
            archived_at=utc_now(),
            items=[
                DailyReportArchiveItem(
                    symbol=symbol,
                    status=item_statuses[symbol],
                    error_code=(
                        "ANALYSIS_FAILED"
                        if item_statuses[symbol] == "failed"
                        else None
                    ),
                )
                for symbol in SESSION_IDS
            ],
        ),
    )


class HermesScheduledReviewTests(unittest.TestCase):
    def test_non_skipped_item_requires_session_and_review_ids(self):
        with self.assertRaises(ValidationError):
            ScheduledReviewItem(
                symbol="BTC",
                horizon_days=1,
                review_date="2026-08-06",
                state="review_pending",
                updated_at=utc_now(),
            )

    def test_ready_batch_creates_three_horizons_per_symbol(self):
        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")

            plan = store.create_or_load(archived_batch())
            loaded = store.load(date(2026, 8, 5))

        self.assertEqual(plan, loaded)
        self.assertEqual(len(plan.items), 9)
        self.assertEqual(
            [(item.symbol, item.horizon_days) for item in plan.items],
            [
                (symbol, horizon)
                for symbol in ("BTC", "ETH", "SOL")
                for horizon in (1, 7, 15)
            ],
        )
        self.assertEqual(
            [item.review_date for item in plan.items[:3]],
            [date(2026, 8, 6), date(2026, 8, 12), date(2026, 8, 20)],
        )
        self.assertTrue(all(item.state == "review_pending" for item in plan.items))

    def test_degraded_batch_creates_skipped_horizons_for_failed_symbol(self):
        statuses = {"BTC": "completed", "ETH": "failed", "SOL": "completed"}
        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")

            plan = store.create_or_load(archived_batch(statuses))

        eth_items = [item for item in plan.items if item.symbol == "ETH"]
        self.assertEqual([item.state for item in eth_items], ["skipped"] * 3)
        self.assertEqual(
            [item.skip_reason for item in eth_items], ["ANALYSIS_FAILED"] * 3
        )

    def test_review_date_must_be_fully_elapsed(self):
        calls = []

        def reviewer(session_id, review_date):
            calls.append((session_id, review_date))
            plan = store.load(date(2026, 8, 5))
            item = next(
                candidate
                for candidate in plan.items
                if candidate.session_id == session_id
                and candidate.review_date == review_date
            )
            return {
                "ok": True,
                "data": {
                    "review": {
                        "review_id": item.review_id,
                        "session_id": session_id,
                        "review_date": review_date.isoformat(),
                    }
                },
            }

        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            store.create_or_load(archived_batch())

            same_day = process_due_reviews(store, date(2026, 8, 6), reviewer)
            next_day = process_due_reviews(store, date(2026, 8, 7), reviewer)
            plan = store.load(date(2026, 8, 5))

        self.assertEqual(same_day.reviewed_count, 0)
        self.assertEqual(next_day.reviewed_count, 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [item.state for item in plan.items if item.horizon_days == 1],
            ["memory_pending"] * 3,
        )
        self.assertTrue(
            all(
                item.state == "review_pending"
                for item in plan.items
                if item.horizon_days != 1
            )
        )

    def test_price_failure_remains_retryable(self):
        def failing_reviewer(_session_id, _review_date):
            return {
                "ok": False,
                "error": {"code": "PRICE_DATA_UNAVAILABLE"},
            }

        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            store.create_or_load(archived_batch())

            report = process_due_reviews(
                store, date(2026, 8, 7), failing_reviewer
            )
            item = store.load(date(2026, 8, 5)).items[0]

        self.assertEqual(report.retryable_count, 3)
        self.assertEqual(item.state, "review_pending")
        self.assertEqual(item.attempt_count, 1)
        self.assertEqual(item.last_error_code, "PRICE_DATA_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
