import multiprocessing
import time
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingagents.integrations.hermes_learning import (
    LearningStore,
    ReviewStore,
    review_completed_session,
)
from tradingagents.integrations.schemas import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisSession,
    PaperDecisionReview,
    PriceReference,
    utc_now,
)


def completed_session(action, final_trade_decision=None, symbol="BTC"):
    request = AnalysisRequest(
        symbol=symbol,
        trade_date="2026-07-28",
        analysts=["market"],
        research_depth=1,
        llm_provider="deepseek",
        quick_model="quick",
        deep_model="deep",
    )
    return AnalysisSession(
        session_id="hermes_0123456789abcdef",
        status="completed",
        created_at=utc_now(),
        completed_at=utc_now(),
        request=request,
        result=AnalysisResult(
            reports={"market": "report"},
            investment_plan="plan",
            trader_investment_plan="trader plan",
            final_trade_decision=final_trade_decision or action,
            processed_signal=action,
        ),
    )


def _concurrent_learning_upsert(root, review_payload, ready_queue, start_event):
    from tradingagents.integrations import hermes_learning

    original_write = hermes_learning._atomic_json_write

    def delayed_write(destination, value):
        time.sleep(0.25)
        original_write(destination, value)

    hermes_learning._atomic_json_write = delayed_write
    ready_queue.put(True)
    start_event.wait(timeout=5)
    hermes_learning.LearningStore(Path(root)).upsert(
        PaperDecisionReview.model_validate(review_payload)
    )


