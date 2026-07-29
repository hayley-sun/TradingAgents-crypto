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


class HermesLearningTests(unittest.TestCase):
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
