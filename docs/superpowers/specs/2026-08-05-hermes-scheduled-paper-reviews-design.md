# Hermes Scheduled Paper Reviews Design

## Goal

Automatically evaluate every newly archived daily BTC, ETH, and SOL paper-trading
decision at T+1, T+7, and T+15. Each eligible evaluation creates an immutable
project review, updates the existing per-symbol learning index, promotes the exact
lesson through Hermes's built-in memory tool, and verifies all three persisted
records. No script may modify Hermes `MEMORY.md` directly.

## Scope And Time Semantics

- The feature applies only to daily report archives created after deployment.
  Existing archives are not scanned or backfilled automatically.
- Each archived batch produces up to nine schedule items: BTC, ETH, and SOL at
  T+1, T+7, and T+15.
- A horizon is a UTC calendar review date derived from the report trade date.
  For example, a 2026-08-05 trade date has review dates 2026-08-06,
  2026-08-12, and 2026-08-20.
- A review becomes executable only when `review_date < current UTC date`. This
  waits for the review date to finish before requesting its historical daily
  price. The normal Shanghai execution is therefore the following morning.
- Only completed analysis sessions are scored. Failed, missing, unreadable, or
  submission-failed items are recorded as skipped and never produce a lesson.
- The original Markdown daily report remains immutable. Review artifacts and
  learning state remain separate project-owned data.

## Architecture

The existing 08:00 submit and 12:00 archive jobs remain no-agent jobs. A
successful new archive opts into scheduled review version 1 and creates a
durable project schedule. Two new jobs then divide deterministic project work
from Hermes-owned memory mutation:

```text
08:00 daily report submit (--no-agent)
  -> create BTC/ETH/SOL analysis sessions

12:00 daily report archive (--no-agent)
  -> archive terminal report
  -> create review_schedules/<trade_date>.json for a new archive

08:15 scheduled review processor (--no-agent)
  -> process due review_pending items
  -> fetch same-provider historical USD references
  -> create immutable reviews
  -> upsert per-symbol learning indexes
  -> transition successful items to memory_pending

08:30 scheduled memory promoter (Hermes Agent + dedicated skill)
  -> list the oldest memory_pending items
  -> call Hermes memory(action=add) with each exact lesson
  -> verify review, learning index, and memory consistency
  -> transition verified items to completed
```

The 08:15 processor calls the existing deterministic review implementation
directly inside the project Python environment. It does not start an LLM or an
MCP stdio client. The 08:30 Agent job does not perform price resolution or
review generation; its only privileged responsibility is using Hermes's
built-in memory tool.

## Persistent Model

Schedules live under the configured results root:

```text
results/hermes/
  review_schedules/<YYYY-MM-DD>.json
  reviews/<review_id>.json
  memories/<SYMBOL>.json
```

`ScheduledReviewPlan` is a schema-version-1 record containing the report batch
ID, trade date, creation time, and ordered items. Each item contains:

- symbol and optional session ID;
- `horizon_days`, restricted to 1, 7, or 15;
- exact UTC `review_date`;
- deterministic `review_id`, derived from session ID and review date when a
  session exists;
- state, attempt count, safe last error code, and update timestamp; and
- optional verification timestamp or skip reason.

The state machine is:

```text
review_pending -> memory_pending -> completed
       |                 |
       +-> skipped       +-> attention_required
```

`skipped` is terminal for a source session that cannot be reviewed.
`attention_required` is terminal for automatic processing and indicates a
memory or three-store consistency problem requiring operator review.

Schedule writes are atomic and protected by an exclusive project lock. The
processor also holds a singleton execution lock so a manual run and a Cron run
cannot process the same due queue concurrently.

## New-Archive Enrollment

`DailyReportArchive` gains an optional scheduled-review version marker. New
archives created by the updated code receive version 1. Previously persisted
archives load with no marker and are never enrolled automatically.

After saving new archive metadata, the archive operation creates or repairs the
matching schedule idempotently. If the process stops after archive persistence
but before schedule creation, a retry sees the version marker and repairs the
missing schedule. Re-reading an old archive without the marker does not create
a schedule.

A ready archive normally creates nine `review_pending` items. A degraded
archive still creates the full ordered plan, but items without completed
sessions start as `skipped`. This preserves an auditable explanation for every
requested symbol and horizon without inventing outcomes.

## Review Processing

The processor selects items in review-date, trade-date, symbol, and horizon
order. It processes only `review_pending` items whose review date is strictly
before the current UTC date.

For each eligible item it reuses `review_paper_decision_impl`, which:

1. loads the completed analysis session;
2. resolves exact trade-date and review-date USD references from one provider;
3. extracts BUY, SELL, HOLD, or UNPARSEABLE from the persisted decision;
4. calculates the raw percentage return and deterministic verdict;
5. writes `results/hermes/reviews/<review_id>.json`; and
6. upserts the lesson into `results/hermes/memories/<SYMBOL>.json`.

The existing verdict rules remain unchanged:

- BUY is correct when the price rises and incorrect when it falls.
- SELL is correct when the price falls and incorrect when it rises.
- A zero return is flat.
- HOLD and UNPARSEABLE are not scored.

The review records the derived T+N horizon alongside its dates, action,
same-provider prices, return, verdict, and exact memory entry. Existing manual
reviews remain readable and compatible.

Price resolution remains fail-closed in this order:

```text
CoinGecko -> CryptoCompare -> Coinbase
```

The two dates may not be mixed across providers, replaced with a neighboring
date, or substituted with an incomplete current-day price.

Successful review and learning writes transition the schedule item to
`memory_pending`. A transient price or project-storage failure leaves it
`review_pending`, increments its attempt count, records only a safe error code,
and permits a later daily retry.

