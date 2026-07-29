# Hermes MCP Phase 2: Paper-Decision Review And Learning Design

## Goal

Add an auditable, paper-trading-only learning loop to the Hermes MCP integration. An operator can review a completed analysis at an explicit later date, compare its BUY/SELL/HOLD decision with CoinGecko USD reference prices, persist the result, give Hermes a safe memory entry to write through its own memory tool, and automatically provide recent lessons for the same symbol to future analyses.

## Scope

- Add one strict MCP tool: `review_paper_decision(session_id, review_date)`.
- Review only completed Hermes analysis sessions and require `review_date` to be after the original `trade_date` and not after the current UTC date.
- Use CoinGecko historical daily USD reference prices for the original and review dates.
- Calculate raw asset return and score BUY and SELL directionally. HOLD and unparseable decisions remain explicitly unscored.
- Persist an immutable review artifact and an upserted, per-symbol learning index below the existing Hermes results directory.
- Load recent lessons for the same symbol into future Hermes analyses without requiring an embeddings provider. This must work with the current DeepSeek setup.
- Return a concise `hermes_memory_entry`; Hermes, not the MCP subprocess, writes that entry through its enabled built-in memory tool.
- Document cloud-host deployment, verification, use, recovery, and the unchanged research/paper-trading boundary.

## Non-Goals

- No exchange credentials, order routing, portfolio accounting, real trade execution, or performance claims.
- No automatic cron job or delivery channel. Phase 3 owns scheduling and notifications.
- No direct modification of `/home/ubuntu/.hermes/MEMORY.md`, `/home/ubuntu/.hermes/USER.md`, Hermes SQLite state, or an external Hermes memory provider from the MCP server.
- No LLM call during review. Review facts, verdicts, and memory entries must be deterministic and must not add provider cost or expose a provider key requirement.
- No migration or mutation of Phase 1 `AnalysisSession` schema-version-1 files.

## Existing Constraints

Phase 1 persists `AnalysisSession` JSON files in `results/hermes/sessions`, rejects unknown MCP inputs, and runs `analyze_crypto` serially. Its graph memories are session-scoped Chroma collections that are intentionally cleaned up after each run. The existing `FinancialSituationMemory` embedding implementation is disabled for DeepSeek, so it cannot supply persistent learning in the deployed configuration.

Hermes Agent `v0.19.0` on the cloud host has its built-in memory tool and memory injection enabled. Its active storage is the built-in `MEMORY.md`/`USER.md` provider. MCP is a child process and has no supported API for mutating the parent agent's conversation memory. The host agent must therefore own the actual Hermes memory write.

## Chosen Architecture

### 1. Separate, Backward-Compatible Records

Keep `AnalysisSession` exactly as the Phase 1 v1 contract. Add strict Pydantic models for these independent records:

- `ReviewRequest`: opaque `session_id` and ISO `review_date`.
- `PriceReference`: the queried date, USD price, and `coingecko` source label.
- `PaperDecisionReview`: schema version `1`, deterministic review ID, session ID, symbol, dates, parsed action, entry/review prices, raw return percentage, verdict, creation time, and deterministic memory entry.
- `SymbolLearningIndex`: schema version `1`, normalized symbol, update time, and a bounded list of review-derived lesson entries.

The review ID is deterministic for `(session_id, review_date)` and formatted as a non-path opaque identifier. A repeated invocation with the same pair returns the already persisted review instead of creating a second learning event.

Use the existing `SessionStore` atomic-write pattern for two new stores:

- `results/hermes/reviews/<review_id>.json` is the canonical review artifact.
- `results/hermes/memories/<SYMBOL>.json` is the derived, per-symbol learning index.

The memory index uses review ID as its upsert key and retains the 20 most recent lessons. Each file write is individually atomic. If writing the derived index fails after the review is saved, return a structured failure; the same idempotent review request repairs the missing index entry. Future analyses only consume successfully persisted index entries.

### 2. Deterministic Decision And Price Comparison

