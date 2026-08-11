import asyncio
import inspect
import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import chromadb
from jsonschema import Draft202012Validator
from chromadb.config import Settings
import tradingagents.integrations.hermes_mcp as hermes_mcp

from tradingagents.agents.utils.memory import FinancialSituationMemory
from tradingagents.dataflows.crypto_price_references import HistoricalUsdReference
from tradingagents.integrations.hermes_learning import LearningStore, ReviewStore
from tradingagents.integrations.hermes_report_learning import (
    ReportLearningStore,
    record_review_fact,
)
from tradingagents.integrations.hermes_reports import ReportBatchStore
from tradingagents.integrations.hermes_scheduled_reviews import ScheduledReviewStore
from tradingagents.integrations.schemas import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisSession,
    PaperDecisionReview,
    PriceReference,
)

from tradingagents.integrations.hermes_mcp import (
    MCP,
    PAPER_TRADING_DISCLAIMER,
    SessionStore,
    _cleanup_session_collections,
    archive_daily_report_impl,
    execute_analysis,
    get_analysis_result_impl,
    health_check_impl,
    review_paper_decision_impl,
    run_queued_analysis,
    start_daily_report_batch_impl,
    start_analysis,
    submit_report_reflection_impl,
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


def paired_price_references(entry_price=100.0, review_price=90.0, source="coingecko"):
    def resolve(_symbol, trade_date, review_date):
        return (
            PriceReference(date=trade_date, usd_price=entry_price, source=source),
            PriceReference(date=review_date, usd_price=review_price, source=source),
        )

    return resolve


class HermesMcpTests(unittest.TestCase):
    def valid_reflection_payload(self):
        return {
            "decision_thesis": "Buy only after the archived confirmation signal.",
            "overall_assessment": "The paper decision was disciplined but uncertain.",
            "outcome_assessments": [{"horizon_days": 1, "assessment": "T+1 was assessed."}],
            "reasoning_strengths": ["The entry condition was explicit."],
            "causal_hypotheses": [{"statement": "Momentum may have persisted.", "evidence": ["report.market", "outcome.t1"], "confidence": "medium"}],
            "mistakes_or_missed_opportunities": ["The analysis lacked an invalidation level."],
            "next_decision_checks": ["Check confirmation volume."],
        }

    def test_submit_report_reflection_tool_rejects_unknown_fields(self):
        tool = MCP._tool_manager.get_tool("submit_report_reflection")
        self.assertIs(tool.parameters["additionalProperties"], False)
        self.assertIs(
            tool.parameters["properties"]["reflection"]["additionalProperties"],
            False,
        )
        _, result = asyncio.run(MCP.call_tool(
            "submit_report_reflection",
            {"session_id":"hermes_0123456789abcdef","expected_revision":1,"reflection":self.valid_reflection_payload(),"unexpected":True},
        ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_REPORT_REFLECTION")

    def test_submit_report_reflection_tool_rejects_nested_unknown_fields(self):
        reflection = self.valid_reflection_payload()
        reflection["unexpected_nested"] = True
        _, result = asyncio.run(MCP.call_tool(
            "submit_report_reflection",
            {
                "session_id": "hermes_0123456789abcdef",
                "expected_revision": 1,
                "reflection": reflection,
            },
        ))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_REPORT_REFLECTION")

    def test_submit_report_reflection_schema_resolves_nested_refs(self):
        tool = MCP._tool_manager.get_tool("submit_report_reflection")
        arguments = {
            "session_id": "hermes_0123456789abcdef",
            "expected_revision": 1,
            "reflection": self.valid_reflection_payload(),
        }
        validator = Draft202012Validator(tool.parameters)
        self.assertEqual(list(validator.iter_errors(arguments)), [])
        invalid = dict(arguments)
        invalid["reflection"] = {
            **arguments["reflection"],
            "unexpected_nested": True,
        }
        self.assertTrue(list(validator.iter_errors(invalid)))

    def test_submit_report_reflection_tool_rejects_non_strict_revisions_before_storage(self):
        for revision in (True, 1.0, "1"):
            with self.subTest(revision=revision), patch.object(
                hermes_mcp,
                "SessionStore",
                side_effect=AssertionError("store accessed"),
            ):
                _, result = asyncio.run(MCP.call_tool(
                    "submit_report_reflection",
                    {
                        "session_id": "hermes_0123456789abcdef",
                        "expected_revision": revision,
                        "reflection": self.valid_reflection_payload(),
                    },
                ))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "INVALID_REPORT_REFLECTION")

    def test_same_day_reflection_retry_is_deferred_without_write(self):
        self.assertIn(
            "attempt_date",
            inspect.signature(submit_report_reflection_impl).parameters,
        )
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "hermes"
            session_store = SessionStore(root / "sessions")
            report_store = ReportLearningStore(root / "report_memories")
            learning_store = LearningStore(root / "memories")
            request = self.make_request()
            session = AnalysisSession(
                session_id="hermes_0123456789abcdef",
                status="completed",
                created_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                request=request,
                result=AnalysisResult(
                    reports={"market": "Archived market report."},
                    investment_plan="plan",
                    trader_investment_plan="trader plan",
                    final_trade_decision="FINAL TRANSACTION PROPOSAL: BUY",
                    processed_signal="BUY",
                ),
            )
            session_store.save(session)
            review_date = request.trade_date + timedelta(days=1)
            record_review_fact(
                report_store,
                session,
                PaperDecisionReview(
                    review_id="review_0123456789abcdef0123456789abcdef",
                    session_id=session.session_id,
                    symbol=request.symbol,
                    trade_date=request.trade_date,
                    review_date=review_date,
                    horizon_days=1,
                    action="BUY",
                    entry_price=PriceReference(
                        date=request.trade_date, usd_price=100.0, source="coinbase"
                    ),
                    review_price=PriceReference(
                        date=review_date, usd_price=101.0, source="coinbase"
                    ),
                    raw_return_pct=1.0,
                    verdict="correct",
                    created_at=datetime.now(timezone.utc),
                    hermes_memory_entry="Legacy paper lesson.",
                ),
            )
            unsafe_reflection = self.valid_reflection_payload()
            unsafe_reflection["overall_assessment"] = (
                "This outcome was guaranteed."
            )

            first = submit_report_reflection_impl(
                {
                    "session_id": session.session_id,
                    "expected_revision": 1,
                    "reflection": unsafe_reflection,
                },
                session_store=session_store,
                report_store=report_store,
                learning_store=learning_store,
                attempt_date=date(2026, 8, 11),
            )
            record_path = report_store.path_for(session.session_id)
            first_bytes = record_path.read_bytes()
            same_day = submit_report_reflection_impl(
                {
                    "session_id": session.session_id,
                    "expected_revision": 1,
                    "reflection": unsafe_reflection,
                },
                session_store=session_store,
                report_store=report_store,
                learning_store=learning_store,
                attempt_date=date(2026, 8, 11),
            )
            snapshot = report_store.load(session.session_id).revisions[0]

            self.assertEqual(first["error"]["code"], "REFLECTION_UNSAFE_CONTENT")
            self.assertIn("Do not submit", first["error"]["suggested_action"])
            self.assertEqual(
                same_day["error"]["code"],
                "REPORT_REFLECTION_RETRY_DEFERRED",
            )
            self.assertIn(
                "current Agent run", same_day["error"]["suggested_action"]
            )
            self.assertEqual(snapshot.reflection_attempt_count, 1)
            self.assertEqual(record_path.read_bytes(), first_bytes)

            malformed_reflection = self.valid_reflection_payload()
            malformed_reflection.pop("decision_thesis")
            schema_rejected = submit_report_reflection_impl(
                {
                    "session_id": session.session_id,
                    "expected_revision": 1,
                    "reflection": malformed_reflection,
                },
                session_store=session_store,
                report_store=report_store,
                learning_store=learning_store,
                attempt_date=date(2026, 8, 12),
            )
            schema_rejected_bytes = record_path.read_bytes()
            schema_deferred = submit_report_reflection_impl(
                {
                    "session_id": session.session_id,
                    "expected_revision": 1,
                    "reflection": malformed_reflection,
                },
                session_store=session_store,
                report_store=report_store,
                learning_store=learning_store,
                attempt_date=date(2026, 8, 12),
            )
            snapshot = report_store.load(session.session_id).revisions[0]

            self.assertEqual(
                schema_rejected["error"]["code"], "INVALID_REPORT_REFLECTION"
            )
            self.assertEqual(snapshot.last_error_code, "REFLECTION_SCHEMA_INVALID")
            self.assertEqual(snapshot.reflection_attempt_count, 2)
            self.assertEqual(
                schema_deferred["error"]["code"],
                "REPORT_REFLECTION_RETRY_DEFERRED",
            )
            self.assertEqual(record_path.read_bytes(), schema_rejected_bytes)

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

    def daily_request_data(self, **updates):
        values = {
            "trade_date": "2026-07-29",
            "symbols": ["BTC"],
            "analysts": ["market", "news"],
            "research_depth": 1,
            "llm_provider": "deepseek",
            "quick_model": "deepseek-v4-flash",
            "deep_model": "deepseek-v4-pro",
        }
        return {**values, **updates}

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
                "CRYPTOCOMPARE_API_KEY": secret,
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
                "cryptocompare_key_available",
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
        self.assertEqual(data["configured_llm_providers"], ["ollama", "openai"])
        self.assertIs(data["finnhub_key_available"], True)
        self.assertIs(data["coingecko_key_available"], True)
        self.assertIs(data["cryptocompare_key_available"], True)
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

    def test_health_treats_ollama_as_a_configured_local_provider(self):
        with TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"TRADINGAGENTS_RESULTS_DIR": temp_dir},
            clear=True,
        ):
            result = health_check_impl()

        self.assertEqual(result["data"]["status"], "ready")
        self.assertEqual(result["data"]["configured_llm_providers"], ["ollama"])
        self.assertNotIn("ollama", result["data"]["llm_provider_key_available"])

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

    def test_session_store_rejects_self_referential_link_without_resolve_error(self):
        with TemporaryDirectory() as temp_dir:
            loop = Path(temp_dir) / "loop"
            loop.symlink_to(loop)
            with patch.dict(
                os.environ,
                {"TRADINGAGENTS_RESULTS_DIR": str(loop)},
                clear=True,
            ), patch.object(
                Path,
                "resolve",
                return_value=loop / "hermes" / "sessions",
            ):
                with self.assertRaises(RuntimeError):
                    SessionStore.from_environment()

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

    def test_review_tool_forbids_unknown_fields(self):
        tool = MCP._tool_manager.get_tool("review_paper_decision")
        self.assertIs(tool.parameters["additionalProperties"], False)

        _, result = asyncio.run(
            MCP.call_tool(
                "review_paper_decision",
                {
                    "session_id": "hermes_0123456789abcdef",
                    "review_date": "2026-07-29",
                    "unexpected_field": "unexpected",
                },
            )
        )

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "INVALID_REVIEW_REQUEST")

    def test_daily_report_batch_tool_rejects_unknown_input_before_starting_workers(self):
        calls = []

        def starter(request):
            calls.append(request.symbol)
            return "hermes_0123456789abcdef"

        with TemporaryDirectory() as temp_dir:
            result = start_daily_report_batch_impl(
                self.daily_request_data(unexpected_field=True),
                batch_store=ReportBatchStore(Path(temp_dir) / "report_batches"),
                starter=starter,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_REPORT_REQUEST")
        self.assertEqual(calls, [])

    def test_daily_report_archive_refuses_active_batch(self):
        with TemporaryDirectory() as temp_dir:
            batch_store = ReportBatchStore(Path(temp_dir) / "report_batches")
            session = AnalysisSession(
                session_id="hermes_0123456789abcdef",
                status="running",
                created_at=datetime.now(timezone.utc),
                request=AnalysisRequest(
                    symbol="BTC",
                    trade_date="2026-07-29",
                    analysts=["market", "news"],
                    research_depth=1,
                    llm_provider="deepseek",
                    quick_model="deepseek-v4-flash",
                    deep_model="deepseek-v4-pro",
                ),
            )
            start_result = start_daily_report_batch_impl(
                self.daily_request_data(),
                batch_store=batch_store,
                starter=lambda _request: session.session_id,
            )
            result = archive_daily_report_impl(
                "2026-07-29",
                "Chinese narrative",
                batch_store=batch_store,
                session_loader=lambda _session_id: session,
            )

        self.assertTrue(start_result["ok"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "REPORT_BATCH_ACTIVE")

    def test_daily_report_archive_enrolls_new_scheduled_reviews(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "hermes"
            batch_store = ReportBatchStore(root / "report_batches")
            schedule_store = ScheduledReviewStore(root / "review_schedules")
            session = AnalysisSession(
                session_id="hermes_0123456789abcdef",
                status="completed",
                created_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                request=AnalysisRequest(
                    symbol="BTC",
                    trade_date="2026-07-29",
                    analysts=["market", "news"],
                    research_depth=1,
                    llm_provider="deepseek",
                    quick_model="deepseek-v4-flash",
                    deep_model="deepseek-v4-pro",
                ),
                result=AnalysisResult(
                    reports={},
                    investment_plan="plan",
                    trader_investment_plan="trader plan",
                    final_trade_decision="FINAL TRANSACTION PROPOSAL: BUY",
                    processed_signal="BUY",
                ),
            )
            start_daily_report_batch_impl(
                self.daily_request_data(),
                batch_store=batch_store,
                starter=lambda _request: session.session_id,
            )

            result = archive_daily_report_impl(
                "2026-07-29",
                "Chinese narrative",
                batch_store=batch_store,
                schedule_store=schedule_store,
                session_loader=lambda _session_id: session,
            )
            plan = schedule_store.load(date(2026, 7, 29))
            persisted_batch = batch_store.load(date(2026, 7, 29))

        self.assertTrue(result["ok"])
        self.assertIsNotNone(plan)
        self.assertEqual(plan.workflow_version, 2)
        self.assertEqual(persisted_batch.archive.scheduled_review_version, 2)
        self.assertEqual([item.horizon_days for item in plan.items], [1, 7, 15])

    def test_existing_unmarked_archive_is_not_backfilled(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "hermes"
            batch_store = ReportBatchStore(root / "report_batches")
            schedule_store = ScheduledReviewStore(root / "review_schedules")
            session = AnalysisSession(
                session_id="hermes_0123456789abcdef",
                status="completed",
                created_at=datetime.now(timezone.utc),
                completed_at=datetime.now(timezone.utc),
                request=AnalysisRequest(
                    symbol="BTC",
                    trade_date="2026-07-29",
                    analysts=["market", "news"],
                    research_depth=1,
                    llm_provider="deepseek",
                    quick_model="deepseek-v4-flash",
                    deep_model="deepseek-v4-pro",
                ),
                result=AnalysisResult(
                    reports={},
                    investment_plan="plan",
                    trader_investment_plan="trader plan",
                    final_trade_decision="FINAL TRANSACTION PROPOSAL: BUY",
                    processed_signal="BUY",
                ),
            )
            start_daily_report_batch_impl(
                self.daily_request_data(),
                batch_store=batch_store,
                starter=lambda _request: session.session_id,
            )
            batch = batch_store.load(date(2026, 7, 29))
            batch_store.archive(
                batch, lambda _session_id: session, "Legacy narrative"
            )

            result = archive_daily_report_impl(
                "2026-07-29",
                "Legacy narrative",
                batch_store=batch_store,
                schedule_store=schedule_store,
                session_loader=lambda _session_id: session,
            )

        self.assertTrue(result["ok"])
        self.assertIsNone(schedule_store.load(date(2026, 7, 29)))

    def test_daily_report_mcp_tools_forbid_unknown_fields(self):
        for tool_name in (
            "start_daily_report_batch",
            "get_daily_report_batch",
            "archive_daily_report",
        ):
            with self.subTest(tool_name=tool_name):
                tool = MCP._tool_manager.get_tool(tool_name)
                self.assertIsNotNone(tool)
                self.assertIs(tool.parameters["additionalProperties"], False)

        _, result = asyncio.run(
            MCP.call_tool(
                "start_daily_report_batch",
                self.daily_request_data(unexpected_field=True),
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_REPORT_REQUEST")

    def test_static_review_lessons_are_available_without_embeddings(self):
        memory = FinancialSituationMemory(
            "trader_memory",
            {
                "llm_provider": "deepseek",
                "hermes_review_lessons": ["BTC paper-trading lesson"],
            },
        )

        self.assertEqual(
            memory.get_memories("current market situation"),
            [
                {
                    "matched_situation": "",
                    "recommendation": "BTC paper-trading lesson",
                    "similarity_score": 1.0,
                }
            ],
        )

    @patch("tradingagents.integrations.hermes_mcp._cleanup_session_collections")
    @patch("tradingagents.integrations.hermes_mcp.get_provider_api_key", return_value="api-key")
    @patch("tradingagents.integrations.hermes_mcp.LearningStore.from_environment")
    def test_execute_analysis_passes_review_lessons_to_graph(
        self, learning_store_factory, provider_key, cleanup
    ):
        FakeGraph.instances = []
        learning_store_factory.return_value.lessons_for.return_value = [
            "BTCUSDT paper-trading lesson"
        ]

        with TemporaryDirectory() as temp_dir:
            execute_analysis(
                self.make_request().model_dump(mode="json"),
                store=SessionStore(Path(temp_dir) / "sessions"),
                graph_factory=FakeGraph,
            )

        self.assertEqual(
            FakeGraph.instances[-1].config["hermes_review_lessons"],
            ["BTCUSDT paper-trading lesson"],
        )
        self.assertIs(FakeGraph.instances[-1].config["log_graph_states"], False)
        learning_store_factory.return_value.lessons_for.assert_called_once_with(
            "BTCUSDT", limit=5
        )
        provider_key.assert_called_once_with("openai")
        cleanup.assert_called_once()

    @patch("tradingagents.integrations.hermes_mcp.LearningStore.from_environment")
    def test_load_learning_lessons_returns_balanced_five_lessons(self, learning_store_factory):
        learning_store_factory.return_value.lessons_for.return_value = [
            "lesson 1",
            "lesson 2",
            "lesson 3",
            "lesson 4",
            "lesson 5",
        ]

        lessons = hermes_mcp._load_learning_lessons("BTC")

        self.assertEqual(lessons, ["lesson 1", "lesson 2", "lesson 3", "lesson 4", "lesson 5"])
        learning_store_factory.return_value.lessons_for.assert_called_once_with("BTC", limit=5)

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

    @patch("tradingagents.integrations.hermes_mcp.get_provider_api_key", return_value="api-key")
    def test_start_analysis_queues_session_and_launches_worker(self, provider_key):
        launcher_calls = []

        def launch_worker(session_id, store):
            launcher_calls.append((session_id, store))
            return 4242

        with TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions")
            result = start_analysis(
                self.make_request().model_dump(mode="json"),
                store=store,
                worker_launcher=launch_worker,
            )
            session = store.load(result["data"]["session_id"])

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["data"]["status"], "queued")
        self.assertEqual(session.status, "queued")
        self.assertEqual(session.worker_pid, 4242)
        self.assertEqual(launcher_calls, [(session.session_id, store)])
        provider_key.assert_called_once_with("openai")

    @patch("tradingagents.integrations.hermes_mcp.get_provider_api_key", return_value="api-key")
    def test_start_analysis_persists_worker_launch_failure(self, provider_key):
        def fail_to_launch(session_id, store):
            raise OSError("launch failed")

        with TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions")
            result = start_analysis(
                self.make_request().model_dump(mode="json"),
                store=store,
                worker_launcher=fail_to_launch,
            )
            session = store.load(next(store.root.glob("*.json")).stem)

        self.assertEqual(result["ok"], False)
        self.assertEqual(result["error"]["code"], "WORKER_START_FAILED")
        self.assertEqual(session.status, "failed")
        self.assertEqual(session.error.code, "WORKER_START_FAILED")
        self.assertIsNotNone(session.completed_at)
        provider_key.assert_called_once_with("openai")

    @patch("tradingagents.integrations.hermes_mcp._cleanup_session_collections")
    @patch("tradingagents.integrations.hermes_mcp.get_provider_api_key", return_value="api-key")
    def test_queued_worker_persists_completed_graph_result(self, provider_key, cleanup):
        FakeGraph.instances = []

        with TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions")
            session_id = "hermes_0123456789abcdef"
            store.create(session_id, self.make_request(), status="queued")

            result = run_queued_analysis(session_id, store=store, graph_factory=FakeGraph)
            session = store.load(session_id)

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["data"]["status"], "completed")
        self.assertEqual(session.status, "completed")
        self.assertIsNotNone(session.started_at)
        self.assertEqual(session.worker_pid, os.getpid())
        self.assertEqual(session.result.processed_signal, "HOLD")
        cleanup.assert_called_once_with(session_id)
        provider_key.assert_called_once_with("openai")

    def test_result_lookup_marks_dead_worker_as_failed(self):
        with TemporaryDirectory() as temp_dir:
            store = SessionStore(Path(temp_dir) / "sessions")
            session_id = "hermes_0123456789abcdef"
            session = store.create(session_id, self.make_request())
            store.save(session.model_copy(update={"worker_pid": 4242}))

            with patch(
                "tradingagents.integrations.hermes_mcp._worker_is_alive",
                return_value=False,
            ):
                result = get_analysis_result_impl(session_id, store=store)
            persisted_session = store.load(session_id)

        self.assertEqual(result["ok"], True)
        self.assertEqual(result["data"]["session"]["status"], "failed")
        self.assertEqual(persisted_session.status, "failed")
        self.assertEqual(persisted_session.error.code, "WORKER_EXITED")

    def test_completed_async_session_can_create_paper_review_and_learning(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "hermes"
            store = SessionStore(root / "sessions")
            session_id = "hermes_0123456789abcdef"
            session = store.create(session_id, self.make_request(), status="queued")
            completed = session.model_copy(
                update={
                    "status": "completed",
                    "result": AnalysisResult(
                        reports={},
                        investment_plan="plan",
                        trader_investment_plan="trader plan",
                        final_trade_decision="FINAL TRANSACTION PROPOSAL: SELL",
                        processed_signal="SELL",
                    ),
                }
            )
            store.save(completed)

            result = review_paper_decision_impl(
                {"session_id": session_id, "review_date": "2026-07-29"},
                store=store,
                review_store=ReviewStore(root / "reviews"),
                learning_store=LearningStore(root / "memories"),
                price_reference_resolver=paired_price_references(),
                current_date=date(2026, 7, 29),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["review"]["action"], "SELL")
        self.assertEqual(result["data"]["review"]["verdict"], "correct")
        self.assertIn("Paper-trading research lesson", result["data"]["hermes_memory_entry"])

    def test_review_can_skip_legacy_learning_index(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "hermes"
            store = SessionStore(root / "sessions")
            session_id = "hermes_0123456789abcdef"
            session = store.create(session_id, self.make_request(), status="queued")
            store.save(
                session.model_copy(
                    update={
                        "status": "completed",
                        "result": AnalysisResult(
                            reports={},
                            investment_plan="plan",
                            trader_investment_plan="trader plan",
                            final_trade_decision="FINAL TRANSACTION PROPOSAL: BUY",
                            processed_signal="BUY",
                        ),
                    }
                )
            )
            learning_store = LearningStore(root / "memories")

            result = review_paper_decision_impl(
                {"session_id": session_id, "review_date": "2026-07-29"},
                store=store,
                review_store=ReviewStore(root / "reviews"),
                learning_store=learning_store,
                price_reference_resolver=paired_price_references(),
                current_date=date(2026, 7, 29),
                write_legacy_learning=False,
            )

        self.assertTrue(result["ok"])
        self.assertFalse(learning_store.root.exists())

    def test_review_uses_default_same_source_resolver(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "hermes"
            store = SessionStore(root / "sessions")
            session_id = "hermes_0123456789abcdef"
            session = store.create(session_id, self.make_request(), status="queued")
            store.save(
                session.model_copy(
                    update={
                        "status": "completed",
                        "result": AnalysisResult(
                            reports={},
                            investment_plan="plan",
                            trader_investment_plan="trader plan",
                            final_trade_decision="FINAL TRANSACTION PROPOSAL: BUY",
                            processed_signal="BUY",
                        ),
                    }
                )
            )
            references = [
                HistoricalUsdReference(date(2026, 7, 28), 100.0, "coinbase"),
                HistoricalUsdReference(date(2026, 7, 29), 110.0, "coinbase"),
            ]
            with patch(
                "tradingagents.integrations.hermes_mcp.resolve_historical_usd_references",
                return_value=references,
            ) as resolver:
                result = review_paper_decision_impl(
                    {"session_id": session_id, "review_date": "2026-07-29"},
                    store=store,
                    review_store=ReviewStore(root / "reviews"),
                    learning_store=LearningStore(root / "memories"),
                    current_date=date(2026, 7, 29),
                )

        self.assertTrue(result["ok"])
        self.assertEqual(
            (
                result["data"]["review"]["entry_price"]["source"],
                result["data"]["review"]["review_price"]["source"],
            ),
            ("coinbase", "coinbase"),
        )
        resolver.assert_called_once_with("BTCUSDT", [date(2026, 7, 28), date(2026, 7, 29)])

    def test_review_rejects_tomorrow_in_utc_even_when_local_date_has_advanced(self):
        class LateLocalDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 7, 30)

        with TemporaryDirectory() as temp_dir, patch(
            "tradingagents.integrations.hermes_mcp.date", LateLocalDate
        ), patch(
            "tradingagents.integrations.hermes_mcp.utc_now",
            return_value=datetime(2026, 7, 29, 23, 30, tzinfo=timezone.utc),
        ):
            root = Path(temp_dir) / "hermes"
            store = SessionStore(root / "sessions")
            session_id = "hermes_0123456789abcdef"
            session = store.create(session_id, self.make_request(), status="queued")
            store.save(
                session.model_copy(
                    update={
                        "status": "completed",
                        "result": AnalysisResult(
                            reports={},
                            investment_plan="plan",
                            trader_investment_plan="trader plan",
                            final_trade_decision="FINAL TRANSACTION PROPOSAL: SELL",
                            processed_signal="SELL",
                        ),
                    }
                )
            )
            result = review_paper_decision_impl(
                {"session_id": session_id, "review_date": "2026-07-30"},
                store=store,
                review_store=ReviewStore(root / "reviews"),
                learning_store=LearningStore(root / "memories"),
                price_reference_resolver=paired_price_references(),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "INVALID_REVIEW_REQUEST")

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
