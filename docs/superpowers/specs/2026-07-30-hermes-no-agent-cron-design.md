# Hermes No-Agent Daily Report Cron Design

## Context

Phase 3's first manual submit run exposed a runtime failure in the
agent-driven Cron path. Hermes registered the TradingAgents MCP tools, but a
DeepSeek control-model response blocked for about eleven minutes. During that
wait, the stdio MCP client lost its usable session and the eventual
`start_daily_report_batch` call failed before any report batch was written.

The existing asynchronous analysis workers, report batch store, immutable
archive behavior, MCP tools, and interactive daily-report skill remain valid.
Only the scheduled execution boundary needs to stop depending on an LLM and a
long-lived stdio MCP session.

## Goal

Use Hermes Cron strictly as a local scheduler for deterministic submit and
archive commands. Each command must reuse the existing Phase 3 report
implementation, produce safe structured stdout, and never require a Hermes
control-model call, MCP stdio connection, external delivery, a review, Hermes
memory mutation, exchange credentials, or a real order. The existing detached
analysis workers still use the configured DeepSeek models after submit.

## Scope

Included:

- A version-controlled Python command module with `submit` and `archive`
  modes.
- A standard-library bootstrap that safely reports runner import failures.
- Fixed daily BTC, ETH, and SOL paper-research submission settings.
- A deterministic Chinese archive narrative rendered only from safe persisted
  report-summary data.
- Two version-controlled shell wrappers installed under `~/.hermes/scripts`.
- Hermes Cron jobs created with `--script` and `--no-agent`.
- Tests and the cloud runbook for replacement and manual validation.

Excluded:

- External messages, LLM-generated scheduled narratives, real trading,
  exchange credentials, paper-decision reviews, Hermes memory writes, and
  changes to the existing MCP or interactive-skill behavior.

## Architecture

`tradingagents.integrations.hermes_daily_report_bootstrap` loads
`tradingagents.integrations.hermes_daily_report_runner` behind a safe JSON
failure boundary. The runner is a small command adapter, not a second report
store. It computes the current
`Asia/Shanghai` date unless `--trade-date` is supplied for validation, then
calls the existing internal daily-report implementation:

1. `submit` constructs the fixed `DailyReportRequest` for BTC, ETH, and SOL,
   with market/news/fundamentals analysts, depth 1, and configured DeepSeek
   models. It calls `start_daily_report_batch_impl`, which atomically creates
   or returns the date-keyed batch and launches detached workers.
2. `archive` reads the current batch through `get_daily_report_batch_impl`.
   It exits successfully without writing when the state is `active`. For a
   terminal `ready` or `degraded` batch, it renders a bounded deterministic
   Chinese narrative from ordered symbols, safe signals, decisions, errors,
   and the previous report snapshot, then calls `archive_daily_report_impl`.

The runner emits one JSON object to stdout. It returns zero for successful
submission, successful archival, and an active archive wait. Invalid input,
missing batches, persistence errors, and archive conflicts return nonzero so
Hermes records an actionable Cron failure. It never prints configuration,
environment variables, API keys, worker logs, or traceback details.

The narrative contains no wall-clock timestamp or random value. It renders
only safe persisted result fields after bounded secret-like token redaction.
For identical terminal inputs it remains byte-stable, preserving the existing
archive idempotency guarantee after a transient metadata-write failure.

## Hermes Integration

Two shell wrappers live in `deploy/hermes/scripts` and only execute the
project's `.venv-hermes-mcp` interpreter with one bootstrap mode. Deployment
copies them to `~/.hermes/scripts` with mode `700`; the existing interactive
skill remains installed with mode `600` for manual operator use. The root-only
`/etc/tradingagents/hermes-gateway.env` is attached to
`hermes-gateway.service` through a systemd drop-in, so its child Cron scripts
and detached workers inherit only the configured runtime values.

The jobs use Hermes `--no-agent`, `--script`, the project work directory, and
`--deliver local`:

- `tradingagents-daily-report-submit`: `0 8 * * *`
- `tradingagents-daily-report-archive`: `0 12 * * *`

Neither job attaches the interactive skill, because `--no-agent` executes the
script directly. The runbook creates and pauses equivalent no-agent jobs before
removing the two paused agent jobs. Manual validation waits for each durable
Cron run to reach a terminal state before pausing it, then validates submit,
active archive, and terminal archive. The server timezone remains
`Asia/Shanghai`.

## Failure Handling

The submit runner is idempotent through the existing date-keyed batch store;
re-running it with the fixed configuration cannot start duplicate sessions.
The archive runner never creates a partial file while a batch is active. A
terminal degraded batch is archived with its safe per-symbol error code. A
different report body remains rejected by the existing immutable archive
checks.

An `active` archive run is normal scheduling state, not a failure. All other
safe error envelopes, including bootstrap import failures, are returned to the
Cron execution record with a nonzero exit code. Operators inspect
`hermes cron runs <job-id>` and project batch/report files; they do not repair
reports with shell edits.

## Testing And Acceptance

Tests cover:

- Asia/Shanghai date selection and explicit-date validation.
- Fixed submit request construction and safe JSON exit codes.
- Deterministic Chinese narratives for completed, failed, and prior-report
  inputs, including secret-like model-text redaction.
- Safe bootstrap behavior for runner import failures.
- Active archive behavior with no archive write.
- Successful archive behavior and idempotent retry.
- Wrapper and runbook constraints: no-agent scripts, local delivery, no
  review, memory, exchange, or external messaging behavior.

Phase 3 is accepted when the cloud host's manual no-agent submit creates one
batch with three detached sessions, a later active archive produces no report,
and a later terminal archive creates exactly one mode-`600` Markdown report
with the paper-trading disclaimer. Both jobs must be paused throughout manual
validation and resumed only after those checks pass.
