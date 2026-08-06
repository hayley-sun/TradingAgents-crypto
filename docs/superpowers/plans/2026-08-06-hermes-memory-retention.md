# Hermes Memory Retention Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep active T+1/T+7/T+15 report entries in Hermes memory, retain only the five newest confirmed T+15 entries per symbol, and preserve every project-owned report artifact forever.

**Architecture:** A locked per-symbol retirement journal is separate from immutable report records. Existing report promotion remains add/replace; a second Hermes Agent built-in-memory remove protocol retires only revision-3-confirmed reports beyond the five-entry final limit. Hermes memory entries are compact derivatives, while the full project lesson and index remain unchanged.

**Tech Stack:** Python 3.10+, Pydantic v2, fcntl locks and atomic JSON replacement, FastMCP runner CLI, Hermes built-in memory tool, unittest.

---

## Working Rules

Run commands from:

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.worktrees/hermes-report-level-learning
~~~

Use:

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python
~~~

Read both approved designs. All tests use TemporaryDirectory and a fake Hermes
store with the real \n§\n delimiter. Project code may only read a supplied
memory file to return zero-marker or count-only metadata; it must never write
or expose raw memory text.

### Task 1: Retirement Contracts and Journal

**Files:**
- Modify: tradingagents/integrations/schemas.py:648-805
- Create: tradingagents/integrations/hermes_report_retention.py
- Modify: tests/test_hermes_schemas.py
- Modify: tests/test_hermes_report_memory.py

- [ ] **Step 1: Write failing contract and selection tests**

~~~
def test_retirement_journal_rejects_duplicate_sessions_and_invalid_state(self):
    item = ReportMemoryRetirement(
        session_id="hermes_0123456789abcdef", symbol="btc",
        trade_date=date(2026, 7, 1), revision=3, state="pending",
        created_at=utc_now(), updated_at=utc_now(),
    )
    self.assertEqual(
        ReportMemoryRetirementJournal(symbol="BTC", items=[item]).symbol, "BTC"
    )
    with self.assertRaises(ValidationError):
        ReportMemoryRetirementJournal(symbol="BTC", items=[item, item])

def test_sync_selects_only_oldest_completed_reports_beyond_five(self):
    # Seven confirmed BTC T+15 reports plus one confirmed T+7 report.
    # The two oldest final reports are pending; the T+7 report is never selected.
    items = retirement_store.sync_symbol("BTC", records)
    self.assertEqual([item.session_id for item in items], ["hermes_0000000000000001", "hermes_0000000000000002"])
~~~

- [ ] **Step 2: Verify RED**

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_schemas tests.test_hermes_report_memory -v
~~~

Expected: retirement model and store imports fail.

- [ ] **Step 3: Implement strict models and atomic storage**

Add ReportMemoryRetirement with immutable session identity, normalized symbol, fixed
revision 3, timestamps, safe error code, and these states:

~~~
pending | memory_call_started | verification_pending | retired | attention_required
~~~

Add ReportMemoryRetirementJournal with unique session IDs. Implement
ReportMemoryRetirementStore at results/hermes/report_memory_retirements/<SYMBOL>.json,
following ReportLearningStore lock and atomic-replace discipline.

sync_symbol(symbol, records) selects only records whose confirmed_revision is 3
and whose third revision is confirmed. Sort (trade_date, session_id) descending,
keep five, and create journal items only for older completed sessions. Never
retire an active/pending/failed report and never alter report records.

- [ ] **Step 4: Verify GREEN and commit**

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_schemas tests.test_hermes_report_memory -v
git add tradingagents/integrations/schemas.py tradingagents/integrations/hermes_report_retention.py tests/test_hermes_schemas.py tests/test_hermes_report_memory.py
git commit -m "feat: track bounded Hermes memory retirements"
~~~

### Task 2: Compact Hermes Entry and Capacity Verifier

**Files:**
- Modify: tradingagents/integrations/hermes_report_learning.py:36-66,516-700
- Modify: tradingagents/integrations/hermes_report_memory_verifier.py
- Modify: tests/test_hermes_report_learning.py
- Modify: tests/test_hermes_report_memory.py

- [ ] **Step 1: Write failing rendering and count-only tests**

~~~
def test_compact_hermes_entry_stays_bounded_with_chinese_reflection(self):
    rendered = _render_reflection(record, 3, maximal_chinese_reflection())
    self.assertLessEqual(len(rendered.hermes_memory_entry), 512)
    self.assertLessEqual(len(rendered.hermes_memory_entry.encode("utf-8")), 1536)
    self.assertIn(REPORT_MEMORY_MARKER.format(session_id=SESSION_ID), rendered.hermes_memory_entry)
    self.assertIn("T+15", rendered.hermes_memory_entry)
    self.assertGreater(len(rendered.lesson), len(rendered.hermes_memory_entry))

def test_capacity_verifier_exposes_counts_not_memory_text(self):
    result = verify_report_memory_capacity(memory_path, memory_char_limit=40000)
    self.assertEqual(result.reserved_report_chars, 30897)
    self.assertNotIn("private memory", result.model_dump_json())