Extract a direction without an LLM call. Prefer a terminal `FINAL TRANSACTION PROPOSAL: BUY|SELL|HOLD` marker from `final_trade_decision`; otherwise accept a standalone BUY, SELL, or HOLD token from `processed_signal`; otherwise mark the action `UNPARSEABLE`.

Add a narrow CoinGecko helper that resolves the existing symbol-to-coin mapping and calls the historical coin endpoint for one date. It returns `market_data.current_price.usd` as the day's USD reference price. This is a public market-data reference, not a fill, close, or real execution price. The tool never silently substitutes another date, another quote currency, or a live price.

Calculate:

```text
raw_return_pct = ((review_price_usd - entry_price_usd) / entry_price_usd) * 100
```

Verdicts are:

- `correct`: BUY with positive return, or SELL with negative return.
- `incorrect`: BUY with negative return, or SELL with positive return.
- `flat`: BUY or SELL with zero return.
- `not_scored`: HOLD or `UNPARSEABLE` action.

The review response always includes the action, both price references, raw return, and verdict so it compares the original analysis outcome with observed market movement without returning an entire sensitive graph state.

### 3. Project Learning And Future Analysis Context

The review creates a short, structured lesson such as symbol, dates, action, return, and verdict. It contains no secret, API response body, or unrestricted model output. The new learning index is keyed by normalized symbol, so a BTC review cannot be loaded into ETH analysis.

Before `execute_analysis` constructs the graph, it loads at most five most-recent persisted lessons for `request.symbol` and passes them in a dedicated graph configuration field. Extend `FinancialSituationMemory` to expose those configured review lessons even when embedding memory is disabled; when no review lessons are configured, its existing behavior is unchanged. Existing researcher, trader, and risk-manager prompts already consume `get_memories`, so the lessons reach their established "Reflections from similar situations" sections without changing model prompts or doing vector search.

This is project-owned operational memory and is distinct from Hermes Agent memory. It provides deterministic same-symbol learning to the analysis graph even if a later Hermes conversation starts without prior chat context.

### 4. Hermes Built-In Memory Boundary

`review_paper_decision` returns a `hermes_memory_entry` alongside the canonical review. It is a concise, complete statement suitable for Hermes's existing memory tool, prefixed as paper-trading research and labelled with its symbol. The MCP server must never inspect or write Hermes memory files itself.

The runbook instructs the operator to ask Hermes in the same session to:

1. call `review_paper_decision`;
2. write the returned `hermes_memory_entry` using the enabled built-in memory tool; and
3. confirm that it is only a paper-trading lesson.

This preserves Hermes profile isolation, avoids bypassing memory-provider policy, and makes the host agent's memory action visible in its own audit trail.

### 5. MCP Contract And Error Handling

Register `review_paper_decision` with a raw argument model that exposes only `session_id` and `review_date`; its JSON schema has `additionalProperties: false`, matching Phase 1's strict input policy.

Return the standard `{ "ok": true, "data": ... }` or `{ "ok": false, "error": ... }` envelope. Define only non-sensitive errors:

- `INVALID_REVIEW_REQUEST`: invalid identifier, date, unknown MCP field, future review date, or review date not after trade date.
- `SESSION_NOT_FOUND`, `SESSION_UNREADABLE`, and `SESSION_NOT_COMPLETED`: original analysis cannot be safely reviewed.
- `PRICE_DATA_UNAVAILABLE`: CoinGecko cannot resolve the symbol or provide either required USD reference price.
- `REVIEW_STORE_UNAVAILABLE` and `REVIEW_WRITE_FAILED`: canonical review persistence cannot proceed.
- `LEARNING_STORE_UNAVAILABLE` and `LEARNING_WRITE_FAILED`: the derived learning index cannot be read or persisted.

Failures never disclose an API key, URL query carrying a key, raw provider exception, or full analysis prompt. Reviews may run with no LLM API key because they use only the persisted analysis and CoinGecko.

### 6. Concurrency And Idempotency

Use a review lock around the read-check-write sequence, just as Phase 1 serializes expensive graph execution. This prevents concurrent identical review requests from producing duplicate index entries. The deterministic review ID and index upsert provide a second correctness layer after process restart.

