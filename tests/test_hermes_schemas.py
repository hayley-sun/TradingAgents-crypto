import unittest
from datetime import date, datetime, timezone

from pydantic import ValidationError

from tradingagents.integrations.schemas import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisSession,
    PaperDecisionReview,
    PriceReference,
    ReviewRequest,
    SymbolLearningEntry,
    SymbolLearningIndex,
    ToolError,
    is_valid_review_id,
    is_valid_session_id,
    utc_now,
)


class HermesSchemaTests(unittest.TestCase):
    def test_deepseek_request_normalizes_values(self):
        request = AnalysisRequest(
            symbol="  btcusdt ",
            trade_date="2026-07-28",
            analysts=[" MARKET ", "Social"],
            research_depth=3,
            llm_provider=" DeepSeek ",
            quick_model="  quick-model ",
            deep_model=" deep-model ",
        )

        self.assertEqual(request.symbol, "BTCUSDT")
        self.assertEqual(request.trade_date, date(2026, 7, 28))
        self.assertEqual(request.analysts, ["market", "social"])
        self.assertEqual(request.llm_provider, "deepseek")
        self.assertEqual(request.quick_model, "quick-model")
        self.assertEqual(request.deep_model, "deep-model")

    def test_invalid_analyst_provider_and_depth_are_rejected(self):
        values = {
            "symbol": "BTC",
            "trade_date": "2026-07-28",
            "analysts": ["market"],
            "research_depth": 1,
            "llm_provider": "openai",
            "quick_model": "quick",
            "deep_model": "deep",
        }

        for field, invalid_value in (
            ("analysts", ["unknown"]),
            ("llm_provider", "unknown"),
            ("research_depth", 2),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                AnalysisRequest(**{**values, field: invalid_value})

    def test_non_string_analyst_is_rejected_with_validation_error(self):
        with self.assertRaises(ValidationError):
            AnalysisRequest(
                symbol="BTC",
                trade_date="2026-07-28",
                analysts=[{}],
                research_depth=1,
                llm_provider="openai",
                quick_model="quick",
                deep_model="deep",
            )

    def test_session_ids_reject_empty_and_path_traversal(self):
        for session_id in ("", "../hermes_0123456789abcdef", "hermes_", "hermes_ABCDEF0123456789"):
            self.assertFalse(is_valid_session_id(session_id))

    def test_session_id_shape_and_utc_now(self):
        self.assertTrue(is_valid_session_id("hermes_0123456789abcdef"))
        self.assertTrue(is_valid_session_id("hermes_" + "a" * 64))
        self.assertFalse(is_valid_session_id("hermes_" + "a" * 15))
        self.assertFalse(is_valid_session_id("hermes_" + "g" * 16))

        current = utc_now()
        self.assertIsNotNone(current.tzinfo)
        self.assertEqual(current.utcoffset(), timezone.utc.utcoffset(current))

    def test_review_models_normalize_and_reject_invalid_values(self):
        request = ReviewRequest(
            session_id="hermes_0123456789abcdef",
            review_date="2026-07-29",
        )
        entry_price = PriceReference(
            date="2026-07-28", usd_price=100.0, source="coingecko"
        )
        review_price = PriceReference(
            date="2026-07-29", usd_price=110.0, source="cryptocompare"
        )
        review = PaperDecisionReview(
            review_id="review_0123456789abcdef",
            session_id=request.session_id,
            symbol=" btc ",
            trade_date="2026-07-28",
            review_date=request.review_date,
            action="BUY",
            entry_price=entry_price,
            review_price=review_price,
            raw_return_pct=10.0,
            verdict="correct",
            created_at=utc_now(),
            hermes_memory_entry="Paper-trading research lesson for BTC.",
        )
        index = SymbolLearningIndex(
            symbol=" btc ",
            updated_at=utc_now(),
            entries=[
                SymbolLearningEntry(
                    review_id=review.review_id,
                    review_date=review.review_date,
                    lesson=review.hermes_memory_entry,
                )
            ],
        )

        self.assertEqual(request.review_date, date(2026, 7, 29))
        self.assertTrue(is_valid_review_id(review.review_id))
        self.assertEqual(review.symbol, "BTC")
        self.assertEqual(review.review_price.source, "cryptocompare")
        self.assertEqual(index.symbol, "BTC")
        self.assertEqual(index.entries[0].review_id, review.review_id)

        with self.assertRaises(ValidationError):
            ReviewRequest(session_id="../session", review_date="2026-07-29")
        with self.assertRaises(ValidationError):
            PriceReference(date="2026-07-29", usd_price=0, source="coingecko")
        with self.assertRaises(ValidationError):
            PriceReference(date="2026-07-29", usd_price=1, source="unknown")
        with self.assertRaises(ValidationError):
            PaperDecisionReview.model_validate(
                {
                    **review.model_dump(),
                    "review_id": "../review",
                    "raw_return_pct": float("nan"),
                }
            )

    def test_models_forbid_extra_fields(self):
        with self.assertRaises(ValidationError):
            AnalysisRequest(
                symbol="BTC",
                trade_date="2026-07-28",
                analysts=["market"],
                research_depth=1,
                llm_provider="openai",
                quick_model="quick",
                deep_model="deep",
                extra="forbidden",
            )

    def test_session_and_result_models(self):
        request = AnalysisRequest(
            symbol="BTC",
            trade_date="2026-07-28",
            analysts=["market"],
            research_depth=1,
            llm_provider="ollama",
            quick_model="quick",
            deep_model="deep",
        )
        result = AnalysisResult(
            reports={"market": "report"},
            investment_plan="plan",
            trader_investment_plan="trader plan",
            final_trade_decision="buy",
            processed_signal="bullish",
        )
        session = AnalysisSession(
            session_id="hermes_0123456789abcdef",
            request=request,
            result=result,
            created_at=datetime.now(timezone.utc),
        )

        self.assertEqual(session.schema_version, 1)
        self.assertEqual(session.status, "running")
        self.assertIsNone(session.completed_at)

    def test_queued_session_tracks_worker_metadata(self):
        request = AnalysisRequest(
            symbol="BTC",
            trade_date="2026-07-28",
            analysts=["market"],
            research_depth=1,
            llm_provider="ollama",
            quick_model="quick",
            deep_model="deep",
        )

        session = AnalysisSession(
            session_id="hermes_0123456789abcdef",
            request=request,
            created_at=datetime.now(timezone.utc),
            status="queued",
        )

        self.assertEqual(session.status, "queued")
        self.assertIsNone(session.started_at)
        self.assertIsNone(session.worker_pid)

    def test_tool_error_has_string_fields(self):
        error = ToolError(code="bad_request", message="Invalid request", suggested_action="Retry")
        self.assertEqual(error.code, "bad_request")


if __name__ == "__main__":
    unittest.main()
