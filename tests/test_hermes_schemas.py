import unittest
from datetime import date, datetime, timezone

from pydantic import ValidationError

from tradingagents.integrations.schemas import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisSession,
    ToolError,
    is_valid_session_id,
    utc_now,
)


class HermesSchemaTests(unittest.TestCase):
    def test_deepseek_request_normalizes_values(self):
        request = AnalysisRequest(
            symbol="  btcusdt ",
            date="2026-07-28",
            analysts=[" MARKET ", "Social"],
            research_depth=3,
            provider=" DeepSeek ",
            quick_model="  quick-model ",
            deep_model=" deep-model ",
        )

        self.assertEqual(request.symbol, "BTCUSDT")
        self.assertEqual(request.date, date(2026, 7, 28))
        self.assertEqual(request.analysts, ["market", "social"])
        self.assertEqual(request.provider, "deepseek")
        self.assertEqual(request.quick_model, "quick-model")
        self.assertEqual(request.deep_model, "deep-model")

    def test_invalid_analyst_provider_and_depth_are_rejected(self):
        values = {
            "symbol": "BTC",
            "date": "2026-07-28",
            "analysts": ["market"],
            "research_depth": 1,
            "provider": "openai",
            "quick_model": "quick",
            "deep_model": "deep",
        }

        for field, invalid_value in (
            ("analysts", ["unknown"]),
            ("provider", "unknown"),
            ("research_depth", 2),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                AnalysisRequest(**{**values, field: invalid_value})

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

    def test_models_forbid_extra_fields(self):
        with self.assertRaises(ValidationError):
            AnalysisRequest(
                symbol="BTC",
                date="2026-07-28",
                analysts=["market"],
                research_depth=1,
                provider="openai",
                quick_model="quick",
                deep_model="deep",
                extra="forbidden",
            )

    def test_session_and_result_models(self):
        request = AnalysisRequest(
            symbol="BTC",
            date="2026-07-28",
            analysts=["market"],
            research_depth=1,
            provider="ollama",
            quick_model="quick",
            deep_model="deep",
        )
        result = AnalysisResult(
            reports={"market": "report"},
            investment_plan="plan",
            trader_investment_plan="trader plan",
            final_decision="buy",
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

    def test_tool_error_has_string_fields(self):
        error = ToolError(code="bad_request", message="Invalid request", suggested_action="Retry")
        self.assertEqual(error.code, "bad_request")


if __name__ == "__main__":
    unittest.main()
