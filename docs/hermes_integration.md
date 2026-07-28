# Hermes MCP &#20113;&#20027;&#26426;&#36816;&#34892;&#25163;&#20876;

## &#33539;&#22260;&#21644;&#23433;&#20840;&#36793;&#30028;

This runbook deploys the TradingAgents Crypto MCP server as a local Hermes stdio subprocess. It is research and paper-trading only. Tool output must not be used to place real trades, and this deployment introduces no exchange private key, order placement, public HTTP MCP endpoint, port, or nginx change.

The public Web UI remains at `http://124.222.79.66/`. This MCP service stays inside the host process boundary and is separate from that Web UI.

Host assumptions:

- Host: `124.222.79.66`
- Project: `/home/ubuntu/workspace/TradingAgents-crypto`
- Hermes config: `/home/ubuntu/.hermes/config.yaml`
- MCP session directory: `/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/sessions`
- Session schema version: `1`

Do not commit secrets, copy live secrets into this document, or expose them through nginx, a public URL, or shell history.

## &#37096;&#32626;&#19987;&#29992;&#34394;&#25311;&#29615;&#22659;

The MCP SDK requires Python 3.10 or newer. First check the host interpreter:

```bash
ssh ubuntu@124.222.79.66
python3 --version
python3 -c "import sys; assert sys.version_info >= (3, 10), sys.version"
```

Fetch the intended revision and create a dedicated MCP environment. Do not activate, install into, upgrade, or otherwise alter the existing project `.venv`; that environment continues to serve the Web UI.

```bash
cd /home/ubuntu/workspace/TradingAgents-crypto
set -e
git fetch origin
git pull --ff-only

python3 -m venv /home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip install --upgrade pip

grep -v '^chainlit$' requirements.txt > /tmp/tradingagents-requirements-hermes-mcp.txt
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip install -r /tmp/tradingagents-requirements-hermes-mcp.txt
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip install -r requirements_hermes.txt
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m pip check
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m unittest discover -s tests
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -c "from tradingagents.integrations.hermes_mcp import MCP; assert MCP.name == 'tradingagents_crypto'"
rm /tmp/tradingagents-requirements-hermes-mcp.txt
```

Remove the temporary filtered requirements file only after the installs complete successfully. If an install or check fails, keep it while diagnosing the failure, then remove it once the installation succeeds.

`mcp>=1.10,<2.0` requires AnyIO 4 or newer. The optional `chainlit` dependency in `requirements.txt` is Chainlit `1.1.202`, whose `asyncer` requirement constrains AnyIO below 4. Installing MCP into the current project `.venv` therefore breaks `pip check` and FastAPI construction. Excluding the exact `chainlit` line only in `.venv-hermes-mcp` resolves this verified conflict. The existing Web `.venv` is deliberately unchanged and retains Chainlit for the Web UI.

## Hermes &#37197;&#32622;

Protect the private Hermes directory and config before editing:

```bash
chmod 700 /home/ubuntu/.hermes
touch /home/ubuntu/.hermes/config.yaml
chmod 600 /home/ubuntu/.hermes/config.yaml
hermes config edit
```

In the standard top-level `mcp_servers` mapping, add this entry. Replace every placeholder with its real secret value, or remove that environment variable entirely. Never leave a placeholder as a purported credential.

```yaml
mcp_servers:
  tradingagents_crypto:
    command: "/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python"
    args: ['-m','tradingagents.integrations.hermes_mcp']
    env:
      PYTHONPATH: "/home/ubuntu/workspace/TradingAgents-crypto"
      TRADINGAGENTS_RESULTS_DIR: "/home/ubuntu/workspace/TradingAgents-crypto/results"
      DEEPSEEK_API_KEY: "<replace-with-real-deepseek-secret-or-remove>"
      FINNHUB_API_KEY: "<replace-with-real-finnhub-secret-or-remove>"
      COINGECKO_DEMO_API_KEY: "<optional-replace-with-real-coingecko-secret-or-remove>"
    timeout: 900
    connect_timeout: 60
```

