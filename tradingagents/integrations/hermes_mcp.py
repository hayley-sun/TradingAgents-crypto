"""Hermes MCP health and persisted-session tools."""

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager, redirect_stdout
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import chromadb
import fcntl
from chromadb.config import Settings
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from pydantic import ConfigDict, ValidationError

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.dataflows.crypto_price_references import (
    resolve_historical_usd_references,
)
from tradingagents.graph import TradingAgentsGraph
from tradingagents.integrations.hermes_learning import (
    LearningStorageError,
    LearningStore,
    ReviewStorageError,
    ReviewStore,
    review_completed_session,
)
from tradingagents.integrations.hermes_report_learning import (
    ReportLearningConflict,
    ReportLearningError,
    ReportLearningStore,
    ReportReflectionRejected,
    submit_report_reflection as _persist_report_reflection,
)
from tradingagents.integrations.hermes_reports import (
    ReportArchiveConflict,
    ReportBatchActive,
    ReportBatchConflict,
    ReportBatchStorageError,
    ReportBatchStore,
)
from tradingagents.integrations.hermes_scheduled_reviews import (
    ScheduledReviewStorageError,
    ScheduledReviewStore,
)
from tradingagents.integrations.schemas import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisSession,
    DailyReportRequest,
    PriceReference,
    ReviewRequest,
    ReportReflection,
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


def _reject_self_referential_symlinks(path: Path) -> None:
    candidate = path.absolute()
    while candidate != candidate.parent:
        if candidate.is_symlink():
            target = Path(os.readlink(candidate))
            if not target.is_absolute():
                target = candidate.parent / target
            if target.absolute() == candidate:
                raise RuntimeError("symbolic link loop")
        candidate = candidate.parent


class SessionStore:
    """Filesystem-backed storage for opaque Hermes analysis sessions."""

    def __init__(self, root: Path):
        configured_root = Path(root).expanduser()
        _reject_self_referential_symlinks(configured_root)
        self.root = configured_root.resolve()

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

    def create(
        self, session_id: str, request: AnalysisRequest, status: str = "running"
    ) -> AnalysisSession:
        session = AnalysisSession(
            session_id=session_id,
            status=status,
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
    cryptocompare_key_available = bool(os.getenv("CRYPTOCOMPARE_API_KEY"))
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
            "cryptocompare_key_available": cryptocompare_key_available,
            "disclaimer": PAPER_TRADING_DISCLAIMER,
        }
    )


