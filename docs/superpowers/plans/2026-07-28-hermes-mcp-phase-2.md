# Hermes MCP Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Add deterministic paper-decision reviews, per-symbol learning context, and Hermes-owned memory handoff to the existing TradingAgents Crypto MCP server.

**Architecture:** Keep Phase 1 session JSON unchanged and persist independent review and learning-index records under the Hermes results root. A strict MCP review tool obtains CoinGecko USD historical references, creates an idempotent review, and returns a concise memory candidate. Future analyses load only same-symbol learning-index entries into the existing graph-memory interface without embeddings.

**Tech Stack:** Python 3.10+, Pydantic v2, FastMCP 1.28.1, `requests`, existing CoinGecko client, `unittest`.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `tradingagents/integrations/schemas.py` | Strict review request, reference-price, review, and symbol-learning schemas. |
| `tradingagents/integrations/hermes_learning.py` | Atomic review/index persistence, deterministic comparison, and review orchestration. |
| `tradingagents/dataflows/coingecko_utils.py` | One-date CoinGecko USD historical reference function. |
| `tradingagents/agents/utils/memory.py` | Inject configured review lessons when embeddings are unavailable. |
| `tradingagents/integrations/hermes_mcp.py` | Learn-before-analysis loading and strict `review_paper_decision` registration. |
| `tests/test_hermes_learning.py` | Learning module behavior and storage lifecycle. |
| `tests/test_hermes_schemas.py` | New strict model validation. |
| `tests/test_dataflow_requests.py` | Historical CoinGecko request/parse tests. |
| `tests/test_hermes_mcp.py` | MCP response and graph-context integration tests. |
| `docs/hermes_integration.md` | Phase 2 cloud deployment, verification, memory handoff, rollback. |

### Task 1: Add Strict Review Contracts

**Files:**
- Modify: `tradingagents/integrations/schemas.py`
- Modify: `tests/test_hermes_schemas.py`

- [x] **Step 1: Write failing schema tests for deterministic review records and invalid input.**

```python
from tradingagents.integrations.schemas import (
    PaperDecisionReview,
    PriceReference,
    ReviewRequest,
    SymbolLearningIndex,
    is_valid_review_id,
)

def test_review_models_normalize_and_reject_invalid_values(self):
    request = ReviewRequest(
        session_id="hermes_0123456789abcdef", review_date="2026-07-29"
    )
    self.assertEqual(request.review_date, date(2026, 7, 29))
    self.assertTrue(is_valid_review_id("review_0123456789abcdef"))
    with self.assertRaises(ValidationError):
        ReviewRequest(session_id="../session", review_date="2026-07-29")
    with self.assertRaises(ValidationError):
        PriceReference(date="2026-07-29", usd_price=0, source="coingecko")
```

- [x] **Step 2: Run the schema test and verify it fails because review contracts do not exist.**

Run: `.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_schemas -v`

Expected: `ImportError` for `PaperDecisionReview`, `ReviewRequest`, or `is_valid_review_id`.

- [x] **Step 3: Add strict Pydantic models and identifier helpers.**

```python
_REVIEW_ID_PATTERN = re.compile(r"^review_[0-9a-f]{16,64}$")

def is_valid_review_id(review_id: str) -> bool:
    return isinstance(review_id, str) and bool(_REVIEW_ID_PATTERN.fullmatch(review_id))

class ReviewRequest(_StrictModel):
    session_id: str
    review_date: date

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not is_valid_session_id(value):
            raise ValueError("invalid session id")
        return value

class PriceReference(_StrictModel):
    date: date
    usd_price: float = Field(gt=0)
    source: Literal["coingecko"]
```

Define `PaperDecisionReview`, `SymbolLearningEntry`, and `SymbolLearningIndex` with only fixed literals for action, verdict, source, schema version, normalized symbols, positive prices, and non-path IDs. Keep `AnalysisSession` schema version exactly `Literal[1]`.

- [x] **Step 4: Re-run the schema test and the complete schema module.**

Run: `.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_schemas -v`

Expected: all schema tests pass.

- [x] **Step 5: Commit the review contract.**

