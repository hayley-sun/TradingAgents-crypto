# Hermes MCP Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose a safe, local stdio MCP bridge so Hermes Agent can health-check TradingAgents Crypto, run one paper-trading analysis, and retrieve its persisted result.

**Architecture:** Hermes launches `tradingagents.integrations.hermes_mcp` as a same-host stdio subprocess. The adapter validates a flat MCP request with Pydantic, builds a one-run TradingAgents configuration through the existing `build_graph_config`, serializes graph execution because the existing dataflow and toolkit configuration is process-global, and writes versioned JSON session files below `results/hermes/sessions`. Hermes is the orchestrator only; TradingAgents remains the analysis executor and no order-placement tool is added.

**Tech Stack:** Python 3.10+ (required by the current MCP Python SDK), MCP Python SDK (`FastMCP`), Pydantic v2 (already installed transitively), existing LangGraph TradingAgents runtime, `unittest`, Hermes Agent stdio MCP support.

---

## Scope And Boundaries

This plan implements the three tools required to prove the bridge works:

| MCP tool | Contract |
| --- | --- |
| `health_check` | Report safe runtime readiness without returning secret values. |
| `analyze_crypto` | Run one synchronous, paper-trading research analysis and persist a versioned session. |
| `get_analysis_result` | Return one persisted session by its opaque identifier. |

`list_analysis_sessions`, `compare_crypto`, paper-decision review, Hermes memory writes, cron jobs, and message delivery remain out of this change. The session store is deliberately structured to support those later phases, but no public MCP tool exposes them yet. A normal analysis can take several minutes; Hermes gets a 900-second per-tool timeout and only one analysis can run in an MCP process at a time.

## File Structure

| File | Responsibility |
| --- | --- |
| `requirements_hermes.txt` | The additive MCP SDK dependency, kept separate from the Web/CLI requirements. |
| `tradingagents/integrations/__init__.py` | Marks the integration package without importing the MCP server as a side effect. |
| `tradingagents/integrations/schemas.py` | Input validation, session/result/error schemas, opaque-id validation, and supported option constants. |
| `tradingagents/integrations/hermes_mcp.py` | FastMCP server, atomic JSON persistence, safe readiness check, graph execution, cleanup, and tool registration. |
| `tests/test_hermes_schemas.py` | Schema normalization and invalid-input coverage. |
| `tests/test_hermes_mcp.py` | Store persistence, readiness, result lookup, and mocked graph execution coverage. |
| `docs/hermes_integration.md` | Cloud-host installation, `~/.hermes/config.yaml` configuration, verification, security, and rollback steps. |

### Task 1: Add The Integration Contract And Dependency

**Files:**
- Create: `requirements_hermes.txt`
- Create: `tradingagents/integrations/__init__.py`
- Create: `tradingagents/integrations/schemas.py`
- Test: `tests/test_hermes_schemas.py`

- [ ] **Step 1: Create the failing schema tests.**

```python
# tests/test_hermes_schemas.py
import unittest
from datetime import date

from pydantic import ValidationError

from tradingagents.integrations.schemas import AnalysisRequest, is_valid_session_id


class HermesSchemaTest(unittest.TestCase):
    def test_analysis_request_normalizes_symbol_and_provider(self):
        request = AnalysisRequest(
            symbol=" btc ",
            trade_date="2026-07-28",
            analysts=["market", "news"],
            research_depth=3,
            llm_provider="DeepSeek",
            quick_model="deepseek-v4-flash",
            deep_model="deepseek-v4-pro",
        )

        self.assertEqual(request.symbol, "BTC")
        self.assertEqual(request.trade_date, date(2026, 7, 28))
        self.assertEqual(request.llm_provider, "deepseek")

    def test_analysis_request_rejects_invalid_options(self):
        invalid_request = {
            "symbol": "BTC",
            "trade_date": "2026-07-28",
            "analysts": ["unsupported"],
            "research_depth": 2,
            "llm_provider": "unknown",
            "quick_model": "quick",
            "deep_model": "deep",
        }

        with self.assertRaises(ValidationError):
            AnalysisRequest(**invalid_request)

    def test_session_ids_are_opaque_and_cannot_be_paths(self):
        self.assertTrue(is_valid_session_id("hermes_0123456789abcdef"))
        self.assertFalse(is_valid_session_id("../../results/hermes"))
        self.assertFalse(is_valid_session_id(""))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify that the integration module is absent.**

Run:

```bash
.venv/bin/python -m unittest tests.test_hermes_schemas -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'tradingagents.integrations'`.

- [ ] **Step 3: Add the MCP dependency and the complete validation/data contract.**

```text
# requirements_hermes.txt
mcp>=1.10,<2.0
```

```python
# tradingagents/integrations/__init__.py
"""Optional external-agent integrations for TradingAgents Crypto."""
```

```python
# tradingagents/integrations/schemas.py
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


