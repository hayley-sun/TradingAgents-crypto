# Hermes Cloud Integration Design

Date: 2026-07-28

## Context

TradingAgents Crypto is already deployed on an Ubuntu cloud host and served at:

- Public web URL: `http://124.222.79.66/`
- Project path on host: `/home/ubuntu/workspace/TradingAgents-crypto`

Hermes Agent is also already deployed on the same host. The integration should use same-host communication and should not expose the MCP server to the public internet.

Relevant Hermes references:

- Hermes Agent repository: https://github.com/NousResearch/hermes-agent
- Hermes MCP feature docs: https://nousresearch-hermes-agent.mintlify.app/user-guide/features/mcp
- Hermes memory docs: https://nousresearch-hermes-agent.mintlify.app/user-guide/features/memory
- Hermes cron docs: https://nousresearch-hermes-agent.mintlify.app/user-guide/features/cron
- Hermes messaging docs: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/

## Goal

Integrate TradingAgents Crypto with Hermes in three phases:

1. Expose TradingAgents as Hermes-callable MCP tools.
2. Add a paper-trading review and learning loop.
3. Add scheduled 24/7 reports and optional message delivery.

TradingAgents remains the trading-analysis executor. Hermes becomes the outer orchestration layer for tool calling, memory, periodic review, and scheduled reporting.

## Non-Goals

- No real exchange order placement.
- No public MCP HTTP endpoint.
- No wholesale rewrite from LangGraph to Hermes internals.
- No automatic production trading based on LLM output.
- No storage of raw API keys in project files.

## Architecture

The integration uses a local stdio MCP server launched by Hermes:

```text
Hermes Agent
  -> local stdio MCP process
    -> tradingagents.integrations.hermes_mcp
      -> TradingAgentsGraph
        -> analysts, researchers, trader, risk manager
        -> crypto dataflows and LLM providers
```

The public website at `http://124.222.79.66/` remains the user-facing web UI. Hermes MCP communication happens only inside the cloud host process boundary.

## Phase 1: MCP Tool Bridge

Add a Python MCP server inside the project:

- `tradingagents/integrations/hermes_mcp.py`
- `tradingagents/integrations/schemas.py`
- `requirements_hermes.txt`
- `docs/hermes_integration.md`

Implemented MCP tools:

- `health_check()`: verifies imports, config visibility, and basic runtime readiness.
- `analyze_crypto(symbol, trade_date, analysts, research_depth, llm_provider, quick_model, deep_model)`: creates a detached paper-trading analysis job and returns its opaque session id immediately.
- `get_analysis_result(session_id)`: returns a stored result by session id.
- `review_paper_decision(session_id, review_date)`: deterministically scores one completed paper decision and returns a compact Hermes memory candidate.

`analyze_crypto` never runs the graph in the stdio MCP process. The detached worker serializes graph execution with a cross-process file lock, writes per-session logs, and persists `queued`, `running`, `completed`, or `failed` state. `get_analysis_result` is the only status/result lookup API.

### Cloud Host Configuration

All commands are intended to run on the cloud host:

```bash
ssh ubuntu@124.222.79.66
cd /home/ubuntu/workspace/TradingAgents-crypto
set -e
git status --short
test -z "$(git status --short)" || { echo "working tree must be clean" >&2; exit 1; }
reviewed_integration_commit="<replace-with-reviewed-integration-commit-already-pushed-to-origin>"
git fetch origin --tags
git show --verify --quiet "$reviewed_integration_commit^{commit}"
git switch --detach "$reviewed_integration_commit"
git log -1 --oneline
python3 -c "import sys; assert sys.version_info >= (3, 10), sys.version"
python3 -m venv /home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip install --upgrade pip
requirements_file="$(mktemp /tmp/tradingagents-requirements-hermes-mcp.XXXXXX)"
grep -v '^chainlit$' requirements.txt > "$requirements_file"
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip install -r "$requirements_file"
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip install -r requirements_hermes.txt
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip check
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m unittest discover -s tests
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -c "from tradingagents.integrations.hermes_mcp import MCP; assert MCP.name == 'tradingagents_crypto'"
rm -- "$requirements_file"
```

The operator must replace `reviewed_integration_commit` with a reviewed integration commit already pushed to `origin`. The canonical command and branch-ref verification procedure is `docs/hermes_integration.md`; it preserves local changes, uses a detached checkout, and does not force-reset the host worktree.

MCP must use a dedicated `.venv-hermes-mcp`, not the existing Web `.venv`. This was verified during dependency integration: `mcp==1.28.1` requires AnyIO 4+, while optional Chainlit `1.1.202` depends on `asyncer` with AnyIO <4. Installing MCP into the existing project `.venv` breaks `pip check` and FastAPI construction. The dedicated MCP environment installs all runtime requirements except the one exact `chainlit` line, then installs `requirements_hermes.txt`; the Web `.venv` remains unchanged.

Hermes config should use the dedicated MCP virtualenv Python and the standard top-level `mcp_servers` mapping:

```yaml
mcp_servers:
  tradingagents_crypto:
    command: "/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python"
    args: ['-m','tradingagents.integrations.hermes_mcp']
    env:
      PYTHONPATH: "/home/ubuntu/workspace/TradingAgents-crypto"
      TRADINGAGENTS_RESULTS_DIR: "/home/ubuntu/workspace/TradingAgents-crypto/results"
      FINNHUB_API_KEY: "${FINNHUB_API_KEY}"
      DEEPSEEK_API_KEY: "${DEEPSEEK_API_KEY}"
      COINGECKO_DEMO_API_KEY: "${COINGECKO_DEMO_API_KEY}"
    timeout: 900
    connect_timeout: 60
```

