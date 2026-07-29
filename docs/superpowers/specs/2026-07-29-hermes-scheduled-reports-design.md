# Hermes Scheduled Reports Design

## Goal

Phase 3 adds a daily, project-archived crypto research workflow driven by
Hermes Cron. It submits paper-trading research jobs for BTC, ETH, and SOL,
then later archives one Chinese Markdown report when every job is terminal.
It has no delivery gateway, order routing, review automation, or Hermes
memory mutation.

## Constraints

- The cloud host and Cron schedule use `Asia/Shanghai`.
- Existing analysis workers are detached and graph execution is serialized.
  A Cron agent must not wait for three analyses to finish.
- API keys remain only in `/home/ubuntu/.hermes/config.yaml`. No `.env`,
  `EnvironmentFile`, source-controlled secret, port, or web route is added.
- Scheduled work never calls `review_paper_decision` or writes Hermes memory.
- Every generated report remains research and paper trading only.

## Architecture

The workflow has two short Cron tasks and a project-owned persistent batch:

1. The submit task calls `start_daily_report_batch` at 08:00. It creates or
   returns a daily batch and starts detached analyses without polling.
2. The archive task calls `get_daily_report_batch` at 12:00. If all items are
   terminal, Hermes writes a bounded Chinese narrative and calls
   `archive_daily_report`; an active batch causes no write.

The MCP process owns manifest and archive writes. Cron and its LLM agent only
call strict MCP tools, which makes repeated ticks idempotent and avoids agent
filesystem writes or concurrency races.

## Persistent Data

```text
results/hermes/
  report_batches/<YYYY-MM-DD>.json
  reports/<YYYY-MM-DD>.md
```

`DailyReportBatch` is a schema-version-1 record keyed by the normalized trade
date. It contains the normalized request, opaque per-symbol session IDs or a
safe submission error, timestamps, and optional archive metadata. A batch item
has exactly one of a session ID and a submission error.

`ReportBatchStore` protects create and update operations with an exclusive
file lock and writes JSON atomically. Batch creation persists every successful
item before attempting the next one; a retry after a submit error returns the
partial batch instead of launching duplicates. A matching request for the same
date returns the existing batch. A different request for that date returns a
safe conflict error.

Markdown reports are atomically written with mode `0600`. Archive metadata
stores the filename, content SHA-256, timestamp, and signal snapshot. A retry
with the same body returns the existing archive; a different body is rejected,
so historical reports cannot be overwritten. The newest earlier archive
supplies a bounded signal and decision snapshot for comparison.

## MCP Tools

All three input models forbid unknown fields.

1. `start_daily_report_batch` accepts trade date, 1-5 unique symbols,
   analysts, research depth, and LLM configuration. It calls the current
   asynchronous analysis launcher and promptly returns the stored batch.
2. `get_daily_report_batch` accepts a trade date. It reads the batch and
   sessions without invoking providers, returning aggregate state, safe result
   data, and the previous archived signal snapshot.
3. `archive_daily_report` accepts a trade date and a nonblank narrative up to
   20,000 characters. It rejects missing or active batches and conflicting
   rewrites. The server renders the title, request configuration, per-symbol
   snapshot, narrative section, comparison section, and fixed disclaimer.

Expected error codes include `REPORT_BATCH_NOT_FOUND`,
`REPORT_BATCH_CONFLICT`, `REPORT_BATCH_ACTIVE`, `REPORT_BATCH_UNREADABLE`,
`REPORT_ARCHIVE_INVALID`, and `REPORT_ARCHIVE_CONFLICT`. Errors never expose
paths, keys, raw provider exceptions, or Hermes memory contents.

## States And Failure Policy

State is derived from stored item and session state:

- `active`: one or more sessions are queued or running.
- `ready`: every item completed and no submission error exists.
- `degraded`: every item is terminal, but one or more session failed or
  submission failed.

Only `ready` and `degraded` batches can be archived. A degraded archive lists
unavailable symbols by their safe error code and never retries them. Active
batches never receive a partial report.

## Hermes Skill And Cron

The `tradingagents-daily-report` skill has two explicit modes:

- Submit uses the current `Asia/Shanghai` date, BTC/ETH/SOL, `market`,
  `news`, and `fundamentals`, depth 1, and configured DeepSeek models. It
  calls only `start_daily_report_batch`.
- Archive calls `get_daily_report_batch`; when terminal, it creates a concise
  Chinese narrative from returned data and calls `archive_daily_report` once.
  It stops without writing when the batch is active.

The cloud runbook installs Hermes Gateway as a boot-time service owned by
`ubuntu`, creates local-delivery Cron jobs for 08:00 and 12:00, initially
pauses them, and validates each manually with `hermes cron run` before resume.
No Telegram, Discord, or other gateway destination is configured.

## Safety And Acceptance

The archive tool always appends the project paper-trading disclaimer. It stores
only normalized request data and safe data already persisted in sessions. It
does not place orders, create reviews, modify Hermes memory, deliver messages,
or use exchange credentials.

Tests use temporary directories and fake workers. They cover request
normalization, batch idempotency, conflict rejection, partial submission
persistence, aggregate state, prior snapshot lookup, active-batch rejection,
degraded archival, fixed disclaimer, archive immutability, file permissions,
strict MCP inputs, and static skill/runbook safeguards.

Phase 3 is accepted when a manual submit creates exactly one batch, detached
workers complete independently, a later archive creates one owner-only report,
and repeated archive ticks cannot overwrite it. No external delivery, public
port, review, Hermes memory update, or real order may occur.