SUPPORTED_ANALYSTS = ("market", "social", "news", "fundamentals")
SUPPORTED_PROVIDERS = (
    "openai",
    "anthropic",
    "google",
    "deepseek",
    "openrouter",
    "ollama",
)
SESSION_ID_PATTERN = re.compile(r"^hermes_[0-9a-f]{16,64}$")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def is_valid_session_id(session_id: str) -> bool:
    return bool(SESSION_ID_PATTERN.fullmatch(session_id or ""))


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=2, max_length=20)
    trade_date: date
    analysts: List[str] = Field(min_length=1, max_length=len(SUPPORTED_ANALYSTS))
    research_depth: Literal[1, 3, 5]
    llm_provider: str
    quick_model: str = Field(min_length=1, max_length=200)
    deep_model: str = Field(min_length=1, max_length=200)

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized.isalnum():
            raise ValueError("symbol must contain letters and digits only")
        return normalized

    @field_validator("analysts")
    @classmethod
    def validate_analysts(cls, value: List[str]) -> List[str]:
        normalized = [analyst.strip().lower() for analyst in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("analysts must not contain duplicates")
        unsupported = sorted(set(normalized) - set(SUPPORTED_ANALYSTS))
        if unsupported:
            raise ValueError("unsupported analysts: {}".format(", ".join(unsupported)))
        return normalized

    @field_validator("llm_provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_PROVIDERS:
            raise ValueError("unsupported llm_provider: {}".format(value))
        return normalized

    @field_validator("quick_model", "deep_model")
    @classmethod
    def normalize_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("model name must not be blank")
        return normalized


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reports: Dict[str, str]
    investment_plan: str
    trader_investment_plan: str
    final_trade_decision: str
    processed_signal: str


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    suggested_action: str


class AnalysisSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    session_id: str
    status: Literal["running", "completed", "failed"]
    created_at: datetime
    completed_at: Optional[datetime] = None
    request: AnalysisRequest
    result: Optional[AnalysisResult] = None
    error: Optional[ToolError] = None
```

The schema intentionally requires provider and model identifiers. A silent fallback to the OpenAI defaults would make a DeepSeek, Anthropic, or Google request appear valid while selecting incorrect models.

- [ ] **Step 4: Install the additive requirement and run the schema tests.**

Run:

```bash
.venv/bin/python -m pip install -r requirements_hermes.txt
.venv/bin/python -m unittest tests.test_hermes_schemas -v
```

Expected: all three tests PASS.

- [ ] **Step 5: Commit the contract.**

```bash
git add requirements_hermes.txt tradingagents/integrations/__init__.py tradingagents/integrations/schemas.py tests/test_hermes_schemas.py
git commit -m "feat: add Hermes MCP integration schemas"
```

### Task 2: Implement Atomic Session Storage And Safe Readiness Checks

**Files:**
- Create: `tradingagents/integrations/hermes_mcp.py`
- Test: `tests/test_hermes_mcp.py`

- [ ] **Step 1: Write tests for persisted sessions, readiness, and protected result lookup.**

```python
# tests/test_hermes_mcp.py
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from tradingagents.integrations.hermes_mcp import (
    SessionStore,
    get_analysis_result_impl,
    health_check_impl,
)
from tradingagents.integrations.schemas import AnalysisRequest


def make_request():
    return AnalysisRequest(
        symbol="BTC",
        trade_date=date(2026, 7, 28),
        analysts=["market", "news"],
        research_depth=1,
        llm_provider="deepseek",
        quick_model="deepseek-v4-flash",
        deep_model="deepseek-v4-pro",
    )


class HermesMcpTest(unittest.TestCase):
    def test_store_round_trip_and_result_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            session = store.create("hermes_0123456789abcdef", make_request())
            stored = store.save(session.model_copy(update={"status": "completed"}))

            self.assertEqual(store.load(stored.session_id).status, "completed")
            response = get_analysis_result_impl(stored.session_id, store)
            self.assertTrue(response["ok"])
            self.assertEqual(response["data"]["session"]["session_id"], stored.session_id)

    def test_invalid_result_id_returns_structured_error(self):
        with tempfile.TemporaryDirectory() as directory:
            response = get_analysis_result_impl("../../results", SessionStore(Path(directory)))

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "INVALID_SESSION_ID")

    def test_health_check_never_returns_provider_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "secret-value"}, clear=False):
                response = health_check_impl(SessionStore(Path(directory)))

        self.assertTrue(response["ok"])
        self.assertTrue(response["data"]["llm_provider_key_available"]["deepseek"])
        self.assertNotIn("secret-value", str(response))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify the MCP service implementation is missing.**