For an analysis provider, set exactly the active provider key and remove the inactive provider key entries. The available alternatives are `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, and `OPENROUTER_API_KEY`; DeepSeek uses `DEEPSEEK_API_KEY`. `FINNHUB_API_KEY` is a data-provider key. CoinGecko is optional and may use `COINGECKO_DEMO_API_KEY` or `COINGECKO_PRO_API_KEY` instead. Keep all values only in the mode-600 Hermes config, never in the repository.

Do not add `cwd`, a tools include list, or other unverified fields. This is a stdio configuration, not an HTTP service.

## &#37325;&#36733;&#21644;&#39564;&#35777;

In a Hermes session, reload the configuration and inspect the registered tools:

```text
/reload-mcp
/tools
```

The exact expected tool names are:

```text
mcp__tradingagents_crypto__health_check
mcp__tradingagents_crypto__analyze_crypto
mcp__tradingagents_crypto__get_analysis_result
```

Use this health prompt:

> &#35831;&#35843;&#29992; mcp__tradingagents_crypto__health_check&#65292;&#30830;&#35748; session_store_writable &#20026; true&#65292;&#24182;&#26816;&#26597;&#24403;&#21069;&#37197;&#32622;&#30340;&#23494;&#38053;&#26159;&#21542;&#21487;&#29992;&#12290;&#19981;&#35201;&#36755;&#20986;&#20219;&#20309;&#23494;&#38053;&#20540;&#12290;

Start with this shallow BTC analysis prompt. It explicitly selects DeepSeek and should remain a small research request:

> &#35831;&#35843;&#29992; mcp__tradingagents_crypto__analyze_crypto &#23545; BTC &#36827;&#34892;&#27973;&#23618;&#30740;&#31350;&#20998;&#26512;&#12290;&#20351;&#29992; llm_provider=deepseek&#65292;quick_model=deepseek-v4-flash&#65292;deep_model=deepseek-v4-pro&#65292;research_depth=1&#12290;&#36825;&#26159;&#30740;&#31350;&#21644;&#27169;&#25311;&#20132;&#26131;&#65292;&#19981;&#24471;&#25552;&#20132;&#30495;&#23454;&#20132;&#26131;&#25110;&#19979;&#21333;&#12290;

Record the returned `session_id`, then retrieve it with this Chinese prompt:

> &#35831;&#35843;&#29992; mcp__tradingagents_crypto__get_analysis_result&#65292;&#20351;&#29992;&#20250;&#35805; ID &lt;session_id&gt; &#21462;&#22238;&#20013;&#25991;&#20998;&#26512;&#32467;&#26524;&#12290;&#35831;&#26126;&#30830;&#35828;&#26126;&#36825;&#20123;&#32467;&#26524;&#20165;&#29992;&#20110;&#30740;&#31350;&#21644;&#27169;&#25311;&#20132;&#26131;&#12290;

## &#20250;&#35805;&#23384;&#20648;&#21644;&#25925;&#38556;&#22788;&#29702;

Successful and failed analysis sessions persist as version-1 JSON files below `/home/ubuntu/workspace/TradingAgents-crypto/results/hermes/sessions`. Preserve this directory during normal rollback and incident investigation.

| Error code | Operator action |
| --- | --- |
| `INVALID_REQUEST` | Correct required analysis fields, provider, model, symbol, or date and retry. |
| `MISSING_API_KEY` | Add only the selected provider's real key to the private Hermes config, then reload MCP. |
| `SESSION_STORE_UNAVAILABLE` | Verify `TRADINGAGENTS_RESULTS_DIR`, ownership, free space, and that the results directory can be created. |
| `SESSION_WRITE_FAILED` | Verify write permission and storage health for `results/hermes/sessions`, then retry. |
| `SESSION_NOT_FOUND` | Check the opaque `session_id` returned by the analysis tool or start a new analysis. |
| `SESSION_UNREADABLE` | Preserve the session file, inspect filesystem health and file permissions, then retry or start a new session. |
| `ANALYSIS_FAILED` | Review the safe tool error, provider/data availability, and model request; retry later. |

## &#22238;&#28378;

To disable Hermes access, remove only the `tradingagents_crypto` entry from `/home/ubuntu/.hermes/config.yaml`, keep the file mode at `600`, then run `/reload-mcp` in Hermes. This does not alter the Web UI, open or close any network port, change nginx, delete `.venv-hermes-mcp`, or remove persisted sessions.