~~~

- [ ] **Step 2: Verify RED**

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_report_learning tests.test_hermes_report_memory -v
~~~

Expected: current entry exceeds 512 characters and capacity API is absent.

- [ ] **Step 3: Implement compact rendering and safe arithmetic**

Define source-controlled constants:

~~~
HERMES_REPORT_MEMORY_MAX_CHARS = 512
HERMES_REPORT_MEMORY_MAX_BYTES = 1536
HERMES_MEMORY_CHAR_LIMIT = 40000
HERMES_REPORT_ENTRY_RESERVATION = 60
HERMES_ENTRY_DELIMITER_CHARS = 3
~~~

Render Hermes content independently of the full project lesson. It includes the
marker, identity/action/maturity, outcomes, clipped decision-time context, one
strength-or-mistake, one hypothesis, one check, and the paper-trading disclaimer.
Use character and byte clipping; drop optional content before clipping mandatory
sections.

Add a read-only verifier that returns booleans and numbers only. Reserve
60 * 512 + 59 * 3 = 30897 characters. Fail closed for unreadable memory, a limit
below 40000, or current usage above 9000.

- [ ] **Step 4: Verify GREEN and commit**

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_report_learning tests.test_hermes_report_memory -v
git add tradingagents/integrations/hermes_report_learning.py tradingagents/integrations/hermes_report_memory_verifier.py tests/test_hermes_report_learning.py tests/test_hermes_report_memory.py
git commit -m "feat: bound Hermes report memory capacity"
~~~

### Task 3: Agent-Owned Retirement Protocol

**Files:**
- Modify: tradingagents/integrations/hermes_report_memory.py
- Modify: tradingagents/integrations/hermes_report_memory_verifier.py
- Modify: tradingagents/integrations/hermes_report_retention.py
- Modify: tests/test_hermes_report_memory.py

- [ ] **Step 1: Write failing removal lifecycle tests**

~~~
def test_retirement_removes_one_old_completed_marker_after_t15_confirmation(self):
    item = sync_seven_completed_btc_records()[0]
    operation = begin_report_memory_retirement(store, "BTC", item.session_id)
    self.assertEqual(operation.action, "remove")
    self.assertEqual(operation.old_text, REPORT_MEMORY_MARKER.format(session_id=item.session_id))
    self.assertEqual(fake_memory.apply("remove", old_text=operation.old_text), "Entry removed")
    retired = confirm_report_memory_retirement(retirement_store, "BTC", item.session_id, absence_verifier)
    self.assertEqual(retired.state, "retired")

def test_duplicate_or_missing_retirement_marker_requires_attention(self):
    record = confirm_report_memory_retirement(retirement_store, "BTC", item.session_id, lambda *_: False)
    self.assertEqual(record.state, "attention_required")
~~~

- [ ] **Step 2: Verify RED**

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_report_memory -v
~~~

Expected: retirement work, begin/confirm functions and absence verifier are missing.

- [ ] **Step 3: Implement state transitions**

Add ReportMemoryRetirementWork and ReportMemoryRetirementOperation with only
action=remove and old_text=<stable marker>. Implement bounded list, begin,
confirm and quarantine functions. Begin transitions pending to
memory_call_started atomically and is idempotent for memory_call_started and
verification_pending. Confirm persists verification_pending before a read-only
zero-marker check, then marks only the journal item retired. Missing/duplicate
markers and failed verification enter attention_required. No function may alter
report records, index entries, reviews, or MEMORY.md.

- [ ] **Step 4: Verify GREEN and commit**

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_report_memory -v
git add tradingagents/integrations/hermes_report_memory.py tradingagents/integrations/hermes_report_memory_verifier.py tradingagents/integrations/hermes_report_retention.py tests/test_hermes_report_memory.py
git commit -m "feat: retire completed Hermes report entries"
~~~

### Task 4: Safe Runner Commands

**Files:**
- Modify: tradingagents/integrations/hermes_scheduled_review_runner.py:250-500
- Modify: tests/test_hermes_scheduled_review_runner.py

- [ ] **Step 1: Write failing runner tests**

~~~
def test_retirement_runner_hides_marker_until_begin(self):
    code, listing = runner.run_report_memory_retirement_pending(18, lister)
    self.assertEqual(code, 0)
    self.assertNotIn("old_text", listing["items"][0])
    code, begin = runner.run_begin_report_memory_retirement("BTC", SESSION_ID, starter)
    self.assertEqual(begin["action"], "remove")
    self.assertIn("old_text", begin)

def test_capacity_runner_returns_only_counts_and_rejects_wrong_limit(self):
    code, payload = runner.run_report_memory_capacity(memory_path, 40000, verifier)
    self.assertEqual((code, payload["reserved_report_chars"]), (0, 30897))
    self.assertNotIn("memory_text", payload)
~~~

- [ ] **Step 2: Verify RED**

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_scheduled_review_runner -v
~~~

Expected: retirement and capacity modes are unavailable.