Run:

```bash
.venv/bin/python -m unittest tests.test_hermes_mcp -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'tradingagents.integrations.hermes_mcp'`.

- [ ] **Step 3: Implement the session store, response helpers, and health check.**

```python
# tradingagents/integrations/hermes_mcp.py
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_providers import API_KEY_ENV_VARS, build_graph_config, get_provider_api_key

from .schemas import AnalysisRequest, AnalysisResult, AnalysisSession, ToolError, is_valid_session_id, utc_now


LOGGER = logging.getLogger(__name__)
MCP = FastMCP("tradingagents_crypto")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_LOCK = Lock()
PAPER_TRADING_DISCLAIMER = "Research and paper-trading output only. Do not use this output to place real trades."


def success(data: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "data": data}


def failure(code: str, message: str, suggested_action: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "error": ToolError(
            code=code,
            message=message,
            suggested_action=suggested_action,
        ).model_dump(mode="json"),
    }


class SessionStore:
    def __init__(self, root: Path):
        self.root = root

    @classmethod
    def from_environment(cls) -> "SessionStore":
        results_dir = Path(
            os.environ.get(
                "TRADINGAGENTS_RESULTS_DIR",
                str(PROJECT_ROOT / "results"),
            )
        )
        return cls(results_dir / "hermes" / "sessions")

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not is_valid_session_id(session_id):
            raise ValueError("invalid session id")
        return self.root / "{}.json".format(session_id)

    def create(self, session_id: str, request: AnalysisRequest) -> AnalysisSession:
        session = AnalysisSession(
            session_id=session_id,
            status="running",
            created_at=utc_now(),
            request=request,
        )
        return self.save(session)

    def save(self, session: AnalysisSession) -> AnalysisSession:
        self.ensure()
        target = self._path(session.session_id)
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.root),
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(session.model_dump(mode="json"), temporary_file, ensure_ascii=True, indent=2)
                temporary_file.write("\n")
            temporary_path.replace(target)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
        return session

    def load(self, session_id: str) -> Optional[AnalysisSession]:
        path = self._path(session_id)
        if not path.is_file():
            return None
        return AnalysisSession.model_validate_json(path.read_text(encoding="utf-8"))


def health_check_impl(store: Optional[SessionStore] = None) -> Dict[str, Any]:
    session_store = store or SessionStore.from_environment()
    try:
        session_store.ensure()
        store_writable = os.access(str(session_store.root), os.W_OK)
    except OSError:
        store_writable = False

    key_available = {
        provider: bool(get_provider_api_key(provider))
        for provider, env_var in API_KEY_ENV_VARS.items()
        if env_var
    }
    configured_providers = sorted(
        provider for provider, available in key_available.items() if available
    )
    status = "ready" if store_writable and configured_providers else "degraded"

    return success(
        {
            "status": status,
            "project_dir": str(PROJECT_ROOT),
            "session_store": str(session_store.root),
            "session_store_writable": store_writable,
            "llm_provider_key_available": key_available,
            "configured_llm_providers": configured_providers,
            "finnhub_key_available": bool(os.environ.get("FINNHUB_API_KEY")),
            "coingecko_key_available": bool(
                os.environ.get("COINGECKO_DEMO_API_KEY")
                or os.environ.get("COINGECKO_PRO_API_KEY")
            ),
            "disclaimer": PAPER_TRADING_DISCLAIMER,
        }
    )


def get_analysis_result_impl(
    session_id: str,
    store: Optional[SessionStore] = None,
) -> Dict[str, Any]:
    if not is_valid_session_id(session_id):
        return failure(
            "INVALID_SESSION_ID",
            "The session identifier has an invalid format.",
            "Use the session_id returned by analyze_crypto.",
        )

    session_store = store or SessionStore.from_environment()
    try:
        session = session_store.load(session_id)
    except (OSError, ValidationError, ValueError):
        return failure(
            "SESSION_UNREADABLE",
            "The saved analysis session could not be read.",
            "Inspect the session JSON on the host and run a new analysis if it is malformed.",
        )
    if session is None:
        return failure(
            "SESSION_NOT_FOUND",
            "No saved analysis session matches this identifier.",
            "Use the session_id returned by analyze_crypto.",
        )
    return success(
        {
            "session": session.model_dump(mode="json"),
            "disclaimer": PAPER_TRADING_DISCLAIMER,
        }
    )
```

