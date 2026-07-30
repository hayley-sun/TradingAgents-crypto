# Hermes Scheduled Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an idempotent Hermes-Cron daily BTC/ETH/SOL research batch and immutable project-local Markdown archive without real trading, reviews, Hermes memory writes, or external delivery.

**Architecture:** A new filesystem-backed report module owns normalized batch manifests, state aggregation, prior signal snapshots, and atomic archives. Strict MCP tools submit detached analyses, load a date-keyed batch, and archive a narrative only after items are terminal. A Hermes skill and cloud runbook create two short local Cron jobs.

**Tech Stack:** Python 3.10+, Pydantic v2, FastMCP, `unittest`, `fcntl`, `hashlib`, existing async Hermes MCP session store, Hermes Agent Cron.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `tradingagents/integrations/schemas.py` | Strict daily request, batch, item, state, and archive models. |
| `tradingagents/integrations/hermes_reports.py` | Locked atomic batch store, aggregation, prior snapshots, and Markdown writing. |
| `tradingagents/integrations/hermes_mcp.py` | MCP wrappers and safe errors. |
| `tests/test_hermes_reports.py` | Batch and archive tests with temporary directories. |
| `tests/test_hermes_schemas.py` | Daily request and record validation. |
| `tests/test_hermes_mcp.py` | Public tool envelopes and strict inputs. |
| `tests/test_hermes_review_verifier.py` | Skill and runbook static checks. |
| `deploy/hermes/skills/tradingagents-daily-report/SKILL.md` | Submit/archive guardrails. |
| `docs/hermes_integration.md` | Cloud Gateway and paused Cron operations. |

### Task 1: Define Daily Report Schemas

**Files:**
- Modify: `tradingagents/integrations/schemas.py`
- Modify: `tests/test_hermes_schemas.py`

- [ ] **Step 1: Write failing tests for request normalization and item invariants.**

```python
def test_daily_report_request_normalizes_unique_symbols(self):
    request = DailyReportRequest(
        trade_date="2026-07-29",
        symbols=["btc", "ETH", "sol"],
        analysts=["market", "news", "fundamentals"],
        research_depth=1,
        llm_provider="deepseek",
        quick_model="deepseek-v4-flash",
        deep_model="deepseek-v4-pro",
    )
    self.assertEqual(request.symbols, ["BTC", "ETH", "SOL"])

def test_daily_batch_item_requires_exactly_one_outcome(self):
    with self.assertRaises(ValidationError):
        DailyReportBatchItem(symbol="BTC")
```

- [ ] **Step 2: Run the focused test and confirm RED.**

```bash
$VENV -m unittest tests.test_hermes_schemas.HermesSchemaTests.test_daily_report_request_normalizes_unique_symbols -v
```

Expected: import or name failure for `DailyReportRequest`.

- [ ] **Step 3: Implement the strict models.**

```python
class DailyReportRequest(_StrictModel):
    trade_date: date
    symbols: list[str] = Field(min_length=1, max_length=5)
    analysts: list[str] = Field(min_length=1, max_length=4)
    research_depth: Literal[1, 3, 5]
    llm_provider: str
    quick_model: str = Field(max_length=200)
    deep_model: str = Field(max_length=200)
```

Reuse `AnalysisRequest` normalization for symbols, analysts, provider, and
model values. Add strict batch, item, archive, and aggregate summary models.
Use a model validator to require exactly one item outcome: opaque session ID
or safe submission error.

- [ ] **Step 4: Run all schema tests and commit.**

```bash
$VENV -m unittest tests.test_hermes_schemas -v
git add tradingagents/integrations/schemas.py tests/test_hermes_schemas.py
git commit -m "feat: add daily report batch schemas"
```

Expected: all schema tests pass.

### Task 2: Persist Batches And Aggregate Session State

**Files:**
- Create: `tradingagents/integrations/hermes_reports.py`
- Create: `tests/test_hermes_reports.py`

- [ ] **Step 1: Write failing persistence tests.**

```python
def test_create_or_load_returns_one_batch_for_matching_request(self):
    first = self.store.create_or_load(self.request, self.fake_start)
    second = self.store.create_or_load(self.request, self.fake_start)
    self.assertEqual(first.batch.batch_id, second.batch.batch_id)
    self.assertEqual(self.fake_start.calls, ["BTC", "ETH", "SOL"])

def test_create_or_load_rejects_changed_request_for_same_date(self):
    self.store.create_or_load(self.request, self.fake_start)
    changed = self.request.model_copy(update={"research_depth": 3})
    with self.assertRaises(ReportBatchConflict):
        self.store.create_or_load(changed, self.fake_start)
```

- [ ] **Step 2: Run the new test module and confirm RED.**

```bash
$VENV -m unittest tests.test_hermes_reports -v
```

Expected: `ModuleNotFoundError` for `hermes_reports`.