```bash
git add tradingagents/integrations/schemas.py tests/test_hermes_schemas.py
git commit -m "feat: add Hermes paper-review schemas"
```

### Task 2: Add A One-Date CoinGecko USD Reference

**Files:**
- Modify: `tradingagents/dataflows/coingecko_utils.py`
- Modify: `tests/test_dataflow_requests.py`

- [x] **Step 1: Write failing mocked-HTTP tests for historical USD reference lookup.**

```python
from datetime import date
from tradingagents.dataflows.coingecko_utils import get_crypto_historical_usd_price

def test_historical_usd_price_uses_coin_history_endpoint(self):
    response = FakeResponse({"market_data": {"current_price": {"usd": 101.25}}})
    with patch("requests.Session.get", return_value=response) as get:
        self.assertEqual(get_crypto_historical_usd_price("BTC", date(2026, 7, 28)), 101.25)
    self.assertIn("/coins/bitcoin/history", get.call_args.args[0])
    self.assertEqual(get.call_args.kwargs["params"], {"date": "28-07-2026", "localization": "false"})

def test_historical_usd_price_rejects_missing_or_non_positive_values(self):
    with patch.object(CoinGeckoAPI, "_make_request", return_value={}):
        with self.assertRaises(ValueError):
            get_crypto_historical_usd_price("BTC", date(2026, 7, 28))
```

- [x] **Step 2: Run those tests and verify the import fails.**

Run: `.venv-hermes-mcp/bin/python -m unittest tests.test_dataflow_requests.DataflowRequestTest -v`

Expected: import failure for `get_crypto_historical_usd_price`.

- [x] **Step 3: Implement the narrow helper without changing existing report formatting.**

```python
def get_crypto_historical_usd_price(symbol: str, reference_date: date) -> float:
    api = CoinGeckoAPI()
    coin_id = api.get_coin_id(symbol)
    if not coin_id:
        raise ValueError("coin ID is unavailable")
    data = api._make_request(
        f"/coins/{coin_id}/history",
        {"date": reference_date.strftime("%d-%m-%Y"), "localization": "false"},
    )
    try:
        price = float(data["market_data"]["current_price"]["usd"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("USD reference price is unavailable") from error
    if not math.isfinite(price) or price <= 0:
        raise ValueError("USD reference price is unavailable")
    return price
```

Import `date` and `math`. Do not return an error string, choose another date, use a current price, or leak HTTP exception text.

- [x] **Step 4: Run CoinGecko request tests.**

Run: `.venv-hermes-mcp/bin/python -m unittest tests.test_dataflow_requests -v`

Expected: all dataflow request tests pass with no live network request.

- [x] **Step 5: Commit the historical reference helper.**

```bash
git add tradingagents/dataflows/coingecko_utils.py tests/test_dataflow_requests.py
git commit -m "feat: add CoinGecko historical USD reference"
```

### Task 3: Build Idempotent Review And Learning Stores

**Files:**
- Create: `tradingagents/integrations/hermes_learning.py`
- Create: `tests/test_hermes_learning.py`

- [x] **Step 1: Write failing unit tests for parsing, scoring, persistence, symbol isolation, and idempotency.**

```python
def test_review_buy_direction_and_repeated_request_are_idempotent(self):
    session = completed_session("BUY", "FINAL TRANSACTION PROPOSAL: **BUY**")
    prices = {date(2026, 7, 28): 100.0, date(2026, 7, 29): 110.0}
    with TemporaryDirectory() as directory:
        result = review_completed_session(
            session, date(2026, 7, 29), lambda _symbol, day: prices[day], stores(directory)
        )
        repeated = review_completed_session(
            session, date(2026, 7, 29), lambda _symbol, day: prices[day], stores(directory)
        )
    self.assertEqual(result.verdict, "correct")
    self.assertEqual(result.raw_return_pct, 10.0)
    self.assertEqual(repeated.review_id, result.review_id)
    self.assertIn("paper-trading", result.hermes_memory_entry.lower())

def test_learning_index_is_symbol_isolated_and_bounded(self):
    store = LearningStore(Path(self.temp_dir) / "memories")
    store.upsert("BTC", learning_entry("review_0123456789abcdef"))
    self.assertEqual(store.lessons_for("ETH"), [])
    self.assertEqual(len(store.lessons_for("BTC")), 1)
```