def _worker_is_alive(worker_pid: int) -> bool:
    try:
        os.kill(worker_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _worker_exited_session(session: AnalysisSession) -> AnalysisSession:
    return _failed_session(
        session,
        ToolError(
            code="WORKER_EXITED",
            message="The analysis worker exited before completing the session.",
            suggested_action="Start a new analysis session.",
        ),
    )


def reconcile_session_worker(
    session: AnalysisSession,
    store: SessionStore,
    worker_is_alive: Callable[[int], bool] | None = None,
    persist: bool = True,
) -> tuple[AnalysisSession, bool]:
    """Mark only tracked active sessions with a dead worker as failed."""
    if session.status not in {"queued", "running"} or session.worker_pid is None:
        return session, False

    liveness_check = worker_is_alive or _worker_is_alive
    if liveness_check(session.worker_pid):
        return session, False

    reconciled_session = _worker_exited_session(session)
    if persist:
        store.save(reconciled_session)
    return reconciled_session, True


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

    try:
        session, _ = reconcile_session_worker(session, active_store)
    except OSError:
        return failure(
            ToolError(
                code="SESSION_WRITE_FAILED",
                message="The analysis session could not be updated.",
                suggested_action="Verify that session storage is writable and try again.",
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


def _report_error(code: str, message: str, suggested_action: str) -> dict[str, Any]:
    return failure(
        ToolError(code=code, message=message, suggested_action=suggested_action)
    )


def _report_batch_store_from_environment() -> ReportBatchStore:
    results_dir = os.getenv("TRADINGAGENTS_RESULTS_DIR")
    base_dir = Path(results_dir) if results_dir else PROJECT_ROOT / "results"
    return ReportBatchStore(base_dir / "hermes" / "report_batches")


def _parse_report_trade_date(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("invalid trade date")
    return date.fromisoformat(value)


def _report_session_starter(
    request: AnalysisRequest,
    store: SessionStore,
    worker_launcher=None,
) -> str | ToolError:
    result = start_analysis(
        request.model_dump(mode="json"),
        store=store,
        worker_launcher=worker_launcher or launch_analysis_worker,
    )
    if result.get("ok"):
        return result["data"]["session_id"]
    try:
        return ToolError.model_validate(result["error"])
    except (KeyError, TypeError, ValidationError):
        return ToolError(
            code="REPORT_SUBMISSION_FAILED",
            message="The daily report analysis could not be submitted.",
            suggested_action="Inspect the safe analysis status and start a later batch.",
        )


def _report_summary_data(summary) -> dict[str, Any]:
    return {
        "state": summary.state,
        "items": [
            {
                "symbol": item.symbol,
                "session_id": item.session_id,
                "status": item.status,
                "processed_signal": item.result.processed_signal
                if item.result is not None
                else None,
                "final_trade_decision": item.result.final_trade_decision
                if item.result is not None
                else None,
                "error": item.error.model_dump(mode="json")
                if item.error is not None
                else None,
            }
            for item in summary.items
        ],
    }


def _previous_report_data(previous) -> dict[str, Any] | None:
    if previous is None:
        return None
    return {
        "trade_date": previous.trade_date.isoformat(),
        "items": [item.model_dump(mode="json") for item in previous.items],
    }


def start_daily_report_batch_impl(
    request_data: Mapping[str, Any],
    batch_store: ReportBatchStore | None = None,
    session_store: SessionStore | None = None,
    starter: Callable[[AnalysisRequest], str | ToolError] | None = None,
    worker_launcher=None,
) -> dict[str, Any]:
    try:
        request = DailyReportRequest.model_validate(request_data)
    except ValidationError:
        return _report_error(
            "INVALID_REPORT_REQUEST",
            "The daily report request is invalid.",
            "Use one ISO trade date, unique supported symbols and analysts, and valid model settings.",
        )

    try:
        active_batch_store = batch_store or _report_batch_store_from_environment()
        active_session_store = session_store or SessionStore.from_environment()
        active_starter = starter or (
            lambda analysis_request: _report_session_starter(
                analysis_request, active_session_store, worker_launcher
            )
        )
        batch = active_batch_store.create_or_load(request, active_starter)
    except ReportBatchConflict:
        return _report_error(
            "REPORT_BATCH_CONFLICT",
            "A daily report batch already exists for this date with different settings.",
            "Use the existing batch settings or create a batch for a different trade date.",
        )
    except ReportBatchStorageError:
        return _report_error(
            "REPORT_BATCH_UNREADABLE",
            "The daily report batch could not be read or written.",
            "Verify report batch storage and retry later.",
        )
    except (OSError, ValueError, ValidationError):
        return _report_error(
            "REPORT_BATCH_WRITE_FAILED",
            "The daily report batch could not be created.",
            "Verify report batch storage and retry later.",
        )

    return success(
        {
            "batch": batch.model_dump(mode="json"),
            "disclaimer": PAPER_TRADING_DISCLAIMER,
        }
    )


def get_daily_report_batch_impl(
    trade_date: str,
    batch_store: ReportBatchStore | None = None,
    session_store: SessionStore | None = None,
    session_loader: Callable[[str], AnalysisSession | None] | None = None,
) -> dict[str, Any]:
    try:
        requested_date = _parse_report_trade_date(trade_date)
        active_batch_store = batch_store or _report_batch_store_from_environment()
        batch = active_batch_store.load(requested_date)
        if batch is None:
            return _report_error(
                "REPORT_BATCH_NOT_FOUND",
                "No daily report batch exists for this trade date.",
                "Start a daily report batch for this trade date first.",
            )
        loader = session_loader or (session_store or SessionStore.from_environment()).load
        summary = active_batch_store.summarize(batch, loader)
        previous = active_batch_store.previous_snapshot(requested_date)
    except ReportBatchStorageError:
        return _report_error(
            "REPORT_BATCH_UNREADABLE",
            "The daily report batch could not be read.",
            "Verify report batch and session storage, then retry later.",
        )
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return _report_error(
            "INVALID_REPORT_REQUEST",
            "The daily report trade date is invalid.",
            "Use an ISO trade date in YYYY-MM-DD format.",
        )

    return success(
        {
            "batch": batch.model_dump(mode="json"),
            "summary": _report_summary_data(summary),
            "previous_report": _previous_report_data(previous),
            "disclaimer": PAPER_TRADING_DISCLAIMER,
        }
    )


def archive_daily_report_impl(
    trade_date: str,
    narrative: str,
    batch_store: ReportBatchStore | None = None,
    session_store: SessionStore | None = None,
    session_loader: Callable[[str], AnalysisSession | None] | None = None,
    schedule_store: ScheduledReviewStore | None = None,
) -> dict[str, Any]:
    try:
        requested_date = _parse_report_trade_date(trade_date)
        active_batch_store = batch_store or _report_batch_store_from_environment()
        batch = active_batch_store.load(requested_date)
        if batch is None:
            return _report_error(
                "REPORT_BATCH_NOT_FOUND",
                "No daily report batch exists for this trade date.",
                "Start a daily report batch for this trade date first.",
            )
        loader = session_loader or (session_store or SessionStore.from_environment()).load
        archive = active_batch_store.archive(
            batch, loader, narrative, scheduled_review_version=2
        )
        persisted_batch = active_batch_store.load(requested_date)
        if (
            persisted_batch is not None
            and persisted_batch.archive is not None
            and persisted_batch.archive.scheduled_review_version in (1, 2)
        ):
            active_schedule_store = (
                schedule_store
                if schedule_store is not None
                else ScheduledReviewStore.from_environment()
            )
            active_schedule_store.create_or_load(persisted_batch)
    except ReportBatchActive:
        return _report_error(
            "REPORT_BATCH_ACTIVE",
            "The daily report batch still has queued or running analyses.",
            "Wait for all sessions to reach a terminal state, then archive the report.",
        )
    except ReportArchiveConflict:
        return _report_error(
            "REPORT_ARCHIVE_CONFLICT",
            "A different immutable report archive already exists for this date.",
            "Use the existing archive and do not attempt to overwrite it.",
        )
    except ReportBatchStorageError:
        return _report_error(
            "REPORT_BATCH_UNREADABLE",
            "The daily report batch or archive could not be read or written.",
            "Verify report batch and archive storage, then retry later.",
        )
    except ScheduledReviewStorageError:
        return _report_error(
            "REVIEW_SCHEDULE_WRITE_FAILED",
            "The scheduled paper reviews could not be persisted.",
            "Verify scheduled-review storage and retry the same archive request.",
        )
    except (OSError, ValueError, ValidationError, json.JSONDecodeError):
        return _report_error(
            "REPORT_ARCHIVE_INVALID",
            "The daily report archive request is invalid.",
            "Use an ISO trade date and a non-empty report narrative.",
        )

    return success(
        {
            "batch_id": batch.batch_id,
            "filename": archive.path.name,
            "sha256": archive.sha256,
            "state": archive.state,
            "disclaimer": PAPER_TRADING_DISCLAIMER,
        }
    )


def _resolve_review_price_references(
    symbol: str, trade_date: date, review_date: date
) -> tuple[PriceReference, PriceReference]:
    references = resolve_historical_usd_references(
        symbol, [trade_date, review_date]
    )
    try:
        entry_price, review_price = tuple(
            PriceReference(
                date=reference.date,
                usd_price=reference.usd_price,
                source=reference.source,
            )
            for reference in references
        )
    except (TypeError, ValueError, ValidationError) as error:
        raise ValueError("historical USD references are unavailable") from error
    return entry_price, review_price


def _load_learning_lessons(symbol: str) -> list[str]:
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
        started_at=session.started_at,
        completed_at=utc_now(),
        worker_pid=session.worker_pid,
        request=session.request,
        error=error,
    )


def _worker_start_failed_session(session: AnalysisSession) -> AnalysisSession:
    return _failed_session(
        session,
        ToolError(
            code="WORKER_START_FAILED",
            message="The analysis worker could not be started.",
            suggested_action="Verify the MCP Python environment and try the analysis again.",
        ),
    )


def launch_analysis_worker(session_id: str, store: SessionStore) -> int:
    """Start a worker that survives an MCP stdio reconnect."""
    log_directory = store.root.parent / "logs"
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = log_directory / f"{session_id}.log"

    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tradingagents.integrations.hermes_analysis_worker",
                session_id,
            ],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return process.pid


def start_analysis(
    request_data: Mapping[str, Any],
    store: SessionStore | None = None,
    worker_launcher=launch_analysis_worker,
) -> dict[str, Any]:
    """Queue an analysis without blocking the MCP stdio process."""
    try:
        request = AnalysisRequest.model_validate(request_data)
    except (ValidationError, TypeError, ValueError):
        return _analysis_error(
            "INVALID_REQUEST",
            "The analysis request is invalid.",
            "Correct the request fields and try again.",
        )

    if request.llm_provider != "ollama" and not get_provider_api_key(request.llm_provider):
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
            "The analysis session could not be started.",
            "Verify the session storage configuration and try again.",
        )

    session_id = f"hermes_{uuid4().hex}"
    try:
        session = active_store.create(session_id, request, status="queued")
    except Exception:
        return _analysis_error(
            "SESSION_WRITE_FAILED",
            "The analysis session could not be started.",
            "Verify that session storage is writable and try again.",
        )

    try:
        worker_pid = worker_launcher(session_id, active_store)
    except OSError:
        try:
            active_store.save(_worker_start_failed_session(session))
        except Exception:
            pass
        return _analysis_error(
            "WORKER_START_FAILED",
            "The analysis worker could not be started.",
            "Verify the MCP Python environment and try the analysis again.",
        )

    persisted_session = active_store.load(session_id)
    if persisted_session is not None and persisted_session.status == "queued":
        persisted_session = persisted_session.model_copy(update={"worker_pid": worker_pid})
        active_store.save(persisted_session)

    return success(
        {
            "session_id": session_id,
            "status": persisted_session.status if persisted_session else "queued",
            "disclaimer": PAPER_TRADING_DISCLAIMER,
        }
    )


@contextmanager
def _analysis_file_lock(store: SessionStore):
    lock_path = store.root.parent / ".analysis.lock"
    store.ensure()
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _missing_provider_error() -> ToolError:
    return ToolError(
        code="MISSING_API_KEY",
        message="The selected LLM provider is not configured.",
        suggested_action="Configure an API key for the selected provider and try again.",
    )


def _run_analysis_session(
    session: AnalysisSession,
    store: SessionStore,
    graph_factory: type[TradingAgentsGraph] = TradingAgentsGraph,
    provider_key: str | None = None,
) -> dict[str, Any]:
    request = session.request
    if request.llm_provider != "ollama" and provider_key is None:
        provider_key = get_provider_api_key(request.llm_provider)
    if request.llm_provider != "ollama" and not provider_key:
        error = _missing_provider_error()
        try:
            store.save(_failed_session(session, error))
        except OSError:
            pass
        return failure(error)

    analysis_started = False
    try:
        graph_config = build_graph_config(
            DEFAULT_CONFIG,
            {
                "llm_provider": request.llm_provider,
                "api_key": provider_key or "",
                "quick_think_llm": request.quick_model,
                "deep_think_llm": request.deep_model,
                "research_depth": request.research_depth,
            },
            session.session_id,
        )
        graph_config["log_graph_states"] = False
        graph_config["hermes_review_lessons"] = _load_learning_lessons(request.symbol)
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
        completed_session = session.model_copy(
            update={
                "status": "completed",
                "completed_at": utc_now(),
                "result": result,
                "error": None,
            }
        )
        store.save(completed_session)
        return success(
            {
                "session_id": session.session_id,
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
            session.session_id,
            type(error).__name__,
        )
        try:
            store.save(_failed_session(session, analysis_error))
        except OSError:
            pass
        return failure(analysis_error)
    finally:
        if analysis_started:
            _cleanup_session_collections(session.session_id)


def run_queued_analysis(
    session_id: str,
    store: SessionStore | None = None,
    graph_factory: type[TradingAgentsGraph] = TradingAgentsGraph,
) -> dict[str, Any]:
    """Run a queued session in a detached worker process."""
    try:
        active_store = store if store is not None else SessionStore.from_environment()
        with _analysis_file_lock(active_store):
            session = active_store.load(session_id)
            if session is None:
                return _analysis_error(
                    "SESSION_NOT_FOUND",
                    "No analysis session exists for this session ID.",
                    "Check the session ID or start a new analysis session.",
                )
            if session.status != "queued":
                return _analysis_error(
                    "SESSION_NOT_QUEUED",
                    "The analysis session is not waiting to run.",
                    "Use get_analysis_result to inspect the existing session.",
                )
            running_session = session.model_copy(
                update={
                    "status": "running",
                    "started_at": utc_now(),
                    "worker_pid": os.getpid(),
                }
            )
            active_store.save(running_session)
            return _run_analysis_session(running_session, active_store, graph_factory)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError):
        return _analysis_error(
            "SESSION_UNREADABLE",
            "The stored analysis session could not be read.",
            "Verify the session storage configuration and try again.",
        )


def execute_analysis(
    request_data: Mapping[str, Any],
    store: SessionStore | None = None,
    graph_factory: type[TradingAgentsGraph] = TradingAgentsGraph,
) -> dict[str, Any]:
    """Run a TradingAgents analysis synchronously for internal callers."""
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
            return failure(_missing_provider_error())

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
        session = session.model_copy(
            update={"started_at": utc_now(), "worker_pid": os.getpid()}
        )
        active_store.save(session)
        with _analysis_file_lock(active_store):
            return _run_analysis_session(session, active_store, graph_factory, provider_key)
    except OSError:
        return _analysis_error(
            "SESSION_WRITE_FAILED",
            "The analysis session could not be started.",
            "Verify that session storage is writable and try again.",
        )


def review_paper_decision_impl(
    request_data: Mapping[str, Any],
    store: SessionStore | None = None,
    review_store: ReviewStore | None = None,
    learning_store: LearningStore | None = None,
    price_reference_resolver: Any = None,
    current_date: date | None = None,
    *,
    write_legacy_learning: bool = True,
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

    today = current_date or utc_now().date()
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
        ) if write_legacy_learning else None
    except _SESSION_STORE_CONSTRUCTION_ERRORS:
        return _review_error(
            "REVIEW_STORE_UNAVAILABLE",
            "Paper-decision review storage is currently unavailable.",
            "Verify the Hermes results directory and try again.",
        )

    resolver = price_reference_resolver or _resolve_review_price_references
    try:
        with _REVIEW_LOCK:
            with redirect_stdout(sys.stderr):
                review = review_completed_session(
                    session,
                    request.review_date,
                    resolver,
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
            "Historical USD reference price data is unavailable for this review.",
            "Verify the symbol and review date, then try again later.",
        )

    return success(
        {
            "review": review.model_dump(mode="json"),
            "hermes_memory_entry": review.hermes_memory_entry,
            "disclaimer": PAPER_TRADING_DISCLAIMER,
        }
    )


def submit_report_reflection_impl(
    request_data: Mapping[str, Any],
    session_store: SessionStore | None = None,
    report_store: ReportLearningStore | None = None,
    learning_store: LearningStore | None = None,
) -> dict[str, Any]:
    """Persist one validated report reflection behind a strict, safe MCP boundary."""
    if not isinstance(request_data, Mapping):
        return _report_error(
            "INVALID_REPORT_REFLECTION",
            "The report reflection request is invalid.",
            "Use only session_id, expected_revision, and reflection fields.",
        )
    allowed = {"session_id", "expected_revision", "reflection"}
    if set(request_data) != allowed:
        return _report_error(
            "INVALID_REPORT_REFLECTION",
            "The report reflection request is invalid.",
            "Use only session_id, expected_revision, and reflection fields.",
        )
    session_id = request_data.get("session_id")
    expected_revision = request_data.get("expected_revision")
    reflection_data = request_data.get("reflection")
    if (
        not is_valid_session_id(session_id)
        or isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or not 1 <= expected_revision <= 3
        or not isinstance(reflection_data, Mapping)
    ):
        return _report_error(
            "INVALID_REPORT_REFLECTION",
            "The report reflection request is invalid.",
            "Use a valid Hermes session ID, revision, and reflection payload.",
        )

    try:
        ReportReflection.model_validate(reflection_data)
    except ValidationError as validation_error:
        if any(
            item.get("type") == "extra_forbidden"
            for item in validation_error.errors()
        ):
            return _report_error(
                "INVALID_REPORT_REFLECTION",
                "The report reflection request is invalid.",
                "Use only the documented reflection fields.",
            )

    try:
        active_session_store = session_store or SessionStore.from_environment()
    except _SESSION_STORE_CONSTRUCTION_ERRORS:
        return _report_error(
            "SESSION_STORE_UNAVAILABLE",
            "The analysis session could not be read.",
            "Verify the session storage configuration and try again.",
        )
    try:
        session = active_session_store.load(session_id)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError):
        return _report_error(
            "SESSION_UNREADABLE",
            "The stored analysis session could not be read.",
            "Retry later or start a new analysis session.",
        )
    if session is None:
        return _report_error(
            "SESSION_NOT_FOUND",
            "No analysis session exists for this session ID.",
            "Check the session ID or start a new analysis.",
        )
    if session.status != "completed" or session.result is None:
        return _report_error(
            "SESSION_NOT_COMPLETED",
            "Only completed analysis sessions can receive reflections.",
            "Wait for a completed paper-trading analysis before reflecting.",
        )

    try:
        active_report_store = report_store or ReportLearningStore.from_environment()
        active_learning_store = learning_store or LearningStore.from_environment()
    except (OSError, RuntimeError, ValueError):
        return _report_error(
            "REPORT_STORE_UNAVAILABLE",
            "Report reflection storage is currently unavailable.",
            "Verify the Hermes results directory and try again.",
        )
    try:
        updated = _persist_report_reflection(
            active_report_store,
            active_learning_store,
            session,
            expected_revision,
            reflection_data,
        )
    except ReportReflectionRejected as error:
        return _report_error(
            (
                "INVALID_REPORT_REFLECTION"
                if error.error_code == "REFLECTION_SCHEMA_INVALID"
                else error.error_code
            ),
            "The report reflection did not pass bounded validation.",
            "Correct the reflection fields and try again.",
        )
    except ReportLearningConflict:
        return _report_error(
            "REPORT_REFLECTION_STALE",
            "The report reflection revision is stale or unavailable.",
            "Refresh the pending report reflection and retry the expected revision.",
        )
    except (OSError, ValueError, ReportLearningError, LearningStorageError):
        return _report_error(
            "REPORT_REFLECTION_STORAGE_FAILED",
            "The report reflection could not be persisted.",
            "Verify report and learning storage, then retry the same request.",
        )

    revision = updated.revisions[expected_revision - 1]
    return success(
        {
            "session_id": updated.session_id,
            "revision": expected_revision,
            "reflection_state": revision.reflection_state,
            "memory_state": revision.memory_state,
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
    """Queue a paper-trading crypto analysis through TradingAgents."""
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
    return start_analysis(request_data)


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


@MCP.tool()
def submit_report_reflection(
    session_id: str,
    expected_revision: int,
    reflection: Mapping[str, Any],
    **unknown_fields: Any,
) -> dict[str, Any]:
    """Persist one bounded retrospective report reflection."""
    request_data = {
        "session_id": session_id,
        "expected_revision": expected_revision,
        "reflection": reflection,
        **unknown_fields,
    }
    return submit_report_reflection_impl(request_data)


@MCP.tool()
def start_daily_report_batch(
    trade_date: str,
    symbols: list[str],
    analysts: list[str],
    research_depth: int,
    llm_provider: str,
    quick_model: str,
    deep_model: str,
    **unknown_fields: Any,
) -> dict[str, Any]:
    """Start or retrieve one idempotent daily paper-trading research batch."""
    request_data = {
        "trade_date": trade_date,
        "symbols": symbols,
        "analysts": analysts,
        "research_depth": research_depth,
        "llm_provider": llm_provider,
        "quick_model": quick_model,
        "deep_model": deep_model,
    }
    request_data.update(unknown_fields)
    return start_daily_report_batch_impl(request_data)


@MCP.tool()
def get_daily_report_batch(
    trade_date: str, **unknown_fields: Any
) -> dict[str, Any]:
    """Read one daily report batch and safe per-symbol completion state."""
    if unknown_fields:
        return _report_error(
            "INVALID_REPORT_REQUEST",
            "The daily report request is invalid.",
            "Use only an ISO trade date in YYYY-MM-DD format.",
        )
    return get_daily_report_batch_impl(trade_date)


@MCP.tool()
def archive_daily_report(
    trade_date: str, narrative: str, **unknown_fields: Any
) -> dict[str, Any]:
    """Archive one terminal daily report batch as immutable Markdown."""
    if unknown_fields:
        return _report_error(
            "INVALID_REPORT_REQUEST",
            "The daily report request is invalid.",
            "Use only an ISO trade date and a report narrative.",
        )
    return archive_daily_report_impl(trade_date, narrative)


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


class _SubmitReportReflectionArguments(ArgModelBase):
    """Preserve raw reflection fields so the strict boundary can reject extras."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    session_id: str
    expected_revision: Any
    reflection: dict[str, Any]

    def model_dump_one_level(self) -> dict[str, Any]:
        return self.model_dump()


class _DailyReportBatchArguments(ArgModelBase):
    """Preserve raw batch fields so DailyReportRequest rejects unknown inputs."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    trade_date: str
    symbols: list[str]
    analysts: list[str]
    research_depth: int
    llm_provider: str
    quick_model: str
    deep_model: str

    def model_dump_one_level(self) -> dict[str, Any]:
        return self.model_dump()


class _DailyReportLookupArguments(ArgModelBase):
    """Preserve raw lookup fields so the wrapper can reject extras."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    trade_date: str

    def model_dump_one_level(self) -> dict[str, Any]:
        return self.model_dump()


class _DailyReportArchiveArguments(ArgModelBase):
    """Preserve raw archive fields so the wrapper can reject extras."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    trade_date: str
    narrative: str

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


def _configure_submit_report_reflection_tool() -> None:
    tool = MCP._tool_manager.get_tool("submit_report_reflection")
    if tool is None:
        raise RuntimeError("submit_report_reflection tool registration is unavailable")

    tool.fn_metadata.arg_model = _SubmitReportReflectionArguments
    tool.parameters = _SubmitReportReflectionArguments.model_json_schema()
    reflection_schema = ReportReflection.model_json_schema()
    tool.parameters.setdefault("$defs", {}).update(reflection_schema.pop("$defs", {}))
    tool.parameters["properties"]["reflection"] = reflection_schema
    tool.parameters["properties"]["expected_revision"] = {"type": "integer"}
    tool.parameters["additionalProperties"] = False


def _configure_daily_report_tool(name: str, arguments: type[ArgModelBase]) -> None:
    tool = MCP._tool_manager.get_tool(name)
    if tool is None:
        raise RuntimeError(f"{name} tool registration is unavailable")

    tool.fn_metadata.arg_model = arguments
    tool.parameters = arguments.model_json_schema()
    tool.parameters["additionalProperties"] = False


_configure_analyze_crypto_tool()
_configure_review_paper_decision_tool()
_configure_submit_report_reflection_tool()
_configure_daily_report_tool("start_daily_report_batch", _DailyReportBatchArguments)
_configure_daily_report_tool("get_daily_report_batch", _DailyReportLookupArguments)
_configure_daily_report_tool("archive_daily_report", _DailyReportArchiveArguments)


if __name__ == "__main__":
    MCP.run(transport="stdio")
