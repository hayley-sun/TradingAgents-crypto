"""Hermes MCP health and persisted-session tools."""

import json
import logging
import os
import sys
import tempfile
import threading
from collections.abc import Mapping
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import chromadb
from chromadb.config import Settings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from pydantic import ConfigDict, ValidationError

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dataflows.coingecko_utils import get_crypto_historical_usd_price
from tradingagents.graph import TradingAgentsGraph
from tradingagents.integrations.hermes_learning import (
    LearningStorageError,
    LearningStore,
    ReviewStorageError,
    ReviewStore,
    review_completed_session,
)
from tradingagents.integrations.schemas import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisSession,
    ReviewRequest,
    ToolError,
    is_valid_session_id,
    utc_now,
)
from tradingagents.llm_providers import (
    API_KEY_ENV_VARS,
    build_graph_config,
    get_provider_api_key,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAPER_TRADING_DISCLAIMER = (
    "Research and paper-trading output only. Do not use this output to place real trades."
)
MCP = FastMCP("tradingagents_crypto")
_SESSION_STORE_CONSTRUCTION_ERRORS = (OSError, RuntimeError, ValueError)
_ANALYSIS_LOCK = threading.Lock()
_REVIEW_LOCK = threading.Lock()
LOGGER = logging.getLogger(__name__)
SESSION_MEMORY_COLLECTION_BASE_NAMES = (
    "bull_memory",
    "bear_memory",
    "trader_memory",
    "invest_judge_memory",
    "risk_manager_memory",
)
LOCAL_LLM_PROVIDERS = ("ollama",)


def success(data: Any) -> dict[str, Any]:
    """Wrap successful tool data in the Hermes JSON envelope."""
    return {"ok": True, "data": data}


def failure(error: ToolError) -> dict[str, Any]:
    """Wrap a schema-validated error in the Hermes JSON envelope."""
    return {"ok": False, "error": error.model_dump(mode="json")}


class SessionStore:
    """Filesystem-backed storage for opaque Hermes analysis sessions."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    @classmethod
    def from_environment(cls) -> "SessionStore":
        results_dir = os.getenv("TRADINGAGENTS_RESULTS_DIR")
        base_dir = Path(results_dir) if results_dir else PROJECT_ROOT / "results"
        return cls(base_dir / "hermes" / "sessions")

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, session_id: str) -> Path:
        if not is_valid_session_id(session_id):
            raise ValueError("invalid session id")
        return self.root / f"{session_id}.json"

    def create(self, session_id: str, request: AnalysisRequest) -> AnalysisSession:
        session = AnalysisSession(
            session_id=session_id,
            status="running",
            created_at=utc_now(),
            request=request,
        )
        self.save(session)
        return session

    def save(self, session: AnalysisSession) -> None:
        destination = self.path_for(session.session_id)
        self.ensure()
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="ascii",
                dir=self.root,
                prefix=f".{session.session_id}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(session.model_dump(mode="json"), temporary_file, indent=2, ensure_ascii=True)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def load(self, session_id: str) -> AnalysisSession | None:
        path = self.path_for(session_id)
        if not path.exists():
            return None
        with path.open(encoding="ascii") as session_file:
            return AnalysisSession.model_validate(json.load(session_file))


def _store_is_writable(store: SessionStore) -> bool:
    try:
        store.ensure()
        with tempfile.NamedTemporaryFile(dir=store.root, prefix=".health-", suffix=".tmp"):
            pass
    except OSError:
        return False
    return True


def health_check_impl(store: SessionStore | None = None) -> dict[str, Any]:
    """Return non-sensitive MCP configuration and storage status."""
    try:
        active_store = store or SessionStore.from_environment()
    except _SESSION_STORE_CONSTRUCTION_ERRORS:
        return failure(
            ToolError(
                code="SESSION_STORE_UNAVAILABLE",
                message="Session storage is currently unavailable.",
                suggested_action="Verify the session storage configuration and try again.",
            )
        )

    store_writable = _store_is_writable(active_store)
    llm_provider_key_available = {
        provider: bool(os.getenv(environment_variable))
        for provider, environment_variable in API_KEY_ENV_VARS.items()
        if environment_variable
    }
    configured_llm_providers = sorted(
        set(LOCAL_LLM_PROVIDERS)
        | {
            provider
            for provider, key_available in llm_provider_key_available.items()
            if key_available
        }
    )
    coingecko_key_available = any(
        bool(os.getenv(environment_variable))
        for environment_variable in (
            "COINGECKO_DEMO_API_KEY",
            "COINGECKO_PRO_API_KEY",
        )
    )
    return success(
        {
            "status": "ready" if store_writable and configured_llm_providers else "degraded",
            "project_dir": str(PROJECT_ROOT),
            "session_store": str(active_store.root),
            "session_store_writable": store_writable,
            "llm_provider_key_available": llm_provider_key_available,
            "configured_llm_providers": configured_llm_providers,
            "finnhub_key_available": bool(os.getenv("FINNHUB_API_KEY")),
            "coingecko_key_available": coingecko_key_available,
            "disclaimer": PAPER_TRADING_DISCLAIMER,
        }
    )


def get_analysis_result_impl(
    session_id: str, store: SessionStore | None = None
) -> dict[str, Any]:
    """Load a persisted Hermes analysis session without invoking providers."""
    if not is_valid_session_id(session_id):
        return failure(
            ToolError(
                code="INVALID_SESSION_ID",
                message="The session ID is not a valid Hermes opaque identifier.",
                suggested_action="Use the Hermes session ID returned when the analysis was created.",
            )
        )

    try:
        active_store = store or SessionStore.from_environment()
    except _SESSION_STORE_CONSTRUCTION_ERRORS:
        return failure(
            ToolError(
                code="SESSION_UNREADABLE",
                message="The stored analysis session could not be read.",
                suggested_action="Verify the session storage configuration and try again.",
            )
        )

    try:
        session = active_store.load(session_id)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError):
        return failure(
            ToolError(
                code="SESSION_UNREADABLE",
                message="The stored analysis session could not be read.",
                suggested_action="Retry later or start a new analysis session.",
            )
        )

    if session is None:
        return failure(
            ToolError(
                code="SESSION_NOT_FOUND",
                message="No analysis session exists for this session ID.",
                suggested_action="Check the session ID or start a new analysis session.",
            )
        )

    return success(
        {
            "session": session.model_dump(mode="json"),
            "disclaimer": PAPER_TRADING_DISCLAIMER,
        }
    )


def _analysis_error(code: str, message: str, suggested_action: str) -> dict[str, Any]:
    return failure(
        ToolError(code=code, message=message, suggested_action=suggested_action)
    )


def _review_error(code: str, message: str, suggested_action: str) -> dict[str, Any]:
    return failure(
        ToolError(code=code, message=message, suggested_action=suggested_action)
    )


def _load_review_lessons(symbol: str) -> list[str]:
    try:
        return LearningStore.from_environment().lessons_for(symbol, limit=5)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError, ValidationError) as error:
        LOGGER.warning(
            "hermes_learning_load_failed symbol=%s exception_class=%s",
            symbol,
            type(error).__name__,
        )
        return []


def _cleanup_session_collections(session_id: str) -> None:
    """Delete in-memory Chroma collections owned by a completed Hermes session."""
    try:
        chroma_client = chromadb.Client(Settings(allow_reset=True))
        owned_collection_names = {
            f"{base_name}_{session_id}"
            for base_name in SESSION_MEMORY_COLLECTION_BASE_NAMES
        }
        for collection in chroma_client.list_collections():
            collection_name = getattr(collection, "name", collection)
            if collection_name in owned_collection_names:
                chroma_client.delete_collection(name=collection_name)
    except Exception:
        return


def _failed_session(session: AnalysisSession, error: ToolError) -> AnalysisSession:
    return AnalysisSession(
        session_id=session.session_id,
        status="failed",
        created_at=session.created_at,
        completed_at=utc_now(),
        request=session.request,
        error=error,
    )


def execute_analysis(
    request_data: Mapping[str, Any],
    store: SessionStore | None = None,
    graph_factory: type[TradingAgentsGraph] = TradingAgentsGraph,
) -> dict[str, Any]:
    """Run a TradingAgents analysis and persist its sanitized Hermes result."""
    try:
        request = AnalysisRequest.model_validate(request_data)
    except (ValidationError, TypeError, ValueError):
        return _analysis_error(
            "INVALID_REQUEST",
            "The analysis request is invalid.",
            "Correct the request fields and try again.",
        )

    provider_key = ""
    if request.llm_provider != "ollama":
        provider_key = get_provider_api_key(request.llm_provider)
        if not provider_key:
            return _analysis_error(
                "MISSING_API_KEY",
                "The selected LLM provider is not configured.",
                "Configure an API key for the selected provider and try again.",
            )

    try:
        active_store = store if store is not None else SessionStore.from_environment()
    except _SESSION_STORE_CONSTRUCTION_ERRORS:
        return _analysis_error(
            "SESSION_STORE_UNAVAILABLE",
            "Session storage is currently unavailable.",
            "Verify the session storage configuration and try again.",
        )

    session_id = f"hermes_{uuid4().hex}"
    try:
        session = active_store.create(session_id, request)
    except Exception:
        return _analysis_error(
            "SESSION_WRITE_FAILED",
            "The analysis session could not be started.",
            "Verify that session storage is writable and try again.",
        )

    analysis_started = False
    try:
        with _ANALYSIS_LOCK:
            with redirect_stdout(sys.stderr):
                graph_config = build_graph_config(
                    DEFAULT_CONFIG,
                    {
                        "llm_provider": request.llm_provider,
                        "api_key": provider_key,
                        "quick_think_llm": request.quick_model,
                        "deep_think_llm": request.deep_model,
                        "research_depth": request.research_depth,
                    },
                    session_id,
                )
                graph_config["log_graph_states"] = False
                graph_config["hermes_review_lessons"] = _load_review_lessons(
                    request.symbol
                )
                analysis_started = True
                graph = graph_factory(
                    selected_analysts=request.analysts,
                    debug=False,
                    config=graph_config,
                )
                final_state, processed_signal = graph.propagate(
                    request.symbol, request.trade_date.isoformat()
                )

        result = AnalysisResult(
            reports={
                "market": final_state["market_report"],
                "sentiment": final_state["sentiment_report"],
                "news": final_state["news_report"],
                "fundamentals": final_state["fundamentals_report"],
            },
            investment_plan=final_state["investment_plan"],
            trader_investment_plan=final_state["trader_investment_plan"],
            final_trade_decision=final_state["final_trade_decision"],
            processed_signal=processed_signal,
        )
        completed_session = AnalysisSession(
            session_id=session.session_id,
            status="completed",
            created_at=session.created_at,
            completed_at=utc_now(),
            request=request,
            result=result,
        )
        active_store.save(completed_session)
        return success(
            {
                "session_id": session_id,
                "status": "completed",
                "processed_signal": result.processed_signal,
                "final_trade_decision": result.final_trade_decision,
                "disclaimer": PAPER_TRADING_DISCLAIMER,
            }
        )
    except Exception as error:
        analysis_error = ToolError(
            code="ANALYSIS_FAILED",
            message="The analysis could not be completed.",
            suggested_action="Try the analysis again later.",
        )
        LOGGER.error(
            "hermes_analysis_failed session_id=%s exception_class=%s",
            session_id,
            type(error).__name__,
        )
        try:
            active_store.save(_failed_session(session, analysis_error))
        except Exception:
            pass
        return failure(analysis_error)
    finally:
        if analysis_started:
            _cleanup_session_collections(session_id)


def review_paper_decision_impl(
    request_data: Mapping[str, Any],
    store: SessionStore | None = None,
    review_store: ReviewStore | None = None,
    learning_store: LearningStore | None = None,
    price_lookup: Any = None,
    current_date: date | None = None,
) -> dict[str, Any]:
    """Review one completed paper-trading decision without an LLM call."""
    try:
        request = ReviewRequest.model_validate(request_data)
    except (ValidationError, TypeError, ValueError):
        return _review_error(
            "INVALID_REVIEW_REQUEST",
            "The paper-decision review request is invalid.",
            "Use a completed Hermes session ID and a later ISO review date.",
        )

    try:
        active_store = store if store is not None else SessionStore.from_environment()
    except _SESSION_STORE_CONSTRUCTION_ERRORS:
        return _review_error(
            "SESSION_UNREADABLE",
            "The stored analysis session could not be read.",
            "Verify the session storage configuration and try again.",
        )

    try:
        session = active_store.load(request.session_id)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError):
        return _review_error(
            "SESSION_UNREADABLE",
            "The stored analysis session could not be read.",
            "Retry later or start a new analysis session.",
        )

    if session is None:
        return _review_error(
            "SESSION_NOT_FOUND",
            "No analysis session exists for this session ID.",
            "Check the session ID or start a new analysis.",
        )
    if session.status != "completed" or session.result is None:
        return _review_error(
            "SESSION_NOT_COMPLETED",
            "Only completed analysis sessions can be reviewed.",
            "Wait for a completed paper-trading analysis before reviewing it.",
        )

    today = current_date or date.today()
    if request.review_date <= session.request.trade_date or request.review_date > today:
        return _review_error(
            "INVALID_REVIEW_REQUEST",
            "The review date must be after the trade date and not in the future.",
            "Choose a completed UTC calendar date after the original trade date.",
        )

    try:
        active_review_store = (
            review_store if review_store is not None else ReviewStore.from_environment()
        )
        active_learning_store = (
            learning_store
            if learning_store is not None
            else LearningStore.from_environment()
        )
    except _SESSION_STORE_CONSTRUCTION_ERRORS:
        return _review_error(
            "REVIEW_STORE_UNAVAILABLE",
            "Paper-decision review storage is currently unavailable.",
            "Verify the Hermes results directory and try again.",
        )

    lookup = price_lookup or get_crypto_historical_usd_price
    try:
        with _REVIEW_LOCK:
            with redirect_stdout(sys.stderr):
                review = review_completed_session(
                    session,
                    request.review_date,
                    lookup,
                    active_review_store,
                    active_learning_store,
                    current_date=today,
                )
    except LearningStorageError:
        return _review_error(
            "LEARNING_WRITE_FAILED",
            "The paper-decision review was saved but learning could not be updated.",
            "Verify learning storage, then repeat the same review request to repair it.",
        )
    except ReviewStorageError:
        return _review_error(
            "REVIEW_WRITE_FAILED",
            "The paper-decision review could not be persisted.",
            "Verify review storage and try again.",
        )
    except (OSError, json.JSONDecodeError, ValidationError):
        return _review_error(
            "REVIEW_WRITE_FAILED",
            "The paper-decision review could not be persisted.",
            "Verify review and learning storage, then repeat the same request.",
        )
    except ValueError:
        return _review_error(
            "PRICE_DATA_UNAVAILABLE",
            "CoinGecko USD reference price data is unavailable for this review.",
            "Verify the symbol and review date, then try again later.",
        )

    return success(
        {
            "review": review.model_dump(mode="json"),
            "hermes_memory_entry": review.hermes_memory_entry,
            "disclaimer": PAPER_TRADING_DISCLAIMER,
        }
    )


@MCP.tool()
def health_check() -> dict[str, Any]:
    """Report non-sensitive Hermes MCP configuration and storage status."""
    return health_check_impl()


@MCP.tool()
def get_analysis_result(session_id: str) -> dict[str, Any]:
    """Return a persisted Hermes analysis session by opaque session ID."""
    return get_analysis_result_impl(session_id)


@MCP.tool()
def analyze_crypto(
    symbol: str,
    trade_date: str,
    analysts: list[str],
    research_depth: int,
    llm_provider: str,
    quick_model: str,
    deep_model: str,
    **unknown_fields: Any,
) -> dict[str, Any]:
    """Run a paper-trading crypto analysis through TradingAgents."""
    request_data = {
        "symbol": symbol,
        "trade_date": trade_date,
        "analysts": analysts,
        "research_depth": research_depth,
        "llm_provider": llm_provider,
        "quick_model": quick_model,
        "deep_model": deep_model,
    }
    request_data.update(unknown_fields)
    return execute_analysis(request_data)


@MCP.tool()
def review_paper_decision(
    session_id: str,
    review_date: str,
    **unknown_fields: Any,
) -> dict[str, Any]:
    """Review a completed paper-trading decision using historical USD references."""
    request_data = {"session_id": session_id, "review_date": review_date}
    request_data.update(unknown_fields)
    return review_paper_decision_impl(request_data)


class _AnalyzeCryptoArguments(ArgModelBase):
    """Preserve raw flat MCP fields so AnalysisRequest can reject unknown inputs."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    symbol: str
    trade_date: str
    analysts: list[str]
    research_depth: int
    llm_provider: str
    quick_model: str
    deep_model: str

    def model_dump_one_level(self) -> dict[str, Any]:
        return self.model_dump()


class _ReviewPaperDecisionArguments(ArgModelBase):
    """Preserve raw review fields so ReviewRequest can reject unknown inputs."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    session_id: str
    review_date: str

    def model_dump_one_level(self) -> dict[str, Any]:
        return self.model_dump()


def _configure_analyze_crypto_tool() -> None:
    tool = MCP._tool_manager.get_tool("analyze_crypto")
    if tool is None:
        raise RuntimeError("analyze_crypto tool registration is unavailable")

    tool.fn_metadata.arg_model = _AnalyzeCryptoArguments
    tool.parameters = _AnalyzeCryptoArguments.model_json_schema()
    tool.parameters["additionalProperties"] = False


def _configure_review_paper_decision_tool() -> None:
    tool = MCP._tool_manager.get_tool("review_paper_decision")
    if tool is None:
        raise RuntimeError("review_paper_decision tool registration is unavailable")

    tool.fn_metadata.arg_model = _ReviewPaperDecisionArguments
    tool.parameters = _ReviewPaperDecisionArguments.model_json_schema()
    tool.parameters["additionalProperties"] = False


_configure_analyze_crypto_tool()
_configure_review_paper_decision_tool()


if __name__ == "__main__":
    MCP.run(transport="stdio")
