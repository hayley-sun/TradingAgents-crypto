import asyncio
import json
import os
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import chromadb
from chromadb.config import Settings

from tradingagents.integrations.schemas import AnalysisRequest

from tradingagents.integrations.hermes_mcp import (
    MCP,
    PAPER_TRADING_DISCLAIMER,
    SessionStore,
    _cleanup_session_collections,
    execute_analysis,
    get_analysis_result_impl,
    health_check_impl,
)


class FakeGraph:
    instances = []
    final_state = {
        "market_report": "market report",
        "sentiment_report": "sentiment report",
        "news_report": "news report",
        "fundamentals_report": "fundamentals report",
        "investment_plan": "investment plan",
        "trader_investment_plan": "trader investment plan",
        "final_trade_decision": "final trade decision",
    }

    def __init__(self, selected_analysts, debug, config):
        self.selected_analysts = selected_analysts
        self.debug = debug
        self.config = config
        self.propagate_calls = []
        self.__class__.instances.append(self)

    def propagate(self, symbol, trade_date):
        self.propagate_calls.append((symbol, trade_date))
        return self.final_state, "HOLD"


class FailingGraph(FakeGraph):
    def propagate(self, symbol, trade_date):
        self.propagate_calls.append((symbol, trade_date))
        raise RuntimeError("provider secret at /private/failure")


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

    def test_health_contains_session_store_resolution_errors(self):
        with TemporaryDirectory() as temp_dir:
            loop = Path(temp_dir) / "loop"
            loop.symlink_to(loop)
            with patch.dict(
                os.environ,
                {"TRADINGAGENTS_RESULTS_DIR": str(loop)},
                clear=True,
            ):
                result = health_check_impl()

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "SESSION_STORE_UNAVAILABLE")
        self.assertNotIn(str(loop), json.dumps(result))
        self.assertNotIn("Symlink loop", json.dumps(result))

    def test_result_contains_session_store_resolution_errors(self):
        with TemporaryDirectory() as temp_dir:
            loop = Path(temp_dir) / "loop"
            loop.symlink_to(loop)
            with patch.dict(
                os.environ,
                {"TRADINGAGENTS_RESULTS_DIR": str(loop)},
                clear=True,
            ):
                result = get_analysis_result_impl("hermes_0123456789abcdef")

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "SESSION_UNREADABLE")
        self.assertNotIn(str(loop), json.dumps(result))
        self.assertNotIn("Symlink loop", json.dumps(result))

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

    def test_cleanup_removes_only_owned_session_collections(self):
        session_id = "hermes_0123456789abcdef"
        owned_collection = f"bull_memory_{session_id}"
        foreign_collection = f"foreign_{session_id}"
        chroma_client = chromadb.Client(Settings(allow_reset=True))

        try:
            existing_names = {
                getattr(collection, "name", collection)
                for collection in chroma_client.list_collections()
            }
            for collection_name in (owned_collection, foreign_collection):
                if collection_name in existing_names:
                    chroma_client.delete_collection(name=collection_name)

            chroma_client.create_collection(name=owned_collection)
            chroma_client.create_collection(name=foreign_collection)

            _cleanup_session_collections(session_id)

            remaining_names = {
                getattr(collection, "name", collection)
                for collection in chroma_client.list_collections()
            }
            self.assertNotIn(owned_collection, remaining_names)
            self.assertIn(foreign_collection, remaining_names)
        finally:
            remaining_names = {
                getattr(collection, "name", collection)
                for collection in chroma_client.list_collections()
            }
            for collection_name in (owned_collection, foreign_collection):
                if collection_name in remaining_names:
                    chroma_client.delete_collection(name=collection_name)

    def test_analyze_crypto_schema_forbids_unknown_fields(self):
        tool = MCP._tool_manager.get_tool("analyze_crypto")

        self.assertIs(tool.parameters["additionalProperties"], False)

    @patch("tradingagents.integrations.hermes_mcp.get_provider_api_key", return_value="")
    def test_analyze_crypto_rejects_unknown_mcp_fields_before_provider_access(
        self, provider_key
    ):
        request_data = self.make_request().model_dump(mode="json")
        request_data["unexpected_field"] = "unexpected"

        _, result = asyncio.run(MCP.call_tool("analyze_crypto", request_data))

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "INVALID_REQUEST")
        provider_key.assert_not_called()

    @patch("tradingagents.integrations.hermes_mcp._cleanup_session_collections")
    @patch("tradingagents.integrations.hermes_mcp.get_provider_api_key", return_value="api-key")
    def test_execute_analysis_persists_completed_fake_graph_result(
        self, provider_key, cleanup
    ):
        FakeGraph.instances = []
        request = self.make_request()

        with TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions")
            result = execute_analysis(
                request.model_dump(mode="json"), store=store, graph_factory=FakeGraph
            )
            session = store.load(result["data"]["session_id"])

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["data"]["status"], "completed")
        self.assertEqual(result["data"]["processed_signal"], "HOLD")
        self.assertEqual(result["data"]["final_trade_decision"], "final trade decision")
        self.assertEqual(result["data"]["disclaimer"], PAPER_TRADING_DISCLAIMER)
        self.assertEqual(len(FakeGraph.instances), 1)
        graph = FakeGraph.instances[0]
        self.assertEqual(graph.selected_analysts, ["market"])
        self.assertIs(graph.debug, False)
        self.assertEqual(graph.propagate_calls, [("BTCUSDT", "2026-07-28")])
        self.assertEqual(graph.config["llm_provider"], "openai")
        self.assertEqual(graph.config["quick_think_llm"], "quick")
        self.assertEqual(graph.config["deep_think_llm"], "deep")
        self.assertEqual(graph.config["max_debate_rounds"], 1)
        self.assertEqual(graph.config["max_risk_discuss_rounds"], 1)
        self.assertIs(graph.config["log_graph_states"], False)
        self.assertEqual(graph.config["session_id"], result["data"]["session_id"])
        self.assertEqual(session.status, "completed")
        self.assertIsNotNone(session.completed_at)
        self.assertEqual(
            session.result.reports,
            {
                "market": "market report",
                "sentiment": "sentiment report",
                "news": "news report",
                "fundamentals": "fundamentals report",
            },
        )
        self.assertEqual(session.result.investment_plan, "investment plan")
        self.assertEqual(session.result.trader_investment_plan, "trader investment plan")
        self.assertEqual(session.result.final_trade_decision, "final trade decision")
        self.assertEqual(session.result.processed_signal, "HOLD")
        cleanup.assert_called_once_with(result["data"]["session_id"])
        provider_key.assert_called_once_with("openai")

    @patch("tradingagents.integrations.hermes_mcp.get_provider_api_key", return_value="")
    def test_execute_analysis_rejects_provider_without_api_key(self, provider_key):
        with TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions")
            result = execute_analysis(self.make_request().model_dump(mode="json"), store=store)

            self.assertFalse(store.root.exists())

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "MISSING_API_KEY")
        provider_key.assert_called_once_with("openai")

    def test_execute_analysis_rejects_invalid_request_without_session(self):
        request_data = self.make_request().model_dump(mode="json")
        request_data["research_depth"] = 2

        with TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions")
            result = execute_analysis(request_data, store=store)

            self.assertFalse(store.root.exists())

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "INVALID_REQUEST")

    @patch("tradingagents.integrations.hermes_mcp._cleanup_session_collections")
    @patch("tradingagents.integrations.hermes_mcp.get_provider_api_key", return_value="api-key")
    def test_execute_analysis_redacts_graph_failure_and_persists_failed_session(
        self, provider_key, cleanup
    ):
        FailingGraph.instances = []

        with TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions")
            result = execute_analysis(
                self.make_request().model_dump(mode="json"),
                store=store,
                graph_factory=FailingGraph,
            )
            session = store.load(next(store.root.glob("*.json")).stem)

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "ANALYSIS_FAILED")
        self.assertEqual(session.status, "failed")
        self.assertIsNotNone(session.completed_at)
        self.assertEqual(session.error.code, "ANALYSIS_FAILED")
        self.assertNotIn("provider secret", json.dumps(result))
        self.assertNotIn("/private/failure", json.dumps(result))
        self.assertNotIn("provider secret", json.dumps(session.model_dump(mode="json")))
        self.assertNotIn("/private/failure", json.dumps(session.model_dump(mode="json")))
        cleanup.assert_called_once_with(session.session_id)
        provider_key.assert_called_once_with("openai")


if __name__ == "__main__":
    unittest.main()
