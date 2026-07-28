"""Hermes MCP health and persisted-session tools."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.integrations.schemas import (
    AnalysisRequest,
    AnalysisSession,
    ToolError,
    is_valid_session_id,
    utc_now,
)
from tradingagents.llm_providers import API_KEY_ENV_VARS


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PAPER_TRADING_DISCLAIMER = (
    "Research and paper-trading output only. Do not use this output to place real trades."
)
MCP = FastMCP("tradingagents_crypto")


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
    active_store = store or SessionStore.from_environment()
    store_writable = _store_is_writable(active_store)
    provider_api_keys = {
        provider: bool(os.getenv(environment_variable))
        for provider, environment_variable in API_KEY_ENV_VARS.items()
        if environment_variable
    }
    has_keyed_provider = any(provider_api_keys.values())
    coingecko_api_key_configured = any(
        bool(os.getenv(environment_variable))
        for environment_variable in (
            "COINGECKO_API_KEY",
            "COINGECKO_DEMO_API_KEY",
            "COINGECKO_PRO_API_KEY",
        )
    )
    return success(
        {
            "status": "ready" if store_writable and has_keyed_provider else "degraded",
            "project_root": str(PROJECT_ROOT),
            "session_directory": str(active_store.root),
            "configured_providers": list(API_KEY_ENV_VARS),
            "default_llm_provider": DEFAULT_CONFIG["llm_provider"],
            "provider_api_keys": provider_api_keys,
            "finnhub_api_key_configured": bool(os.getenv("FINNHUB_API_KEY")),
            "coingecko_api_key_configured": coingecko_api_key_configured,
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

    active_store = store or SessionStore.from_environment()
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


@MCP.tool()
def health_check() -> dict[str, Any]:
    """Report non-sensitive Hermes MCP configuration and storage status."""
    return health_check_impl()


@MCP.tool()
def get_analysis_result(session_id: str) -> dict[str, Any]:
    """Return a persisted Hermes analysis session by opaque session ID."""
    return get_analysis_result_impl(session_id)


if __name__ == "__main__":
    MCP.run(transport="stdio")