The review tool does not alter an `AnalysisSession`, invoke `analyze_crypto`, or create any trading side effect. A failed price lookup leaves no review or learning entry. A successful review is safe to request repeatedly.

## File-Level Responsibilities

| File | Change |
| --- | --- |
| `tradingagents/integrations/schemas.py` | Strict review, price, and learning Pydantic models plus validation constants. |
| `tradingagents/integrations/hermes_learning.py` | Atomic review/index stores, action parsing, deterministic verdict and memory-entry construction, and review orchestration helpers. |
| `tradingagents/dataflows/coingecko_utils.py` | Narrow historical USD reference-price helper built on the existing authenticated CoinGecko client. |
| `tradingagents/agents/utils/memory.py` | Return configured review lessons without embeddings; preserve existing behavior otherwise. |
| `tradingagents/integrations/hermes_mcp.py` | Wire stores and price provider into analysis/review operations; register the strict new MCP tool. |
| `tests/test_hermes_schemas.py` | Review-model validation and date boundary tests. |
| `tests/test_hermes_learning.py` | Price comparison, action parsing, idempotency, storage, symbol isolation, and failure coverage. |
| `tests/test_hermes_mcp.py` | MCP registration, unknown-field rejection, structured review responses, and prior-lesson graph configuration coverage. |
| `tests/test_dataflow_requests.py` | CoinGecko historical-reference parsing and failure handling without live HTTP. |
| `docs/hermes_integration.md` | Cloud host deployment SHA procedure, unchanged config requirements, Hermes memory status check, review prompts, and recovery/rollback steps. |

## Test Strategy

Tests must mock CoinGecko and graph behavior; they must not issue an LLM request, modify a real Hermes memory file, or depend on a live market price. Cover at least:

- strict schemas and rejection of unknown MCP fields;
- deterministic BUY/SELL/HOLD/unparseable parsing and verdicts;
- both boundary dates and future-date rejection;
- missing, unreadable, failed, and completed sessions;
- unavailable or zero USD price references;
- atomic review round-trip and repeated-call idempotency;
- symbol-isolated bounded learning indexes and repair after a derived-index failure;
- configured review lessons available under a DeepSeek graph configuration without embeddings;
- memory entry has a paper-trading label and no secret value;
- full existing test suite and `pip check` in the MCP-specific virtual environment.

## Cloud Deployment And Operations

Phase 2 uses the existing local stdio MCP server, virtual environment, `TRADINGAGENTS_RESULTS_DIR`, and `mcp_servers.tradingagents_crypto` configuration. No public port, nginx change, exchange key, or new Hermes secret is required. CoinGecko credentials remain optional but are recommended for historical-data reliability; use the already documented `COINGECKO_DEMO_API_KEY` or `COINGECKO_PRO_API_KEY` only in `/home/ubuntu/.hermes/config.yaml` with mode `600`.

After fetching a reviewed Phase 2 commit and switching the cloud project to that exact commit, reinstall only additive requirements in `.venv-hermes-mcp`, run `pip check` and the test suite, then execute `/reload-mcp` in Hermes. Verify the new tool appears, run an existing completed session through a historical review date, inspect the structured result, and request Hermes to write the returned memory entry using its built-in memory tool. Verify `hermes memory status` reports memory tool and injection enabled; do not read or print memory contents containing unrelated user information.

Rollback removes only the Phase 2 MCP deployment commit or disables the existing MCP entry as documented in Phase 1. Retain both `results/hermes/sessions` and the new `reviews`/`memories` directories for audit and recovery.

## Acceptance Criteria

- A completed BTC paper analysis can be reviewed once for a later historical date without any LLM call or real order.
- The response contains deterministic action comparison, USD reference prices, return, verdict, review ID, and a labelled Hermes memory entry.
- The same request is idempotent and does not duplicate BTC learning entries.
- A later BTC analysis receives only recent BTC lessons; ETH does not receive them.
- Hermes itself writes the candidate entry through its enabled memory tool; the MCP subprocess never writes Hermes memory storage.
- Existing Phase 1 tools, session files, Web UI behavior, and research/paper-trading boundary remain intact.