- [ ] **Step 3: Implement parser and JSON modes**

Add:

~~~
report-memory-retirement-pending --limit 18
begin-report-memory-retirement --symbol <symbol> --session-id <session_id>
confirm-report-memory-retirement --symbol <symbol> --session-id <session_id>
quarantine-report-memory-retirement --symbol <symbol> --session-id <session_id> --error-code <allowlisted>
report-memory-capacity --hermes-memory-path <path> --memory-char-limit 40000
~~~

List output includes only symbol, session ID, trade date, revision and state.
Only begin returns old_text. Capacity returns only count metadata. Reject invalid
IDs, limits above 18, capacity other than 40000 and unallowlisted error codes
before any store access.

- [ ] **Step 4: Verify GREEN and commit**

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_scheduled_review_runner -v
git add tradingagents/integrations/hermes_scheduled_review_runner.py tests/test_hermes_scheduled_review_runner.py
git commit -m "feat: expose safe Hermes retirement commands"
~~~

### Task 5: Scheduled Skill and Deployment Runbook

**Files:**
- Modify: deploy/hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md
- Modify: docs/hermes_integration.md:332-630
- Modify: tests/test_hermes_review_verifier.py

- [ ] **Step 1: Write failing workflow tests**

~~~
def test_scheduled_skill_uses_agent_owned_completed_report_retirement(self):
    skill = SCHEDULED_REVIEW_SKILL_PATH.read_text(encoding="ascii")
    self.assertIn("report-memory-retirement-pending --limit 18", skill)
    self.assertIn("begin-report-memory-retirement", skill)
    self.assertIn("memory(action=remove,target=memory,old_text=", skill)
    self.assertIn("confirm-report-memory-retirement", skill)
    self.assertNotIn("memory(action=read", skill)

def test_runbook_documents_capacity_and_active_report_protection(self):
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    self.assertIn("memory_char_limit: 40000", text)
    self.assertIn("report-memory-capacity", text)
    self.assertIn("进行中", text)
    self.assertIn("每个币种 5", text)
~~~

- [ ] **Step 2: Verify RED**

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_review_verifier -v
~~~

Expected: retention and capacity assertions fail.

- [ ] **Step 3: Update source-controlled protocol**

After report promotions, the skill drains up to 18 retirement items. It makes
exactly one Hermes memory(action=remove,target=memory,old_text=<returned marker>)
call per begun item, accepts only Entry removed, then confirms. A
verification_pending item retries confirmation only. Any other result quarantines
without printing raw memory.

Runbook config is:

~~~
memory:
  memory_char_limit: 40000
~~~

Before activation, require a count-only preflight with current_chars <= 9000 and
reserved_report_chars == 30897. The acceptance flow keeps an active report
through replace, completes six reports for one symbol, removes only the oldest
final marker, and proves project records/indexes stay intact.

- [ ] **Step 4: Verify GREEN and commit**

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_review_verifier -v
git add deploy/hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md docs/hermes_integration.md tests/test_hermes_review_verifier.py
git commit -m "docs: deploy bounded Hermes report retention"
~~~

### Task 6: End-to-End Retention Coverage and Branch Verification

**Files:**
- Modify: tests/test_hermes_report_memory.py
- Modify: docs/superpowers/plans/2026-08-06-hermes-memory-retention.md only to mark completed checkboxes

- [x] **Step 1: Write the full lifecycle test**

~~~
def test_retention_keeps_active_reports_and_five_newest_completed_entries(self):
    harness = RetentionLifecycleHarness()
    completed = [harness.complete_report(day) for day in range(1, 7)]
    active = harness.promote_through_t7(day=7)
    self.assertEqual(harness.retire_completed_reports(), [completed[0].session_id])
    self.assertTrue(harness.memory_contains(active.session_id))
    self.assertTrue(harness.replace_at_t15(active.session_id))
    self.assertEqual(harness.project_report_record_count(), 7)
    self.assertEqual(harness.project_index_report_count("BTC"), 7)
    self.assertEqual(harness.completed_memory_entry_count("BTC"), 5)
~~~

Also cover cross-symbol isolation, exact \n§\n delimiter semantics and count-only
capacity output.

- [x] **Step 2: Run full verification**

~~~
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest discover -s tests -p 'test_hermes*.py' -v
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest discover -s tests -v
git diff --check
rg -n "MEMORY\\.md" deploy/hermes tradingagents/integrations tests
rg -n "memory_char_limit|report-memory-retirement|scheduled_review_version" tradingagents tests docs/hermes_integration.md
git status --short
~~~

Expected: zero failures, no whitespace errors, no project-side memory writes, and
only intended retention/version references.

- [x] **Step 3: Commit and request final review**

~~~
git add tests/test_hermes_report_memory.py
git commit -m "test: cover bounded Hermes report retention"
~~~

Use superpowers:requesting-code-review against main, address verified findings
using superpowers:receiving-code-review, rerun the complete suite, and use
superpowers:verification-before-completion before presenting branch integration
options.