class HermesLearningTests(unittest.TestCase):
    def test_concurrent_upserts_retain_each_review(self):
        def review(review_id):
            return PaperDecisionReview(
                review_id=review_id,
                session_id=f"hermes_{review_id.removeprefix('review_')}",
                symbol="BTC",
                trade_date="2026-07-28",
                review_date="2026-07-29",
                action="BUY",
                entry_price=PriceReference(
                    date="2026-07-28", usd_price=100.0, source="coingecko"
                ),
                review_price=PriceReference(
                    date="2026-07-29", usd_price=110.0, source="coingecko"
                ),
                raw_return_pct=10.0,
                verdict="correct",
                created_at=utc_now(),
                hermes_memory_entry=f"Paper-trading lesson for {review_id}.",
            )

        review_ids = (
            "review_0123456789abcdea",
            "review_0123456789abcdef",
        )
        context = multiprocessing.get_context("fork")
        with TemporaryDirectory() as directory:
            ready_queue = context.Queue()
            start_event = context.Event()
            processes = [
                context.Process(
                    target=_concurrent_learning_upsert,
                    args=(
                        directory,
                        review(review_id).model_dump(mode="json"),
                        ready_queue,
                        start_event,
                    ),
                )
                for review_id in review_ids
            ]
            for process in processes:
                process.start()
            for _ in processes:
                ready_queue.get(timeout=5)
            start_event.set()
            for process in processes:
                process.join(timeout=5)
                self.assertEqual(process.exitcode, 0)

            index = LearningStore(Path(directory)).load("BTC")

        self.assertIsNotNone(index)
        self.assertEqual({entry.review_id for entry in index.entries}, set(review_ids))

    def test_buy_review_is_idempotent_and_writes_a_paper_trading_lesson(self):
        calls = []

        def price_lookup(_symbol, reference_date):
            calls.append(reference_date)
            return {date(2026, 7, 28): 100.0, date(2026, 7, 29): 110.0}[reference_date]

        with TemporaryDirectory() as directory:
            review_store = ReviewStore(Path(directory) / "reviews")
            learning_store = LearningStore(Path(directory) / "memories")
            session = completed_session("BUY", "FINAL TRANSACTION PROPOSAL: **BUY**")
            review = review_completed_session(
                session,
                date(2026, 7, 29),
                price_lookup,
                review_store,
                learning_store,
                current_date=date(2026, 7, 29),
            )
            repeated = review_completed_session(
                session,
                date(2026, 7, 29),
                price_lookup,
                review_store,
                learning_store,
                current_date=date(2026, 7, 29),
            )

            self.assertEqual(learning_store.lessons_for("ETH"), [])
            self.assertEqual(learning_store.lessons_for("BTC"), [review.hermes_memory_entry])

        self.assertEqual(review.action, "BUY")
        self.assertEqual(review.verdict, "correct")
        self.assertEqual(review.raw_return_pct, 10.0)
        self.assertEqual(repeated.review_id, review.review_id)
        self.assertEqual(calls, [date(2026, 7, 28), date(2026, 7, 29)])
        self.assertIn("paper-trading", review.hermes_memory_entry.lower())

    def test_sell_hold_and_unparseable_actions_have_deterministic_verdicts(self):
        cases = (
            ("SELL", "FINAL TRANSACTION PROPOSAL: SELL", 90.0, "correct"),
            ("HOLD", "FINAL TRANSACTION PROPOSAL: HOLD", 110.0, "not_scored"),
            ("unknown", "no terminal action", 110.0, "not_scored"),
        )

        for action, final_decision, review_price, verdict in cases:
            with self.subTest(action=action), TemporaryDirectory() as directory:
                review = review_completed_session(
                    completed_session(action, final_decision),
                    date(2026, 7, 29),
                    lambda _symbol, value: 100.0 if value == date(2026, 7, 28) else review_price,
                    ReviewStore(Path(directory) / "reviews"),
                    LearningStore(Path(directory) / "memories"),
                    current_date=date(2026, 7, 29),
                )

                self.assertEqual(review.verdict, verdict)

    def test_review_rejects_invalid_date_or_non_completed_session_without_writes(self):
        with TemporaryDirectory() as directory:
            review_store = ReviewStore(Path(directory) / "reviews")
            learning_store = LearningStore(Path(directory) / "memories")
            session = completed_session("BUY")

            for review_date in (date(2026, 7, 28), date(2026, 7, 30)):
                with self.subTest(review_date=review_date), self.assertRaises(ValueError):
                    review_completed_session(
                        session,
                        review_date,
                        lambda *_args: 100.0,
                        review_store,
                        learning_store,
                        current_date=date(2026, 7, 29),
                    )

            failed = session.model_copy(update={"status": "failed", "result": None})
            with self.assertRaises(ValueError):
                review_completed_session(
                    failed,
                    date(2026, 7, 29),
                    lambda *_args: 100.0,
                    review_store,
                    learning_store,
                    current_date=date(2026, 7, 29),
                )

            self.assertFalse(review_store.root.exists())
            self.assertFalse(learning_store.root.exists())

    def test_price_failure_leaves_no_review_or_learning_entry(self):
        with TemporaryDirectory() as directory:
            review_store = ReviewStore(Path(directory) / "reviews")
            learning_store = LearningStore(Path(directory) / "memories")
            with self.assertRaises(ValueError):
                review_completed_session(
                    completed_session("BUY"),
                    date(2026, 7, 29),
                    lambda *_args: 0.0,
                    review_store,
                    learning_store,
                    current_date=date(2026, 7, 29),
                )

            self.assertFalse(review_store.root.exists())
            self.assertFalse(learning_store.root.exists())

    def test_repeated_review_repairs_missing_learning_index_without_price_lookup(self):
        with TemporaryDirectory() as directory:
            review_store = ReviewStore(Path(directory) / "reviews")
            learning_store = LearningStore(Path(directory) / "memories")
            session = completed_session("BUY")
            review = review_completed_session(
                session,
                date(2026, 7, 29),
                lambda _symbol, value: 100.0 if value == date(2026, 7, 28) else 110.0,
                review_store,
                learning_store,
                current_date=date(2026, 7, 29),
            )
            learning_store.path_for("BTC").unlink()

            repaired = review_completed_session(
                session,
                date(2026, 7, 29),
                lambda *_args: self.fail("price lookup must not run for an existing review"),
                review_store,
                learning_store,
                current_date=date(2026, 7, 29),
            )

            self.assertEqual(repaired.review_id, review.review_id)
            self.assertEqual(learning_store.lessons_for("BTC"), [review.hermes_memory_entry])


if __name__ == "__main__":
    unittest.main()