Include dedicated tests for SELL, HOLD, unparseable decisions, flat return, invalid/future/reversed dates, missing completed result, zero/missing reference prices, unreadable files, and repair of an existing canonical review whose index entry is absent.

- [x] **Step 2: Run the new test module and verify it fails because the module is absent.**

Run: `.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_learning -v`

Expected: `ModuleNotFoundError: No module named 'tradingagents.integrations.hermes_learning'`.

- [x] **Step 3: Implement stores and pure review functions.**

```python
def make_review_id(session_id: str, review_date: date) -> str:
    digest = hashlib.sha256(f"{session_id}:{review_date.isoformat()}".encode("ascii")).hexdigest()
    return f"review_{digest[:32]}"

def classify_direction(action: str, raw_return_pct: float) -> str:
    if action in {"HOLD", "UNPARSEABLE"}:
        return "not_scored"
    if raw_return_pct == 0:
        return "flat"
    is_correct = (action == "BUY" and raw_return_pct > 0) or (action == "SELL" and raw_return_pct < 0)
    return "correct" if is_correct else "incorrect"
```

Use atomic ASCII JSON writes via `NamedTemporaryFile`, `flush`, `fsync`, and `os.replace`. `ReviewStore` owns validated review ID paths. `LearningStore` owns alphanumeric normalized-symbol paths, upserts by review ID, sorts newest first, retains 20 entries, and returns at most five lesson strings for graph configuration. The orchestration must load an existing review before price lookup, then repair its learning-index entry if necessary.

- [x] **Step 4: Run the learning module tests.**

Run: `.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_learning -v`

Expected: all learning tests pass; no test writes outside a temporary directory.

- [x] **Step 5: Commit review and learning persistence.**

```bash
git add tradingagents/integrations/hermes_learning.py tests/test_hermes_learning.py
git commit -m "feat: add idempotent Hermes decision reviews"
```

### Task 4: Wire Learning Into The Graph And MCP Tool Surface

**Files:**
- Modify: `tradingagents/agents/utils/memory.py`
- Modify: `tradingagents/integrations/hermes_mcp.py`
- Modify: `tests/test_hermes_mcp.py`

- [x] **Step 1: Write failing integration tests for static lessons and strict review MCP calls.**

```python
def test_execute_analysis_passes_only_same_symbol_lessons_to_graph(self):
    with TemporaryDirectory() as directory, patch(
        "tradingagents.integrations.hermes_mcp.LearningStore.from_environment",
        return_value=learning_store_with_btc_lesson(directory),
    ):
        execute_analysis(self.make_request().model_dump(mode="json"), store=SessionStore(Path(directory) / "sessions"), graph_factory=FakeGraph)
    self.assertEqual(FakeGraph.instances[-1].config["hermes_review_lessons"], ["BTC lesson"])

def test_review_tool_forbids_unknown_fields_and_returns_no_provider_secret(self):
    tool = MCP._tool_manager.get_tool("review_paper_decision")
    self.assertFalse(tool.parameters["additionalProperties"])
    _, result = asyncio.run(MCP.call_tool("review_paper_decision", {
        "session_id": "hermes_0123456789abcdef", "review_date": "2026-07-29", "extra": "no"
    }))
    self.assertEqual(result["error"]["code"], "INVALID_REVIEW_REQUEST")
```

Add direct `review_paper_decision_impl` tests using temporary stores and a mocked price helper for successful review, failed status, invalid review date, unavailable price, and repeated request repair.

- [x] **Step 2: Run MCP tests and verify failures are due to absent learning context and review tool.**

Run: `.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_mcp -v`

Expected: failures for missing `LearningStore`, `review_paper_decision_impl`, and MCP tool registration.

- [x] **Step 3: Add static review lessons to existing memory behavior.**

```python
class FinancialSituationMemory:
    def __init__(self, name, config):
        self.review_lessons = [str(lesson) for lesson in config.get("hermes_review_lessons", []) if str(lesson).strip()]
        # retain the existing embedding initialization unchanged

    def get_memories(self, current_situation, n_matches=1):
        if self.review_lessons:
            return [
                {"matched_situation": "", "recommendation": lesson, "similarity_score": 1.0}
                for lesson in self.review_lessons
            ]
        # retain the existing enabled/embedding lookup behavior
```