Replace every placeholder with a real secret value or remove that variable. Configure only the active LLM provider key: `DEEPSEEK_API_KEY`, or one of `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, or `OPENROUTER_API_KEY`. CoinGecko is optional. Keep secrets out of the repository and protect `/home/ubuntu/.hermes/config.yaml` with mode `600`.

Verification commands:

```bash
hermes config edit
```

Inside a Hermes session:

```text
/reload-mcp
/tools
```

Expected result: `/tools` lists `mcp__tradingagents_crypto__health_check`, `mcp__tradingagents_crypto__analyze_crypto`, `mcp__tradingagents_crypto__get_analysis_result`, and `mcp__tradingagents_crypto__review_paper_decision`; Hermes can submit a small BTC analysis, poll its session, and review a completed paper decision without opening any new public port. The authoritative operational procedure is `docs/hermes_integration.md`.

## Phase 2: Review And Learning Loop

Phase 2 persists an immutable, deterministic review for one completed session under `results/hermes/reviews`. It compares the original decision with CoinGecko historical USD reference prices for the session trade date and review date, and classifies the action as `correct`, `incorrect`, `flat`, or `not_scored`.

The same operation atomically maintains a bounded, symbol-isolated index under `results/hermes/memories/<SYMBOL>.json`. The index keeps the newest 20 lessons; future same-symbol analyses receive at most five compact lessons through the existing graph-memory interface. Read failures only suppress learning context and do not prevent a new analysis.

`review_paper_decision` makes no LLM call and never places orders. It is idempotent for a `(session_id, review_date)` pair; a retry repairs a missing learning-index entry without fetching prices again. The MCP server does not write Hermes-owned memory files. Hermes receives `hermes_memory_entry` and must explicitly use its own memory tool if the operator accepts the lesson.

Recommended Hermes memory policy:

- Store stable, compact lessons only.
- Do not store raw market dumps.
- Do not store secrets.
- Prefer skill updates for repeatable procedures, such as "crypto risk review checklist".

Expected result: Hermes can request one completed session review, observe the persisted result and learning entry, then explicitly store a concise operator-approved memory lesson. Future same-symbol analyses can use project-local learning context without embedding or reusing raw reports.

## Phase 3: Scheduled Reports And Alerts

After MCP and review tools are stable, enable Hermes cron jobs for scheduled analysis.

Example daily report prompt:

```text
Call the tradingagents_crypto tools to analyze BTC, ETH, and SOL for today. Use market, news, and fundamentals analysts with conservative research depth. Produce a Chinese daily report with signal changes, key risks, confidence limits, and differences from the previous report. Do not provide real order execution instructions.
```

Example cron operation:

```bash
hermes cron list
hermes cron start
```

Inside Hermes:

```text
/cron add "0 8 * * *" "Call tradingagents_crypto to produce the daily BTC, ETH, SOL crypto research report in Chinese. Include signal changes, major risks, and differences from the previous report. Do not give real order execution instructions."
/cron list
/cron run <job-id>
```

Optional message delivery can be configured through the Hermes messaging gateway after cron reports are proven reliable. Telegram or Discord delivery should send reports, not trading commands.

## Error Handling

The MCP server should return structured errors with:

- error code
- human-readable message
- safe details without API keys
- suggested operator action

Common cases:

- missing API key
- unsupported LLM provider
- data provider timeout
- invalid symbol
- analysis run timeout
- malformed stored session

Long-running analysis runs only in its detached worker and must never block stdio keepalives. The MCP call timeout remains a safety limit for short operations, not a mechanism for waiting on an analysis graph.

## Data Storage

Use project-local storage for integration artifacts:

```text
/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/
  sessions/
  logs/
  reviews/
  memories/
```

Session JSON should include a schema version so future migrations are explicit.

## Security

- MCP uses stdio only.
- No new nginx route is required for MCP.
- No raw secrets in git.
- Logs must redact API keys.
- Public web UI remains separate from Hermes MCP tools.
- Automated reports must include a paper-trading disclaimer.
- No exchange private keys are introduced in this integration.

## Testing

Phase 1 tests:

- import test for the MCP module
- schema validation for tool inputs
- unit test for safe config construction
- smoke test for `health_check`
- optional mocked `analyze_crypto` test

Phase 2 tests:

- immutable review/index write and idempotent repair
- outcome calculation with fixed price fixtures
- review MCP validation and graph lesson injection without embeddings

Phase 3 tests:

- dry-run cron prompt
- generated report contains required sections
- message delivery disabled unless explicitly configured

## Acceptance Criteria

Phase 1 is complete when:

- Hermes lists `tradingagents_crypto` tools.
- `health_check` succeeds on the cloud host.
- Hermes can submit `analyze_crypto("BTC", "2026-07-28", ...)`, receive a session id promptly, and retrieve the completed result by polling.
- No public MCP port is exposed.

Phase 2 is complete when:

- a completed session can produce an idempotent review for a later historical date.
- the review and its per-symbol learning entry are persisted under the project results root.
- Hermes receives a compact memory candidate and future same-symbol analyses load at most five project-local lessons.

Phase 3 is complete when:

- Hermes cron can run a daily multi-symbol report manually and on schedule.
- reports are saved and optionally delivered through a configured gateway.
- reports remain clearly marked as research/paper-trading output.