## Hermes Memory Promotion

A new source-controlled Hermes skill handles scheduled memory promotion. It is
installed under `~/.hermes/skills` and attached only to the 08:30 Agent Cron.
It never runs merely because a review exists.

The skill follows this bounded workflow:

1. Run a project read-only command that returns at most 18 oldest
   `memory_pending` items with review ID and exact `hermes_memory_entry`.
2. For each item, call the Hermes built-in memory tool exactly once with
   `action=add`, target `memory`, and the exact entry.
3. Continue that item only when the result is `Entry added` or
   `Entry already exists`.
4. Run the project confirmation command for the review ID. The command reuses
   `verify_review_consistency`, which reads the canonical review, symbol index,
   and Hermes memory file.
5. Mark the schedule item completed only when the review exists, the learning
   index contains it, and the exact memory entry occurs once.

The confirmation command may read `MEMORY.md` but never writes it. Its only
write is the project-owned schedule transition. A verification mismatch moves
the item to `attention_required` and returns a safe nonzero result. Operators
must use targeted Hermes memory-tool `replace` or `remove` operations for an
approved repair; shell editing remains forbidden.

If the Agent stops after memory add but before confirmation, the item remains
`memory_pending`. On the next run the memory tool reports `Entry already
exists`, after which confirmation can complete. A non-success memory-tool
result is not retried for that item during the same Agent run, but other items
may continue.

The normal arrival rate is nine items per day. A limit of 18 allows one missed
daily run to catch up in the next run while bounding Agent tool calls and
context size. Larger backlogs drain oldest-first over subsequent runs.

## Commands And Deployment

The project adds a scheduled-review domain/store module, a safe runner and
bootstrap, an owner-executable no-agent wrapper, and a Hermes Agent skill. The
runner exposes three modes:

- `process-due`: perform eligible deterministic reviews and index updates;
- `memory-pending --limit 18`: return safe pending memory work without
  changing state; and
- `confirm-memory --review-id <id>`: verify the three stores and update only
  schedule state.

The 08:15 bootstrap loads only these allowlisted values from the mode-600
Hermes configuration before importing provider-dependent code:

```text
TRADINGAGENTS_RESULTS_DIR
COINGECKO_DEMO_API_KEY
COINGECKO_PRO_API_KEY
CRYPTOCOMPARE_API_KEY
```

No project `.env`, second secret file, public endpoint, exchange credential,
or external delivery channel is added.

The runbook creates both jobs paused, validates them manually, and resumes them
only after end-to-end acceptance. The processor job uses `--no-agent --script`
at 08:15. The promoter job uses local delivery, the dedicated skill, and a
fixed prompt at 08:30. All schedules use `Asia/Shanghai`.

## Failure And Recovery Policy

- A missing, failed, or unreadable source session becomes skipped without
  blocking other symbols or horizons.
- An unavailable exact historical price remains retryable and produces no
  review or lesson.
- A review or learning write failure never enters `memory_pending`.
- A failed Agent or memory call leaves the item pending for a later run.
- A duplicate, missing, or inconsistent memory verification becomes
  `attention_required` and does not retry automatically.
- Repeated review requests return the deterministic existing review and repair
  a missing learning entry through the existing upsert behavior.
- Repeated memory adds rely on Hermes's exact-entry deduplication.
- Logs and command output contain IDs, counts, states, and safe error codes;
  they do not print keys, raw provider exceptions, unrelated memory text, or
  paths from private configuration.

Pausing either new Cron job is the operational rollback. Existing reviews,
learning indexes, schedules, reports, and Hermes memory are retained for audit
and recovery; rollback never deletes these directories.

## Testing

Unit and integration tests use temporary results directories, fake historical
price resolvers, and temporary Hermes memory files. Coverage includes:

- schedule schemas, validation, ordering, atomic writes, and locking;
- exactly nine items for a new ready BTC/ETH/SOL archive;
- skipped items for degraded source sessions;
- no enrollment for old archives without the version marker;
- idempotent schedule repair for marked new archives;
- T+1, T+7, and T+15 date calculation;
- strict `review_date < current UTC date` eligibility;
- correct, incorrect, flat, and not-scored verdicts;
- successful review/index creation and state transition;
- retryable price and storage failures;
- idempotent review and learning-index retries;
- bounded, oldest-first memory-pending output;
- memory add crash recovery and exact-entry deduplication;
- successful verifier confirmation;
- missing and duplicate memory transitions to `attention_required`;
- safe JSON failure envelopes with no secrets or tracebacks;
- wrapper, skill, and runbook checks that forbid direct `MEMORY.md` writes;
- 08:15 no-agent and 08:30 dedicated-skill Cron constraints; and
- regressions for current submit, archive, maintenance, direct review, and
  manual paper-review workflows.

An end-to-end test archives a three-symbol batch, creates nine schedule items,
advances through a completed T+1 UTC date, produces exactly three reviews and
learning entries, simulates Hermes memory adds, confirms those three items,
and verifies that T+7 and T+15 remain pending.

## Acceptance Criteria

The feature is accepted when a newly archived BTC/ETH/SOL report creates one
auditable nine-item schedule; the no-agent processor generates only fully due
T+1/T+7/T+15 reviews and symbol lessons; the Agent Cron writes every exact
lesson only through Hermes's memory tool; and confirmation reaches completed
only with consistent review, index, and memory state.

Old reports remain unenrolled, original reports remain immutable, failures are
independently retryable or quarantined, and no path can place an order, use
exchange credentials, deliver externally, or write `MEMORY.md` directly.
