import json
import os
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tradingagents.integrations.schemas import AnalysisRequest

from tradingagents.integrations.hermes_mcp import (
    PAPER_TRADING_DISCLAIMER,
    SessionStore,
    get_analysis_result_impl,
    health_check_impl,
)


class HermesMcpTests(unittest.TestCase):
    def make_request(self):
        return AnalysisRequest(
            symbol="BTCUSDT",
            trade_date=date(2026, 7, 28),
            analysts=["market"],
            research_depth=1,
            llm_provider="openai",
            quick_model="quick",
            deep_model="deep",
        )

    def test_atomic_store_write_load_and_internal_result_lookup(self):
        with TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "hermes" / "sessions")
            session_id = "hermes_0123456789abcdef"
            created = store.create(session_id, self.make_request())

            self.assertTrue(store.path_for(session_id).is_file())
            self.assertEqual(list(store.root.glob("*.tmp")), [])
            self.assertEqual(store.load(session_id), created)

            result = get_analysis_result_impl(session_id, store=store)

            self.assertEqual(result["ok"], True)
            self.assertEqual(result["data"]["session"], created.model_dump(mode="json"))
            self.assertEqual(result["data"]["disclaimer"], PAPER_TRADING_DISCLAIMER)

    def test_traversal_session_id_returns_structured_error(self):
        result = get_analysis_result_impl("../hermes_0123456789abcdef")

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "INVALID_SESSION_ID")

    def test_health_exposes_fake_provider_secret_only_as_boolean(self):
        secret = "not-for-output"
        with TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "TRADINGAGENTS_RESULTS_DIR": temp_dir,
                "OPENAI_API_KEY": secret,
                "FINNHUB_API_KEY": secret,
                "COINGECKO_DEMO_API_KEY": secret,
            },
            clear=True,
        ):
            result = health_check_impl()

        self.assertEqual(result["ok"], True)
        data = result["data"]
        self.assertEqual(
            set(data),
            {
                "status",
                "project_dir",
                "session_store",
                "session_store_writable",
                "llm_provider_key_available",
                "configured_llm_providers",
                "finnhub_key_available",
                "coingecko_key_available",
                "disclaimer",
            },
        )
        self.assertEqual(data["status"], "ready")
        self.assertTrue(Path(data["project_dir"]).is_absolute())
        self.assertTrue(Path(data["session_store"]).is_absolute())
        self.assertIs(data["session_store_writable"], True)
        self.assertEqual(
            data["llm_provider_key_available"],
            {
                "openai": True,
                "anthropic": False,
                "google": False,
                "deepseek": False,
                "openrouter": False,
            },
        )
        self.assertEqual(data["configured_llm_providers"], ["openai"])
        self.assertIs(data["finnhub_key_available"], True)
        self.assertIs(data["coingecko_key_available"], True)
        self.assertEqual(data["disclaimer"], PAPER_TRADING_DISCLAIMER)
        self.assertNotIn(secret, json.dumps(result))

    def test_health_ignores_legacy_coingecko_key(self):
        with TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "TRADINGAGENTS_RESULTS_DIR": temp_dir,
                "OPENAI_API_KEY": "openai-key",
                "COINGECKO_API_KEY": "legacy-key",
            },
            clear=True,
        ):
            result = health_check_impl()

        self.assertIs(result["data"]["coingecko_key_available"], False)

    def test_missing_session_returns_structured_error(self):
        with TemporaryDirectory() as temp_dir:
            result = get_analysis_result_impl(
                "hermes_0123456789abcdef",
                store=SessionStore(Path(temp_dir) / "sessions"),
            )

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "SESSION_NOT_FOUND")

    def test_malformed_saved_json_returns_structured_error(self):
        with TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions")
            session_id = "hermes_0123456789abcdef"
            store.ensure()
            store.path_for(session_id).write_text("{not json", encoding="ascii")

            result = get_analysis_result_impl(session_id, store=store)

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "SESSION_UNREADABLE")


if __name__ == "__main__":
    unittest.main()