- [ ] **Step 4: Run the store and health-check tests.**

Run:

```bash
.venv/bin/python -m unittest tests.test_hermes_mcp -v
```

Expected: all three tests PASS. The test must not create files below the repository `results/` directory because it injects a temporary `SessionStore`.

- [ ] **Step 5: Commit the persistence and health-check implementation.**

```bash
git add tradingagents/integrations/hermes_mcp.py tests/test_hermes_mcp.py
git commit -m "feat: add Hermes MCP session storage and health check"
```

### Task 3: Add Serialized Graph Execution And MCP Tool Registration

**Files:**
- Modify: `tradingagents/integrations/hermes_mcp.py`
- Modify: `tests/test_hermes_mcp.py`

- [ ] **Step 1: Extend the test module with a graph-free execution test.**

Add these imports and test code to `tests/test_hermes_mcp.py`:

```python
from tradingagents.integrations.hermes_mcp import execute_analysis


class FakeGraph:
    def __init__(self, selected_analysts, debug, config):
        self.selected_analysts = selected_analysts
        self.debug = debug
        self.config = config

    def propagate(self, symbol, trade_date):
        return (
            {
                "market_report": "market report",
                "sentiment_report": "sentiment report",
                "news_report": "news report",
                "fundamentals_report": "fundamentals report",
                "investment_plan": "investment plan",
                "trader_investment_plan": "trader plan",
                "final_trade_decision": "HOLD because volatility is elevated.",
            },
            "HOLD",
        )


class HermesAnalysisExecutionTest(unittest.TestCase):

    @patch("tradingagents.integrations.hermes_mcp.cleanup_session_memory")
    @patch("tradingagents.integrations.hermes_mcp.get_provider_api_key", return_value="test-key")
    def test_execute_analysis_persists_mocked_graph_result(self, _provider_key, _cleanup):
        with tempfile.TemporaryDirectory() as directory:
            store = SessionStore(Path(directory))
            response = execute_analysis(
                make_request().model_dump(mode="json"),
                store=store,
                graph_factory=FakeGraph,
            )
            session = store.load(response["data"]["session_id"])

        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["processed_signal"], "HOLD")
        self.assertEqual(session.status, "completed")
        self.assertEqual(session.result.reports["market"], "market report")

    def test_execute_analysis_rejects_a_missing_provider_key(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("tradingagents.integrations.hermes_mcp.get_provider_api_key", return_value=""):
                response = execute_analysis(make_request().model_dump(mode="json"), SessionStore(Path(directory)))

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "MISSING_API_KEY")
```

Run:

```bash
.venv/bin/python -m unittest tests.test_hermes_mcp.HermesAnalysisExecutionTest.test_execute_analysis_persists_mocked_graph_result -v
```

