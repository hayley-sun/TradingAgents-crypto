# Hermes Scheduled Paper Reviews Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Automatically create T+1, T+7, and T+15 reviews for newly archived daily reports, update per-symbol learning, and promote verified lessons through a dedicated Hermes Agent Cron memory workflow.

**Architecture:** New archives opt into a versioned, project-owned review schedule. A no-agent runner processes fully elapsed UTC review dates through the existing deterministic review implementation, while a separate Hermes skill lists pending lessons, calls the built-in memory tool, and confirms three-store consistency. Atomic schedule state and deterministic review IDs make every boundary retryable.

**Tech Stack:** Python 3.10+, Pydantic v2, unittest, filesystem JSON stores with \`fcntl\` locks, Hermes Cron and skills, existing historical-price resolvers and review verifier.

---

## File Structure

- Modify \`tradingagents/integrations/schemas.py\`: archive marker, review horizon, and schedule records.
- Create \`tradingagents/integrations/hermes_scheduled_reviews.py\`: schedule store and state transitions.
- Modify \`tradingagents/integrations/hermes_learning.py\`: persist and render T+N.
- Modify \`tradingagents/integrations/hermes_reports.py\` and \`hermes_mcp.py\`: enroll new archives.
- Create \`hermes_scheduled_review_runner.py\` and \`hermes_scheduled_review_bootstrap.py\`: safe CLI.
- Create processor wrapper and scheduled-memory Hermes Skill under \`deploy/hermes\`.
- Create focused domain/runner tests and extend existing integration/static tests.
- Modify \`docs/hermes_integration.md\`: install, validation, recovery, and rollback.

### Task 1: Schedule Schemas And Atomic Store

**Files:**
- Modify: \`tradingagents/integrations/schemas.py\`
- Create: \`tradingagents/integrations/hermes_scheduled_reviews.py\`
- Create: \`tests/test_hermes_scheduled_reviews.py\`
- Modify: \`tests/test_hermes_schemas.py\`

- [ ] **Step 1: Write failing tests**

Add tests for strict item validation and nine-item ordered construction:

\`\`\`python
def test_ready_batch_creates_three_horizons_per_symbol(self):
    plan = ScheduledReviewStore(root).create_or_load(batch)
    self.assertEqual(len(plan.items), 9)
    self.assertEqual(
        [(item.symbol, item.horizon_days) for item in plan.items],
        [(s, h) for s in ("BTC", "ETH", "SOL") for h in (1, 7, 15)],
    )

def test_non_skipped_item_requires_session_and_review_ids(self):
    with self.assertRaises(ValidationError):
        ScheduledReviewItem(
            symbol="BTC", horizon_days=1, review_date="2026-08-06",
            state="review_pending", updated_at=utc_now(),
        )
\`\`\`

- [ ] **Step 2: Verify RED**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_scheduled_reviews tests.test_hermes_schemas -v
\`\`\`

Expected: missing scheduled-review records/module.

- [ ] **Step 3: Implement strict records**

Add \`ScheduledReviewItem\` and \`ScheduledReviewPlan\`. Item fields are symbol,
optional session/review IDs, \`Literal[1, 7, 15]\` horizon, review date, five-state
literal, attempts, safe error/skip codes, timestamps, and verified time. Require
identity for non-skipped items and a reason for skipped items.

- [ ] **Step 4: Implement \`ScheduledReviewStore\`**

Provide \`load\`, \`save\`, \`create_or_load\`, ordered \`plans\`, and atomic
update methods. Use ASCII atomic JSON, an \`fcntl\` store lock, and
\`make_review_id\`. Completed archive items start \`review_pending\`; all other
archive statuses create three auditable \`skipped\` items.

- [ ] **Step 5: Verify GREEN and commit**

Run Step 2, then:

\`\`\`bash
git add tradingagents/integrations/schemas.py tradingagents/integrations/hermes_scheduled_reviews.py tests/test_hermes_scheduled_reviews.py tests/test_hermes_schemas.py
git commit -m "feat: add scheduled paper review store"
\`\`\`

### Task 2: Enroll Only New Archives

**Files:**
- Modify: \`tradingagents/integrations/schemas.py\`
- Modify: \`tradingagents/integrations/hermes_reports.py\`
- Modify: \`tradingagents/integrations/hermes_mcp.py\`
- Modify: \`tests/test_hermes_reports.py\`
- Modify: \`tests/test_hermes_mcp.py\`

- [ ] **Step 1: Write failing enrollment tests**

Test that \`archive_daily_report_impl(..., schedule_store=store)\` enrolls a new
archive, an already persisted unmarked archive is not backfilled, and a marked
archive repairs a missing schedule on retry.

- [ ] **Step 2: Verify RED**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_reports tests.test_hermes_mcp -v
\`\`\`

Expected: missing archive marker and schedule-store argument.

- [ ] **Step 3: Add the opt-in marker**

Add \`scheduled_review_version: Literal[1] | None = None\` to
\`DailyReportArchive\`. Extend \`ReportBatchStore.archive\` with a marker argument
that applies only while creating metadata and never upgrades an existing archive.

- [ ] **Step 4: Enroll or repair in the MCP implementation**

Extend \`archive_daily_report_impl\` with optional schedule-store injection. Pass
version 1 for a new production archive, reload it, and call \`create_or_load\`
only when the persisted marker is 1. Return \`REVIEW_SCHEDULE_WRITE_FAILED\` on
schedule failure so retry repairs state without rewriting the report.

- [ ] **Step 5: Verify GREEN and commit**

Run Step 2, stage only the five task files, and commit:

\`\`\`bash
git commit -m "feat: enroll new daily reports for scheduled review"
\`\`\`

### Task 3: Process Due T+N Reviews

**Files:**
- Modify: \`tradingagents/integrations/schemas.py\`
- Modify: \`tradingagents/integrations/hermes_learning.py\`
- Modify: \`tradingagents/integrations/hermes_scheduled_reviews.py\`
- Modify: \`tests/test_hermes_learning.py\`
- Modify: \`tests/test_hermes_scheduled_reviews.py\`

- [ ] **Step 1: Write failing eligibility and retry tests**

\`\`\`python
def test_review_date_must_be_fully_elapsed(self):
    self.assertEqual(process_due_reviews(store, date(2026, 8, 6), reviewer).reviewed_count, 0)
    self.assertEqual(process_due_reviews(store, date(2026, 8, 7), reviewer).reviewed_count, 3)

def test_price_failure_remains_retryable(self):
    process_due_reviews(store, date(2026, 8, 7), failing_reviewer)
    item = store.load(date(2026, 8, 5)).items[0]
    self.assertEqual((item.state, item.attempt_count), ("review_pending", 1))
    self.assertEqual(item.last_error_code, "PRICE_DATA_UNAVAILABLE")
\`\`\`

Also assert new reviews persist the derived horizon and include \`T+N\` in the
exact lesson.

- [ ] **Step 2: Verify RED**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_learning tests.test_hermes_scheduled_reviews -v
\`\`\`

- [ ] **Step 3: Add review horizon compatibility**

Add optional positive \`horizon_days\` to \`PaperDecisionReview\` so old records
still load. New reviews derive it from the dates, and \`_memory_entry\` includes
\`at T+N\` while retaining return, verdict, and paper-trading disclaimer.

- [ ] **Step 4: Implement \`process_due_reviews\`**

Return a frozen count report. Under a singleton execution lock, process only
\`review_pending\` items with \`review_date < current_utc_date\`. Successful,
identity-matching results become \`memory_pending\`; price/storage errors remain
pending with attempts and safe code; terminal session errors become skipped.

- [ ] **Step 5: Verify GREEN and commit**

Run Step 2, stage the five task files, and commit:

\`\`\`bash
git commit -m "feat: process due scheduled paper reviews"
\`\`\`

### Task 4: List And Confirm Hermes Memory Work

**Files:**
- Modify: \`tradingagents/integrations/hermes_scheduled_reviews.py\`
- Modify: \`tests/test_hermes_scheduled_reviews.py\`

- [ ] **Step 1: Write failing queue/confirmation tests**

Test bounded oldest-first listing with exact canonical lesson; successful
verification moves one item to completed with \`verified_at\`; verifier failure
moves it to \`attention_required\` with \`REVIEW_CONSISTENCY_FAILED\`.

- [ ] **Step 2: Verify RED**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_scheduled_reviews -v
\`\`\`

- [ ] **Step 3: Implement memory work APIs**

Add frozen \`ScheduledMemoryWork\`, \`list_pending_memory(store, review_loader,
limit)\`, and \`confirm_scheduled_memory(store, review_id, verifier)\`. Listing
is read-only. Confirmation writes only schedule state and never memory; failures
raise a safe domain exception after quarantining the item.

- [ ] **Step 4: Verify GREEN and commit**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_scheduled_reviews -v
git add tradingagents/integrations/hermes_scheduled_reviews.py tests/test_hermes_scheduled_reviews.py
git commit -m "feat: verify scheduled Hermes memory promotion"
\`\`\`

### Task 5: Safe Runner And Bootstrap

**Files:**
- Create: \`tradingagents/integrations/hermes_scheduled_review_runner.py\`
- Create: \`tradingagents/integrations/hermes_scheduled_review_bootstrap.py\`
- Create: \`tests/test_hermes_scheduled_review_runner.py\`

- [ ] **Step 1: Write failing command tests**

Cover \`process-due\`, \`memory-pending --limit 18\`, and \`confirm-memory
--review-id\`; canonical input validation; safe JSON exception redaction; and
environment loading before import. Assert bootstrap excludes
\`DEEPSEEK_API_KEY\` and unrelated values.

- [ ] **Step 2: Verify RED**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_scheduled_review_runner -v
\`\`\`

- [ ] **Step 3: Implement strict runner modes**

Wire processing to \`review_paper_decision_impl\`, pending work to \`ReviewStore\`,
and confirmation to \`verify_review_consistency\`. Allow explicit UTC date and
memory path for tests. Emit one sorted JSON object and redact unexpected errors
as \`SCHEDULED_REVIEW_RUNNER_FAILED\`.

- [ ] **Step 4: Implement allowlisted bootstrap**

Load only results-dir, CoinGecko keys, and CryptoCompare key from the private
Hermes MCP config before importing the runner. Return the same safe failure
shape for config, import, or startup errors.

- [ ] **Step 5: Verify GREEN and commit**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_scheduled_review_runner -v
git add tradingagents/integrations/hermes_scheduled_review_runner.py tradingagents/integrations/hermes_scheduled_review_bootstrap.py tests/test_hermes_scheduled_review_runner.py
git commit -m "feat: add scheduled paper review commands"
\`\`\`

### Task 6: Wrapper, Skill, And Runbook

**Files:**
- Create: \`deploy/hermes/scripts/tradingagents-scheduled-review-process.sh\`
- Create: \`deploy/hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md\`
- Modify: \`tests/test_hermes_review_verifier.py\`
- Modify: \`docs/hermes_integration.md\`

- [ ] **Step 1: Write failing static deployment tests**

Assert the wrapper invokes only bootstrap \`process-due\` and has no secret names.
Assert the Skill lists at most 18 items, calls memory add exactly once per item,
accepts only added/already-exists, confirms accepted items, never edits
\`MEMORY.md\`, never trades, and continues independent items.

Assert the runbook creates paused local jobs at \`15 8 * * *\` with
\`--no-agent --script\` and \`30 8 * * *\` with the dedicated \`--skill\`, plus
the no-backfill and pause-first rollback rules.

- [ ] **Step 2: Verify RED**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_review_verifier -v
\`\`\`

- [ ] **Step 3: Add wrapper and Skill**

The wrapper executes the project interpreter and scheduled-review bootstrap
\`process-due\`. The Skill obtains pending JSON, invokes only Hermes memory add,
and calls confirm for accepted outcomes. Terminal/file tools may never write
Hermes memory.

- [ ] **Step 4: Update operations documentation**

Document mode-700/600 installation, immediate pause after creation, manual
validation, schedule observation, pending/attention recovery, no old-report
backfill, and rollback by pausing without deleting project or memory data.

- [ ] **Step 5: Verify GREEN and commit**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_review_verifier -v
git add deploy/hermes/scripts/tradingagents-scheduled-review-process.sh deploy/hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md tests/test_hermes_review_verifier.py docs/hermes_integration.md
git commit -m "docs: deploy scheduled paper review jobs"
\`\`\`

### Task 7: End-To-End And Regression Verification

**Files:**
- Modify: \`tests/test_hermes_scheduled_reviews.py\`
- Modify implementation only when the acceptance test exposes a defect.

- [ ] **Step 1: Write the failing end-to-end test**

Archive a temporary three-symbol batch, assert nine items, advance beyond T+1,
produce exactly three reviews/index lessons with fake same-provider prices,
write those exact lessons once to a temporary memory file, confirm all three,
and assert T+7/T+15 remain pending.

- [ ] **Step 2: Verify RED, close the minimum gap, and verify GREEN**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_scheduled_reviews -v
\`\`\`

- [ ] **Step 3: Run focused regression**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest tests.test_hermes_schemas tests.test_hermes_learning tests.test_hermes_reports tests.test_hermes_mcp tests.test_hermes_review_verifier tests.test_hermes_daily_report_runner tests.test_hermes_scheduled_reviews tests.test_hermes_scheduled_review_runner -v
\`\`\`

- [ ] **Step 4: Run full and static verification**

\`\`\`bash
.venv-hermes-mcp/bin/python -m unittest discover -s tests -v
.venv-hermes-mcp/bin/python -m py_compile tradingagents/integrations/hermes_scheduled_reviews.py tradingagents/integrations/hermes_scheduled_review_runner.py tradingagents/integrations/hermes_scheduled_review_bootstrap.py
git diff --check
\`\`\`

Expected: zero failures/errors, all modules compile, and diff check is clean.

- [ ] **Step 5: Review scope and commit final test integration**

\`\`\`bash
git status --short
git diff --stat HEAD
git add tests/test_hermes_scheduled_reviews.py
git commit -m "test: cover scheduled paper review workflow"
\`\`\`

Do not stage the user's unrelated untracked Markdown files.

