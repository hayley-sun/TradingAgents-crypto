# Hermes Cron Bootstrap Configuration Design

## Context

The first live no-agent submit Cron execution created its 2026-07-30 batch,
but every item recorded `MISSING_API_KEY`. Hermes 0.19.0 deliberately invokes
Cron scripts through `_sanitize_subprocess_env`, which removes
`DEEPSEEK_API_KEY` and other provider credentials. A systemd
`EnvironmentFile` can reach the Gateway process but cannot cross this
intentional child-process boundary.

The failed date-keyed batch is immutable and must not be deleted or rewritten.
Both production Cron jobs remain paused until a corrected no-agent execution
has been validated.

## Goal

Allow the version-controlled daily-report bootstrap to load only the
TradingAgents MCP configuration it requires before it imports the daily-report
runner. This restores detached DeepSeek worker submission without duplicating
credentials, adding a project `.env`, weakening Hermes environment
sanitization, or exposing secrets in output or logs.

## Architecture

`tradingagents.integrations.hermes_daily_report_bootstrap` owns a small
configuration adapter. Before importing
`tradingagents.integrations.hermes_daily_report_runner`, it resolves the
Hermes config from `HERMES_HOME/config.yaml` when set, otherwise from
`Path.home() / ".hermes/config.yaml"`. The adapter uses `yaml.safe_load`,
selects only `mcp_servers.tradingagents_crypto.env`, and copies non-empty
string values for this fixed whitelist into the bootstrap process environment:

- `TRADINGAGENTS_RESULTS_DIR`
- `DEEPSEEK_API_KEY`
- `FINNHUB_API_KEY`
- `COINGECKO_DEMO_API_KEY`
- `COINGECKO_PRO_API_KEY`
- `CRYPTOCOMPARE_API_KEY`

The adapter never reads another MCP server, scans the ambient environment,
writes a file, accepts a production config-path argument, or prints a config
value or error. Missing, unreadable, malformed, or structurally invalid YAML
does not mutate the target environment; the existing runner then emits its
safe missing-configuration result. Test-only function arguments permit a
temporary config path and environment mapping.

The bootstrap remains the failure boundary: unexpected loader or runner-import
failures produce one existing `REPORT_RUNNER_FAILED` JSON envelope without
exception details. The runner, session worker, report store, and MCP behavior
remain unchanged.

## Security And Migration

The `600` Hermes config owned by `ubuntu` remains the only secret source.
Hermes continues to strip provider credentials from arbitrary script
environments. The reviewed bootstrap explicitly reads its own MCP entry before
launching the fixed project runner; values are not logged or persisted by this
integration.

The existing `/etc/tradingagents/hermes-gateway.env` and
`hermes-gateway.service.d/tradingagents-env.conf` become redundant. Cloud
migration removes both, reloads systemd, and restarts the Gateway. Interactive
MCP continues to receive its environment from the same MCP config entry.

## Testing And Acceptance

Unit tests prove whitelist isolation, no mutation on invalid config, and loader
execution before runner import. Static runbook tests reject provider
`EnvironmentFile` instructions. Cloud acceptance uses a temporary historical
date no-agent job to prove submit creates three sessions, active archive writes
no report, and terminal archive writes one mode-`600` Markdown report with the
paper-trading disclaimer. Temporary artifacts are removed afterward; production
jobs remain paused until all checks succeed.