Expected: FAIL with `ImportError: cannot import name 'execute_analysis'`.

- [ ] **Step 2: Add graph execution, Chroma session cleanup, and the three decorated tool functions.**

Append the following code to `tradingagents/integrations/hermes_mcp.py` after `get_analysis_result_impl`:

```python
def cleanup_session_memory(session_id: str) -> None:
    """Remove this run's ephemeral Chroma collections after graph execution."""
    try:
        import chromadb
        from chromadb.config import Settings

        client = chromadb.Client(Settings(allow_reset=True))
        for collection in client.list_collections():
            if collection.name.endswith("_{}".format(session_id)):
                client.delete_collection(name=collection.name)
    except Exception:
        # Cleanup must not hide a completed analysis or leak an exception to MCP.
        return


def result_from_state(final_state: Dict[str, Any], signal: Any) -> AnalysisResult:
    return AnalysisResult(
        reports={
            "market": str(final_state.get("market_report", "")),
            "sentiment": str(final_state.get("sentiment_report", "")),
            "news": str(final_state.get("news_report", "")),
            "fundamentals": str(final_state.get("fundamentals_report", "")),
        },
        investment_plan=str(final_state.get("investment_plan", "")),
        trader_investment_plan=str(final_state.get("trader_investment_plan", "")),
        final_trade_decision=str(final_state.get("final_trade_decision", "")),
        processed_signal=str(signal),
    )


def execute_analysis(
    request_data: Dict[str, Any],
    store: Optional[SessionStore] = None,
    graph_factory: Callable[..., TradingAgentsGraph] = TradingAgentsGraph,
) -> Dict[str, Any]:
    try:
        request = AnalysisRequest.model_validate(request_data)
    except ValidationError:
        return failure(
            "INVALID_REQUEST",
            "The analysis request did not match the supported MCP contract.",
            "Provide a crypto symbol, ISO date, supported analysts, depth 1/3/5, provider, and both model names.",
        )

    if request.llm_provider != "ollama" and not get_provider_api_key(request.llm_provider):
        return failure(
            "MISSING_API_KEY",
            "The requested LLM provider has no key in the MCP subprocess environment.",
            "Add the provider key to this MCP server's env mapping in ~/.hermes/config.yaml and reload MCP.",
        )

    session_store = store or SessionStore.from_environment()
    session_id = "hermes_{}".format(uuid.uuid4().hex)
    try:
        session = session_store.create(session_id, request)
    except OSError:
        return failure(
            "SESSION_WRITE_FAILED",
            "The MCP server cannot create its session file.",
            "Make the TradingAgents results directory writable by the Hermes service user.",
        )

    try:
        with ANALYSIS_LOCK:
            graph_config = build_graph_config(
                DEFAULT_CONFIG,
                {
                    "llm_provider": request.llm_provider,
                    "quick_think_llm": request.quick_model,
                    "deep_think_llm": request.deep_model,
                    "research_depth": request.research_depth,
                },
                session_id,
            )
            graph = graph_factory(
                selected_analysts=request.analysts,
                debug=False,
                config=graph_config,
            )
            final_state, signal = graph.propagate(request.symbol, request.trade_date.isoformat())
            result = result_from_state(final_state, signal)

        completed = session.model_copy(
            update={
                "status": "completed",
                "completed_at": utc_now(),
                "result": result,
            }
        )
        session_store.save(completed)
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
        # Do not log exception text or tracebacks: provider SDK errors can contain credentials.
        LOGGER.error(
            "analysis_failed session_id=%s exception_type=%s",
            session_id,
            type(error).__name__,
        )
        failed = session.model_copy(
            update={
                "status": "failed",
                "completed_at": utc_now(),
                "error": ToolError(
                    code="ANALYSIS_FAILED",
                    message="TradingAgents could not complete this analysis.",
                    suggested_action="Check the selected provider, model names, data-provider connectivity, and Hermes timeout before retrying.",
                ),
            }
        )
        try:
            session_store.save(failed)
        except OSError:
            pass
        return failure(
            "ANALYSIS_FAILED",
            "TradingAgents could not complete this analysis.",
            "Check the selected provider, model names, data-provider connectivity, and Hermes timeout before retrying.",
        )
    finally:
        cleanup_session_memory(session_id)


@MCP.tool(description="Check TradingAgents Crypto MCP readiness without revealing credentials.")
def health_check() -> Dict[str, Any]:
    return health_check_impl()


@MCP.tool(description="Run one paper-trading crypto research analysis and save its result.")
def analyze_crypto(
    symbol: str,
    trade_date: str,
    analysts: List[str],
    research_depth: int,
    llm_provider: str,
    quick_model: str,
    deep_model: str,
) -> Dict[str, Any]:
    return execute_analysis(
        {
            "symbol": symbol,
            "trade_date": trade_date,
            "analysts": analysts,
            "research_depth": research_depth,
            "llm_provider": llm_provider,
            "quick_model": quick_model,
            "deep_model": deep_model,
        }
    )


@MCP.tool(description="Retrieve a persisted TradingAgents Crypto analysis by session identifier.")
def get_analysis_result(session_id: str) -> Dict[str, Any]:
    return get_analysis_result_impl(session_id)


if __name__ == "__main__":
    MCP.run(transport="stdio")
```