- [ ] **Step 3: Implement locked storage and submission.**

```python
class ReportBatchStore:
    def create_or_load(self, request, starter):
        with self._exclusive_lock():
            existing = self.load(request.trade_date)
            if existing is not None:
                self._require_matching_request(existing.request, request)
                return BatchLookup(batch=existing, created=False)
            batch = DailyReportBatch.new(request)
            self.save(batch)
            for symbol in request.symbols:
                batch = batch.with_item(self._submit_item(symbol, request, starter))
                self.save(batch)
            return BatchLookup(batch=batch, created=True)
```

Use `fcntl.flock`, `NamedTemporaryFile`, `fsync`, and `os.replace`. Persist an
item after every launch attempt. Store safe submit errors instead of raw
exceptions. Implement `summarize()` as a read-only session aggregation that
returns `active`, `ready`, or `degraded` without provider access.

- [ ] **Step 4: Add state tests, then verify and commit.**

```python
def test_summary_is_active_until_every_session_is_terminal(self):
    self.assertEqual(self.store.summarize(self.batch, self.sessions).state, "active")

def test_summary_is_degraded_when_a_session_failed(self):
    self.assertEqual(self.store.summarize(self.failed_batch, self.sessions).state, "degraded")
```

```bash
$VENV -m unittest tests.test_hermes_reports -v
git add tradingagents/integrations/hermes_reports.py tests/test_hermes_reports.py
git commit -m "feat: persist Hermes daily report batches"
```

Expected: all batch persistence and aggregation tests pass.

### Task 3: Write Immutable Markdown Archives

**Files:**
- Modify: `tradingagents/integrations/hermes_reports.py`
- Modify: `tests/test_hermes_reports.py`

- [ ] **Step 1: Write failing archive tests.**

```python
def test_archive_rejects_active_batch_without_a_file(self):
    with self.assertRaises(ReportBatchActive):
        self.store.archive(self.active_batch, self.sessions, "narrative")
    self.assertFalse((self.reports_root / "2026-07-29.md").exists())

def test_archive_is_immutable_and_has_disclaimer_and_mode(self):
    first = self.store.archive(self.ready_batch, self.sessions, "narrative")
    second = self.store.archive(self.ready_batch, self.sessions, "narrative")
    self.assertEqual(first.sha256, second.sha256)
    self.assertIn(PAPER_TRADING_DISCLAIMER, first.path.read_text(encoding="utf-8"))
    self.assertEqual(stat.S_IMODE(first.path.stat().st_mode), 0o600)
```

- [ ] **Step 2: Run the focused test and confirm RED.**

```bash
$VENV -m unittest tests.test_hermes_reports.HermesReportsTests.test_archive_is_immutable_and_has_disclaimer_and_mode -v
```

Expected: failure because `archive()` does not exist.

- [ ] **Step 3: Implement rendering and immutable writes.**

```python
def archive(self, batch, sessions, narrative):
    summary = self.summarize(batch, sessions)
    if summary.state == "active":
        raise ReportBatchActive()
    document = render_report(batch, summary, narrative, self.previous_snapshot(batch.trade_date))
    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
    return self._write_or_match_archive(batch, document, digest, summary)
```

Reject blank or over-20,000-character narratives. Render fixed request,
snapshot, comparison, and paper-trading sections in code. Write reports mode
`0600`, save digest/snapshot in the batch, permit only digest-identical retry,
and select the latest earlier archive for comparison context.

- [ ] **Step 4: Add degraded and prior-snapshot tests, then verify and commit.**

```python
def test_archive_writes_degraded_report_for_terminal_failure(self):
    archive = self.store.archive(self.degraded_batch, self.sessions, "narrative")
    self.assertEqual(archive.state, "degraded")

def test_previous_snapshot_uses_latest_archived_batch(self):
    snapshot = self.store.previous_snapshot(date(2026, 7, 30))
    self.assertEqual(snapshot.trade_date, date(2026, 7, 29))
```

```bash
$VENV -m unittest tests.test_hermes_reports -v
git add tradingagents/integrations/hermes_reports.py tests/test_hermes_reports.py
git commit -m "feat: archive immutable Hermes daily reports"
```

Expected: all archive tests pass.

### Task 4: Expose Strict MCP Tools

**Files:**
- Modify: `tradingagents/integrations/hermes_mcp.py`
- Modify: `tests/test_hermes_mcp.py`

- [ ] **Step 1: Write failing MCP helper tests.**

```python
def test_daily_report_batch_rejects_unknown_input(self):
    result = start_daily_report_batch_impl({"trade_date": "2026-07-29", "unknown": True})
    self.assertFalse(result["ok"])
    self.assertEqual(result["error"]["code"], "INVALID_REPORT_REQUEST")

def test_archive_daily_report_rejects_active_batch(self):
    result = archive_daily_report_impl("2026-07-29", "narrative", batch_store=self.store)
    self.assertEqual(result["error"]["code"], "REPORT_BATCH_ACTIVE")
```