When `execute_analysis` builds graph config, use `LearningStore.from_environment().lessons_for(request.symbol, limit=5)`. On read failure log only the exception class and use `[]`; analysis behavior must remain available without historical learning.

- [x] **Step 4: Implement `review_paper_decision_impl` and strict FastMCP registration.**

```python
@MCP.tool()
def review_paper_decision(session_id: str, review_date: str, **unknown_fields: Any) -> dict[str, Any]:
    request_data = {"session_id": session_id, "review_date": review_date}
    request_data.update(unknown_fields)
    return review_paper_decision_impl(request_data)

class _ReviewPaperDecisionArguments(ArgModelBase):
    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")
    session_id: str
    review_date: str

    def model_dump_one_level(self) -> dict[str, Any]:
        return self.model_dump()
```

Use a module-level review lock, structured error envelopes, `redirect_stdout(sys.stderr)` around the price helper, no LLM key lookup, and no filesystem access to Hermes home. Configure the tool with `additionalProperties = False` exactly as `analyze_crypto` does.

- [x] **Step 5: Run targeted MCP and graph tests, then the whole suite.**

Run:

```bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_mcp tests.test_trading_graph -v
.venv-hermes-mcp/bin/python -m unittest discover -s tests -v
```

Expected: all tests pass and `FakeGraph` sees only BTC review lessons for a BTC request.

- [x] **Step 6: Commit MCP and graph integration.**

```bash
git add tradingagents/agents/utils/memory.py tradingagents/integrations/hermes_mcp.py tests/test_hermes_mcp.py
git commit -m "feat: expose Hermes paper-decision review tool"
```

### Task 5: Document Phase 2 Cloud Operation And Verify The Release

**Files:**
- Modify: `docs/hermes_integration.md`

- [x] **Step 1: Add Phase 2 deployment and operator tests to the runbook.**

Replace Phase 1 review SHA/ref placeholders with Phase 2 equivalents while retaining the existing clean-tree, origin provenance, detached-checkout, virtual-environment isolation, and no-public-port safeguards. Create `results/hermes/reviews` and `results/hermes/memories` with owner-only access alongside sessions. Add the exact new expected MCP tool name:

```text
mcp__tradingagents_crypto__review_paper_decision
```

Add these Hermes-session prompts:

```text
请调用 mcp__tradingagents_crypto__review_paper_decision，参数为 session_id="<已有分析返回的 session_id>"，review_date="<晚于 trade_date 且不晚于今天的 YYYY-MM-DD>"。这是研究和模拟交易，不得真实下单。

请使用刚才返回的 hermes_memory_entry 调用 Hermes 内置 memory 工具写入记忆。只记录该条 BTC 研究/模拟交易经验，不得记录或输出任何密钥，也不得据此真实下单。
```

Document that CoinGecko credentials remain optional but recommended, the MCP server does not write Hermes memory files, idempotent retry repairs a missing project learning index, and `hermes memory status` must show memory tool/injection enabled.

- [x] **Step 2: Run documentation safety checks.**

Run:

```bash
git diff --check HEAD -- docs/hermes_integration.md
rg -n '/Users|localhost:|0\\.0\\.0\\.0' docs/hermes_integration.md
```

Expected: diff check succeeds; the `rg` command prints no local-development address or path.

- [x] **Step 3: Run release verification.**

Run:

```bash
.venv-hermes-mcp/bin/python -m pip check
.venv-hermes-mcp/bin/python -m unittest discover -s tests -v
.venv-hermes-mcp/bin/python -c "from tradingagents.integrations.hermes_mcp import MCP; assert MCP._tool_manager.get_tool('review_paper_decision') is not None"
git diff --check
```

Expected: dependency check and all tests pass, the review tool is registered, and the diff has no whitespace errors.

- [x] **Step 4: Commit documentation and release verification.**

```bash
git add docs/hermes_integration.md
git commit -m "docs: add Hermes paper-review operations"
```