The `ANALYSIS_LOCK` is not an optimization. `TradingAgentsGraph.__init__()` updates global dataflow configuration and `Toolkit` class-level configuration, so parallel requests could otherwise mix providers, models, and API keys. A process may serve health/result reads while an analysis runs, but only one graph invocation may enter the critical section.

- [ ] **Step 3: Run the entire MCP test module.**

Run:

```bash
.venv/bin/python -m unittest tests.test_hermes_mcp -v
```

Expected: all five tests PASS. The fake graph proves configuration mapping and persisted output without contacting an LLM, CoinGecko, Finnhub, or any exchange.

- [ ] **Step 4: Confirm FastMCP can import and exposes the intended module entrypoint.**

Run:

```bash
.venv/bin/python -c "from tradingagents.integrations.hermes_mcp import MCP; print(MCP.name)"
```

Expected output: `tradingagents_crypto`.

- [ ] **Step 5: Commit the executable bridge.**

```bash
git add tradingagents/integrations/hermes_mcp.py tests/test_hermes_mcp.py
git commit -m "feat: expose TradingAgents through Hermes MCP"
```

### Task 4: Write Cloud-Host Deployment And Hermes Configuration Documentation

**Files:**
- Create: `docs/hermes_integration.md`

- [ ] **Step 1: Write the cloud-host runbook with the following complete content.**

```markdown
# Hermes Agent Integration

## Scope

This integration runs TradingAgents Crypto as a local stdio MCP server for Hermes Agent. It exposes research-only tools and never opens a new HTTP port, changes nginx, or places exchange orders.

## Host Assumptions

- Host: `124.222.79.66`
- Project: `/home/ubuntu/workspace/TradingAgents-crypto`
- Python virtual environment: `/home/ubuntu/workspace/TradingAgents-crypto/.venv`
- Hermes configuration: `/home/ubuntu/.hermes/config.yaml`

## Install The Project Dependency

Connect to the server and update the deployed checkout to the commit containing this integration:

```bash
ssh ubuntu@124.222.79.66
cd /home/ubuntu/workspace/TradingAgents-crypto
git status --short
git pull --ff-only
.venv/bin/python --version
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r requirements_hermes.txt
.venv/bin/python -m unittest tests.test_hermes_schemas tests.test_hermes_mcp -v
.venv/bin/python -c "from tradingagents.integrations.hermes_mcp import MCP; print(MCP.name)"
```

The virtual environment must report Python 3.10 or newer. If it reports an older version, create a new Python 3.10+ virtual environment for this project before installing `requirements_hermes.txt`; the current MCP Python SDK does not support Python 3.8 or 3.9.

Do not run `python -m tradingagents.integrations.hermes_mcp` in a terminal as a manual smoke test. It speaks the MCP stdio protocol and must be started by Hermes.

## Configure Hermes

Hermes runs stdio MCP processes with a filtered environment. Add every credential needed by this server explicitly under the server `env` mapping. Keep only the provider(s) actually used. The values below are examples of field names, not values to copy.

```bash
install -d -m 700 /home/ubuntu/.hermes
touch /home/ubuntu/.hermes/config.yaml
chmod 600 /home/ubuntu/.hermes/config.yaml
hermes config edit
```

Add this entry under the top-level `mcp_servers` mapping in `/home/ubuntu/.hermes/config.yaml`:

```yaml
mcp_servers:
  tradingagents_crypto:
    command: "/home/ubuntu/workspace/TradingAgents-crypto/.venv/bin/python"
    args: ["-m", "tradingagents.integrations.hermes_mcp"]
    env:
      PYTHONPATH: "/home/ubuntu/workspace/TradingAgents-crypto"
      TRADINGAGENTS_RESULTS_DIR: "/home/ubuntu/workspace/TradingAgents-crypto/results"
      DEEPSEEK_API_KEY: "<real DeepSeek key>"
      FINNHUB_API_KEY: "<real Finnhub key>"
      COINGECKO_DEMO_API_KEY: "<optional real CoinGecko demo key>"
    timeout: 900
    connect_timeout: 60