- [ ] **Step 2: Run the focused MCP test and confirm RED.**

```bash
$VENV -m unittest tests.test_hermes_mcp.HermesMcpTests.test_daily_report_batch_rejects_unknown_input -v
```

Expected: import or name failure for the daily report helper.

- [ ] **Step 3: Implement public wrappers.**

```python
@MCP.tool()
def start_daily_report_batch(
    trade_date: str,
    symbols: list[str],
    analysts: list[str],
    research_depth: int,
    llm_provider: str,
    quick_model: str,
    deep_model: str,
):
    return start_daily_report_batch_impl(
        {
            "trade_date": trade_date,
            "symbols": symbols,
            "analysts": analysts,
            "research_depth": research_depth,
            "llm_provider": llm_provider,
            "quick_model": quick_model,
            "deep_model": deep_model,
        }
    )

@MCP.tool()
def get_daily_report_batch(trade_date: str):
    return get_daily_report_batch_impl(trade_date)

@MCP.tool()
def archive_daily_report(trade_date: str, narrative: str):
    return archive_daily_report_impl(trade_date, narrative)
```

Validate input before invoking the starter and reuse `start_analysis()` for
detached workers. Map expected report exceptions to safe standard envelopes;
map unexpected filesystem errors to generic unreadable or write-failed errors
without a path or raw exception.

- [ ] **Step 4: Run focused tests and commit.**

```bash
$VENV -m unittest tests.test_hermes_mcp tests.test_hermes_reports -v
git add tradingagents/integrations/hermes_mcp.py tests/test_hermes_mcp.py
git commit -m "feat: expose Hermes daily report MCP tools"
```

Expected: all MCP and report tests pass.

### Task 5: Add Cron Skill And Cloud Runbook

**Files:**
- Create: `deploy/hermes/skills/tradingagents-daily-report/SKILL.md`
- Modify: `docs/hermes_integration.md`
- Modify: `tests/test_hermes_review_verifier.py`

- [ ] **Step 1: Write failing static deployment test.**

```python
def test_daily_report_skill_and_runbook_keep_reports_local(self):
    skill = DAILY_REPORT_SKILL_PATH.read_text(encoding="ascii")
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")
    self.assertIn("start_daily_report_batch", skill)
    self.assertIn("archive_daily_report", skill)
    self.assertNotIn("review_paper_decision", skill)
    self.assertIn("gateway install --system --run-as-user ubuntu", runbook)
    self.assertIn("--deliver local", runbook)
```

- [ ] **Step 2: Run the static test and confirm RED.**

```bash
$VENV -m unittest tests.test_hermes_review_verifier.HermesReviewVerifierTests.test_daily_report_skill_and_runbook_keep_reports_local -v
```

Expected: failure because the skill and runbook section are absent.

- [ ] **Step 3: Add the skill and exact deployment runbook.**

The skill prohibits review, Hermes memory, exchange credentials, external
delivery, terminal report writes, and real orders. The runbook installs the
skill mode `600`, starts Gateway as `ubuntu`, creates local 08:00 submit and
12:00 archive jobs paused, manually runs each, and then resumes them.

- [ ] **Step 4: Run static tests and commit.**

```bash
$VENV -m unittest tests.test_hermes_review_verifier -v
git add deploy/hermes/skills/tradingagents-daily-report/SKILL.md docs/hermes_integration.md tests/test_hermes_review_verifier.py
git commit -m "docs: add Hermes daily report cron runbook"
```

Expected: all deployment static tests pass.

### Task 6: Verify And Integrate

**Files:** Verify only.

- [ ] **Step 1: Run complete verification.**

```bash
$VENV -m unittest discover -s tests -v
$VENV -m py_compile tradingagents/integrations/schemas.py tradingagents/integrations/hermes_reports.py tradingagents/integrations/hermes_mcp.py
git diff --check
```

Expected: all tests pass, compilation succeeds, and diff check emits no output.

- [ ] **Step 2: Commit documentation and create a pull request.**

```bash
git add docs/superpowers/specs/2026-07-29-hermes-scheduled-reports-design.md docs/superpowers/plans/2026-07-29-hermes-scheduled-reports.md
git commit -m "docs: plan Hermes scheduled reports"
git push -u origin feature/hermes-scheduled-reports
gh pr create --base main --head feature/hermes-scheduled-reports --title "feat: schedule Hermes daily research reports" --body "Adds idempotent daily report batches, immutable project-local archives, and Hermes Cron runbook."
```

Cloud deployment uses the merged commit, retains this feature branch, runs the
full test suite, installs the skill mode `600`, and follows the paused-Cron
manual validation sequence in `docs/hermes_integration.md`.