```

For OpenAI, Anthropic, Google, or OpenRouter, remove `DEEPSEEK_API_KEY` and add exactly one matching key as needed: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, or `OPENROUTER_API_KEY`. Do not put any API key in the Git repository, `.env.example`, shell history, a committed file, nginx, or a public URL. The private Hermes configuration file is the only location in this setup that contains MCP subprocess credentials.

The `TRADINGAGENTS_RESULTS_DIR` path causes session files to appear below:

```text
/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/sessions/
```

No `cwd`, `url`, nginx route, firewall rule, Docker port, or systemd service is required for this MCP entry. Hermes launches the server on demand with stdio; the public website at `http://124.222.79.66/` remains separate.

## Verify From Hermes

Open a Hermes chat and run:

```text
/reload-mcp
/tools
```

`/tools` must list these names:

```text
mcp__tradingagents_crypto__health_check
mcp__tradingagents_crypto__analyze_crypto
mcp__tradingagents_crypto__get_analysis_result
```

Ask Hermes to call the health tool first:

```text
Call tradingagents_crypto health_check. Show only readiness status, configured provider names, and whether the session store is writable.
```

For the first live run, use shallow research and a single analyst pair:

```text
Call tradingagents_crypto analyze_crypto with symbol BTC, today's date, analysts market and news, research depth 1, provider deepseek, quick model deepseek-v4-flash, and deep model deepseek-v4-pro. This is paper-trading research only. Return the session_id, processed signal, and final decision.
```

Then retrieve the full persisted report:

```text
Call tradingagents_crypto get_analysis_result with the session_id returned by the previous tool call. Summarize the market, news, decision, risks, and the paper-trading disclaimer in Chinese.
```

If a long analysis exceeds the Hermes timeout, leave `timeout: 900` in place, reduce `research_depth` to `1`, use fewer analysts, check model/data-provider connectivity, and retry. Do not increase concurrency: the MCP server serializes graph runs because the underlying project configuration is global within the Python process.

## Operations And Rollback

The tool returns structured error codes rather than credentials or tracebacks:

| Code | Operator action |
| --- | --- |
| `MISSING_API_KEY` | Add the selected provider key under this MCP server's `env` mapping, then run `/reload-mcp`. |
| `SESSION_WRITE_FAILED` | Give the Hermes service user write access to the configured `TRADINGAGENTS_RESULTS_DIR`. |
| `SESSION_NOT_FOUND` | Use the opaque identifier returned by `analyze_crypto`; do not construct a filesystem path. |
| `SESSION_UNREADABLE` | Inspect the affected JSON file in `results/hermes/sessions`, retain it for diagnosis, then run a new analysis. |
| `ANALYSIS_FAILED` | Check model name, provider credentials, market-data connectivity, and timeout. Retry only after the underlying cause is fixed. |

To disconnect the bridge, remove only the `tradingagents_crypto` item from `mcp_servers`, save the file, and run `/reload-mcp`. This does not stop the Web UI and does not delete existing research sessions. Do not delete session files during rollback; they are the audit record for later paper-trading review.
```

- [ ] **Step 2: Check the runbook for cloud-host paths and secret leakage.**

Run:

```bash
rg -n "/Users/|localhost:|0\.0\.0\.0|api_key\s*=|sk-" docs/hermes_integration.md
```

Expected: no output. The only host paths in the document are `/home/ubuntu/...`; the YAML contains descriptive placeholder strings, never an actual key.

- [ ] **Step 3: Commit the deployment documentation.**

```bash
git add docs/hermes_integration.md
git commit -m "docs: add Hermes MCP cloud configuration"
```

### Task 5: Perform Full Verification Before Deployment

**Files:**
- Verify only: `requirements_hermes.txt`
- Verify only: `tradingagents/integrations/schemas.py`
- Verify only: `tradingagents/integrations/hermes_mcp.py`
- Verify only: `tests/test_hermes_schemas.py`
- Verify only: `tests/test_hermes_mcp.py`
- Verify only: `docs/hermes_integration.md`

- [ ] **Step 1: Run all existing and new unit tests in the project virtual environment.**

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Expected: PASS. Existing LLM-provider and dataflow request tests must remain green, proving that the integration did not alter the Web/CLI configuration path.

- [ ] **Step 2: Run syntax/import and source-security checks.**

Run:

```bash
.venv/bin/python -m compileall tradingagents/integrations
.venv/bin/python -c "from tradingagents.integrations.hermes_mcp import MCP; assert MCP.name == 'tradingagents_crypto'"
rg -n '(OPENAI|ANTHROPIC|GOOGLE|DEEPSEEK|OPENROUTER|FINNHUB|COINGECKO)_API_KEY: "[^<]' docs/hermes_integration.md && exit 1 || true
git status --short
```

Expected: compilation and import succeed; the secret scan prints no values; `git status --short` lists only the Phase 1 source, test, requirement, and documentation files.

- [ ] **Step 3: Deploy with the cloud-host runbook and record acceptance evidence.**

On `124.222.79.66`, complete the exact commands in `docs/hermes_integration.md`, then retain these non-secret checks in the deployment record:

```text
1. /tools lists all three mcp__tradingagents_crypto__* tools.
2. health_check reports a writable session store and the intended provider as configured.
3. A shallow BTC analysis returns a session_id and a paper-trading disclaimer.
4. get_analysis_result returns that same session_id and reports.
5. nginx configuration and listening ports are unchanged.
```

- [ ] **Step 4: Commit any final test-only corrections.**

```bash
git add requirements_hermes.txt tradingagents/integrations tests docs/hermes_integration.md
git commit -m "test: verify Hermes MCP bridge"
```

Only create this commit when verification required a correction after the earlier focused commits. Otherwise leave the previous commits intact and do not create an empty commit.

## Acceptance Criteria

- Hermes lists `health_check`, `analyze_crypto`, and `get_analysis_result` under `mcp__tradingagents_crypto__*`.
- `health_check` is safe: it reports boolean availability and never echoes a key.
- A valid analysis request runs through `build_graph_config`, `TradingAgentsGraph`, and `propagate`, then writes a schema-versioned JSON session under `results/hermes/sessions`.
- `get_analysis_result` accepts only opaque `hermes_<hex>` identifiers and cannot read arbitrary files.
- A failed request returns an error code, a human-readable message, and a suggested action without an exception traceback or credential.
- No HTTP MCP endpoint, nginx route, exchange credential, order placement, Hermes cron job, or Hermes memory write is added.
- All existing tests and new isolated unit tests pass before cloud deployment.

## Phase 2 And 3 Entry Conditions

Create separate implementation plans for review/learning and scheduled reporting only after this plan has a successful cloud-host acceptance run. The persisted session schema, `schema_version`, opaque IDs, serialized graph execution, and paper-trading disclaimer are the fixed interfaces those later phases must build on.
# Implementation Note (Task 4)

The generated `docs/hermes_integration.md` runbook supersedes this plan's original `.venv` deployment commands. Verified dependency integration established that MCP requires a dedicated `.venv-hermes-mcp` because MCP's AnyIO 4+ requirement conflicts with optional Chainlit `1.1.202` through `asyncer`. This note updates the deployment procedure only; the historical tasks below are intentionally unchanged.
