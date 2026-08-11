# Hermes Reflection Retry Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce at most one bounded report-reflection attempt per valid session/revision per UTC date, align MCP and Agent Skill no-retry guidance, and preserve the existing strict reflection safety boundary.

**Architecture:** Add a backward-compatible attempt date to each report-learning revision and enforce the retry gate inside the existing report-store lock before bounded validation. Map the durable deferred state to a safe MCP error, teach the scheduled Agent to use calibrated causal language and never retry an item in the same run, and document a new-date acceptance recovery path without resetting failed artifacts.

**Tech Stack:** Python 3.12, Pydantic v2, FastMCP, filesystem JSON with `fcntl` locks and atomic replacement, `unittest`, Hermes Agent skills and Cron.

---

## Working Rules

Run implementation commands from:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.worktrees/hermes-reflection-retry-gate
```

Use this interpreter for all tests:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python
```

Read the approved design before changing production code:

```bash
sed -n '1,260p' docs/superpowers/specs/2026-08-11-hermes-reflection-retry-gate-design.md
```

Never modify or delete server acceptance artifacts, project report records, or Hermes `MEMORY.md`. This plan changes repository code and documentation only.

## File Map

- `tradingagents/integrations/schemas.py`: backward-compatible persisted attempt-date field.
- `tradingagents/integrations/hermes_report_learning.py`: retry-deferred domain exception, UTC date resolution, locked gate, rejection accounting, and concurrency behavior.
- `tradingagents/integrations/hermes_mcp.py`: internal attempt-date injection for tests and safe no-current-run-retry responses.
- `deploy/hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md`: one fetch/submit per item, calibrated causal language, and no same-run retry.
- `docs/hermes_integration.md`: deferred-state diagnosis and new-date acceptance recovery.
- `tests/test_hermes_schemas.py`: old/new JSON compatibility.
- `tests/test_hermes_report_learning.py`: date gate, later-day success, three-day quarantine, and concurrent submission.
- `tests/test_hermes_mcp.py`: MCP error mapping and non-mutating same-day deferred behavior.
- `tests/test_hermes_review_verifier.py`: Skill and runbook static contract tests.

### Task 1: Add the Backward-Compatible Attempt Date

**Files:**
- Modify: `tests/test_hermes_schemas.py`
- Modify: `tradingagents/integrations/schemas.py`

- [ ] **Step 1: Write the failing schema compatibility test**

Add this test to `HermesSchemaTests`:

```python
def test_report_learning_attempt_date_is_backward_compatible(self):
    now = utc_now()
    revision = ReportLearningRevision(
        revision=1,
        outcome_review_ids=["review_0123456789abcdef"],
        reflection_state="pending",
        memory_state="blocked",
        source_fields=[
            ReportSourceMetadata(
                name="report.market",
                sha256="a" * 64,
                truncated=False,
            )
        ],
        created_at=now,
        updated_at=now,
    )
    self.assertTrue(hasattr(revision, "last_reflection_attempt_date"))
    legacy_payload = revision.model_dump(mode="json")
    legacy_payload.pop("last_reflection_attempt_date")

    restored = ReportLearningRevision.model_validate(legacy_payload)
    attempted = restored.model_copy(
        update={"last_reflection_attempt_date": date(2026, 8, 11)}
    )

    self.assertIsNone(restored.last_reflection_attempt_date)
    self.assertEqual(
        attempted.model_dump(mode="json")["last_reflection_attempt_date"],
        "2026-08-11",
    )
```

- [ ] **Step 2: Run the schema test and verify RED**

Run:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_schemas.HermesSchemaTests.test_report_learning_attempt_date_is_backward_compatible -v
```

Expected: FAIL at `assertTrue` because `last_reflection_attempt_date` is not a model field.

- [ ] **Step 3: Add the persisted field**

In `ReportLearningRevision`, immediately after `reflection_attempt_count`, add:

```python
last_reflection_attempt_date: date | None = None
```

Do not add a coherence rule requiring the date when `reflection_attempt_count > 0`; existing persisted rejected records predate this field and must remain readable.

- [ ] **Step 4: Run schema tests and verify GREEN**

Run:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_schemas -v
```

Expected: all schema tests PASS.

- [ ] **Step 5: Commit the schema change**

```bash
git add tradingagents/integrations/schemas.py tests/test_hermes_schemas.py
git commit -m "feat: persist report reflection attempt date"
```

### Task 2: Enforce the Atomic UTC-Date Retry Gate

**Files:**
- Modify: `tests/test_hermes_report_learning.py`
- Modify: `tradingagents/integrations/hermes_report_learning.py`

- [ ] **Step 1: Write failing sequential gate tests**

Import `inspect` and add these tests to `HermesReportLearningTests`. Begin the first test with an API assertion so the initial RED is a clean assertion failure:

```python
def test_rejected_reflection_consumes_only_one_attempt_per_utc_date(self):
    self.assertIn(
        "attempt_date",
        inspect.signature(
            hermes_report_learning.submit_report_reflection
        ).parameters,
    )
    self.assertTrue(
        hasattr(hermes_report_learning, "ReportReflectionRetryDeferred")
    )
    with TemporaryDirectory() as directory:
        report_store, index_store, session = pending_report_fixture(directory)
        payload = valid_reflection_payload()
        payload["overall_assessment"] = "This outcome was guaranteed."

        with self.assertRaises(
            hermes_report_learning.ReportReflectionRejected
        ):
            hermes_report_learning.submit_report_reflection(
                report_store,
                index_store,
                session,
                1,
                payload,
                attempt_date=date(2026, 8, 11),
            )
        first_bytes = report_store.path_for(session.session_id).read_bytes()

        with self.assertRaises(
            hermes_report_learning.ReportReflectionRetryDeferred
        ):
            hermes_report_learning.submit_report_reflection(
                report_store,
                index_store,
                session,
                1,
                payload,
                attempt_date=date(2026, 8, 11),
            )

        persisted = report_store.load(session.session_id)
        self.assertEqual(persisted.revisions[0].reflection_attempt_count, 1)
        self.assertEqual(
            persisted.revisions[0].last_reflection_attempt_date,
            date(2026, 8, 11),
        )
        self.assertEqual(
            report_store.path_for(session.session_id).read_bytes(),
            first_bytes,
        )

def test_rejected_reflection_quarantines_after_three_utc_dates(self):
    with TemporaryDirectory() as directory:
        report_store, index_store, session = pending_report_fixture(directory)
        payload = valid_reflection_payload()
        payload["overall_assessment"] = "This outcome was guaranteed."

        for attempt, attempt_date in enumerate(
            (date(2026, 8, 11), date(2026, 8, 12), date(2026, 8, 13)),
            start=1,
        ):
            with self.assertRaises(
                hermes_report_learning.ReportReflectionRejected
            ):
                hermes_report_learning.submit_report_reflection(
                    report_store,
                    index_store,
                    session,
                    1,
                    payload,
                    attempt_date=attempt_date,
                )
            persisted = report_store.load(session.session_id)
            self.assertEqual(
                persisted.revisions[0].reflection_attempt_count,
                attempt,
            )
            self.assertEqual(
                persisted.revisions[0].reflection_state,
                "attention_required" if attempt == 3 else "pending",
            )

def test_valid_reflection_succeeds_on_later_utc_date(self):
    with TemporaryDirectory() as directory:
        report_store, index_store, session = pending_report_fixture(directory)
        unsafe = valid_reflection_payload()
        unsafe["overall_assessment"] = "This outcome was guaranteed."

        with self.assertRaises(
            hermes_report_learning.ReportReflectionRejected
        ):
            hermes_report_learning.submit_report_reflection(
                report_store,
                index_store,
                session,
                1,
                unsafe,
                attempt_date=date(2026, 8, 11),
            )
        ready = hermes_report_learning.submit_report_reflection(
            report_store,
            index_store,
            session,
            1,
            valid_reflection_payload(),
            attempt_date=date(2026, 8, 12),
        )

        self.assertEqual(ready.reflected_revision, 1)
        self.assertEqual(ready.revisions[0].reflection_state, "ready")
        self.assertEqual(ready.revisions[0].memory_state, "add_pending")
        self.assertEqual(index_store.load("BTC").report_entries[0].session_id, session.session_id)
```

Replace the existing three-immediate-attempt quarantine test with the three-distinct-date version rather than retaining contradictory behavior. Also add this module-level worker beside the existing multiprocessing helpers:

```python
def _concurrent_rejected_reflection(root, session_payload, start, results):
    from tradingagents.integrations import hermes_report_learning as report_module
    from tradingagents.integrations.hermes_learning import LearningStore
    from tradingagents.integrations.schemas import AnalysisSession

    start.wait(timeout=5)
    payload = valid_reflection_payload()
    payload["overall_assessment"] = "This outcome was guaranteed."
    try:
        report_module.submit_report_reflection(
            report_module.ReportLearningStore(Path(root) / "reports"),
            LearningStore(Path(root) / "index"),
            AnalysisSession.model_validate(session_payload),
            1,
            payload,
            attempt_date=date(2026, 8, 11),
        )
    except report_module.ReportReflectionRejected:
        results.put("rejected")
    except report_module.ReportReflectionRetryDeferred:
        results.put("deferred")
```

Add the concurrent test before changing production code:

```python
def test_concurrent_rejections_consume_one_utc_date_attempt(self):
    with TemporaryDirectory() as directory:
        report_store, _index_store, session = pending_report_fixture(directory)
        context = multiprocessing.get_context("fork")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_concurrent_rejected_reflection,
                args=(directory, session.model_dump(mode="json"), start, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(timeout=10)

        outcomes = sorted(results.get(timeout=2) for _ in processes)
        persisted = report_store.load(session.session_id)

    self.assertEqual(outcomes, ["deferred", "rejected"])
    self.assertTrue(all(process.exitcode == 0 for process in processes))
    self.assertEqual(persisted.revisions[0].reflection_attempt_count, 1)
```

- [ ] **Step 2: Run the API test and verify the first RED**

Run:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_report_learning.HermesReportLearningTests.test_rejected_reflection_consumes_only_one_attempt_per_utc_date -v
```

Expected: FAIL at the signature assertion because `attempt_date` and `ReportReflectionRetryDeferred` do not exist.

- [ ] **Step 3: Add only the API surface, then verify behavioral RED**

Change the import to `from datetime import date, timedelta`, add `ReportReflectionRetryDeferred` after `ReportReflectionRejected`, and add the keyword-only `attempt_date: date | None = None` parameter to `submit_report_reflection`. Do not add the date check or alter rejection persistence yet.

Run:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_report_learning.HermesReportLearningTests.test_rejected_reflection_consumes_only_one_attempt_per_utc_date tests.test_hermes_report_learning.HermesReportLearningTests.test_rejected_reflection_quarantines_after_three_utc_dates tests.test_hermes_report_learning.HermesReportLearningTests.test_valid_reflection_succeeds_on_later_utc_date tests.test_hermes_report_learning.HermesReportLearningTests.test_concurrent_rejections_consume_one_utc_date_attempt -v
```

Expected: tests now execute but FAIL because same-day retries still consume multiple attempts, no deferred exception is raised, and the attempt date is not persisted.

- [ ] **Step 4: Implement the minimal locked gate**

Change the datetime import in `hermes_report_learning.py` to:

```python
from datetime import date, datetime, timedelta, timezone
```

Change `_save_reflection_rejection` to accept `attempt_date: date` and include this field in the copied snapshot:

```python
"last_reflection_attempt_date": attempt_date,
```

At function entry resolve the server-controlled date:

```python
selected_attempt_date = (
    datetime.now(timezone.utc).date()
    if attempt_date is None
    else attempt_date
)
if type(selected_attempt_date) is not date:
    raise ValueError("invalid report reflection attempt date")
```

Inside the existing locked pending-revision branch, after confirming `reflection_state == "pending"` and before `ReportReflection.model_validate`, add:

```python
if snapshot.last_reflection_attempt_date == selected_attempt_date:
    raise ReportReflectionRetryDeferred(
        "report reflection retry deferred until a later UTC date"
    )
```

Pass `selected_attempt_date` into both `_save_reflection_rejection` calls. Do not change `_validated_reflection` or any unsafe-content pattern.

- [ ] **Step 5: Run sequential and concurrent tests and verify GREEN**

Run the four-test command from Step 3, then:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_report_learning -v
```

Expected: all report-learning tests PASS.

- [ ] **Step 6: Commit the core gate**

```bash
git add tradingagents/integrations/hermes_report_learning.py tests/test_hermes_report_learning.py
git commit -m "fix: gate report reflection retries by UTC date"
```

### Task 3: Align the MCP Error Contract

**Files:**
- Modify: `tests/test_hermes_mcp.py`
- Modify: `tradingagents/integrations/hermes_mcp.py`

- [ ] **Step 1: Write failing MCP rejection and deferred tests**

Import `inspect`. Rename `test_attention_required_reflection_rejects_malformed_retry_without_write` to `test_same_day_reflection_retry_is_deferred_without_write`, begin it with the following clean API assertion, and replace its immediate three-attempt unsafe-reflection loop with a test that calls `submit_report_reflection_impl` twice on the same date:

```python
self.assertIn(
    "attempt_date",
    inspect.signature(submit_report_reflection_impl).parameters,
)
```

```python
first = submit_report_reflection_impl(
    {
        "session_id": session.session_id,
        "expected_revision": 1,
        "reflection": unsafe_reflection,
    },
    session_store=session_store,
    report_store=report_store,
    learning_store=learning_store,
    attempt_date=date(2026, 8, 11),
)
same_day = submit_report_reflection_impl(
    {
        "session_id": session.session_id,
        "expected_revision": 1,
        "reflection": unsafe_reflection,
    },
    session_store=session_store,
    report_store=report_store,
    learning_store=learning_store,
    attempt_date=date(2026, 8, 11),
)
snapshot = report_store.load(session.session_id).revisions[0]

self.assertEqual(first["error"]["code"], "REFLECTION_UNSAFE_CONTENT")
self.assertIn("Do not submit", first["error"]["suggested_action"])
self.assertEqual(
    same_day["error"]["code"],
    "REPORT_REFLECTION_RETRY_DEFERRED",
)
self.assertIn("current Agent run", same_day["error"]["suggested_action"])
self.assertEqual(snapshot.reflection_attempt_count, 1)
```

Continue the same fixture on the next UTC date with a malformed reflection to prove schema rejection also uses the gate:

```python
malformed_reflection = self.valid_reflection_payload()
malformed_reflection.pop("decision_thesis")
schema_rejected = submit_report_reflection_impl(
    {
        "session_id": session.session_id,
        "expected_revision": 1,
        "reflection": malformed_reflection,
    },
    session_store=session_store,
    report_store=report_store,
    learning_store=learning_store,
    attempt_date=date(2026, 8, 12),
)
record_path = report_store.path_for(session.session_id)
schema_rejected_bytes = record_path.read_bytes()
schema_deferred = submit_report_reflection_impl(
    {
        "session_id": session.session_id,
        "expected_revision": 1,
        "reflection": malformed_reflection,
    },
    session_store=session_store,
    report_store=report_store,
    learning_store=learning_store,
    attempt_date=date(2026, 8, 12),
)
snapshot = report_store.load(session.session_id).revisions[0]

self.assertEqual(schema_rejected["error"]["code"], "INVALID_REPORT_REFLECTION")
self.assertEqual(snapshot.last_error_code, "REFLECTION_SCHEMA_INVALID")
self.assertEqual(snapshot.reflection_attempt_count, 2)
self.assertEqual(
    schema_deferred["error"]["code"],
    "REPORT_REFLECTION_RETRY_DEFERRED",
)
self.assertEqual(record_path.read_bytes(), schema_rejected_bytes)
```

- [ ] **Step 2: Run focused MCP tests and verify RED**

Run:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_mcp.HermesMCPTests.test_same_day_reflection_retry_is_deferred_without_write -v
```

Expected: FAIL because `attempt_date` and deferred MCP mapping are absent.

- [ ] **Step 3: Implement the MCP contract**

Import `ReportReflectionRetryDeferred` from `hermes_report_learning`. Add one module constant:

```python
_REPORT_REFLECTION_NO_CURRENT_RUN_RETRY = (
    "Do not submit this session and revision again in the current Agent run. "
    "Leave the item pending for a later scheduled run and continue independent items."
)
```

Change the internal implementation signature to accept keyword-only test injection:

```python
def submit_report_reflection_impl(
    request_data: Mapping[str, Any],
    session_store: SessionStore | None = None,
    report_store: ReportLearningStore | None = None,
    learning_store: LearningStore | None = None,
    *,
    attempt_date: date | None = None,
) -> dict[str, Any]:
```

Remove the early `ReportReflection.model_validate` block that special-cases `extra_forbidden`. Keep envelope checks for exact top-level keys, valid identity, revision, and mapping reflection. Pass `attempt_date=attempt_date` to `_persist_report_reflection`.

Catch deferred before the general rejected exception:

```python
except ReportReflectionRetryDeferred:
    return _report_error(
        "REPORT_REFLECTION_RETRY_DEFERRED",
        "The report reflection already consumed its UTC-date attempt.",
        _REPORT_REFLECTION_NO_CURRENT_RUN_RETRY,
    )
```

For every `ReportReflectionRejected`, retain the existing public code mapping but replace the suggested action with `_REPORT_REFLECTION_NO_CURRENT_RUN_RETRY`.

- [ ] **Step 4: Run MCP tests and verify GREEN**

Run:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_mcp -v
```

Expected: all MCP tests PASS, including tool-schema assertions that no attempt date is exposed.

- [ ] **Step 5: Commit the MCP contract**

```bash
git add tradingagents/integrations/hermes_mcp.py tests/test_hermes_mcp.py
git commit -m "fix: defer same-day reflection retries in MCP"
```

### Task 4: Align the Hermes Agent Skill

**Files:**
- Modify: `tests/test_hermes_review_verifier.py`
- Modify: `deploy/hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md`

- [ ] **Step 1: Write the failing static Skill contract test**

Add this method to `HermesReviewVerifierTests`:

```python
def test_scheduled_skill_defers_rejected_reflection_retries(self):
    skill = SCHEDULED_REVIEW_SKILL_PATH.read_text(encoding="ascii")
    reflection = skill[
        skill.index("## 2. Reflect bounded report evidence (v2)") :
        skill.index("## 3. Promote one report memory entry at a time")
    ]

    self.assertIn("one evidence fetch and one submit", reflection)
    self.assertIn("Do not fetch, regenerate, or submit", reflection)
    self.assertIn("same `session_id` and `revision`", reflection)
    self.assertIn("current Agent run", reflection)
    self.assertIn("may have contributed", reflection)
    self.assertIn("is consistent with", reflection)
    self.assertIn("could indicate", reflection)
    self.assertIn("certainty", reflection)
    self.assertIn("real-order", reflection)
    self.assertIn("credential", reflection)
    self.assertIn("prompt-injection", reflection)
    self.assertIn("unsupported external-source", reflection)
    self.assertIn("marker", reflection)
    self.assertIn("delimiter", reflection)
```

- [ ] **Step 2: Run the static test and verify RED**

Run:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_review_verifier.HermesReviewVerifierTests.test_scheduled_skill_defers_rejected_reflection_retries -v
```

Expected: FAIL because the explicit calibrated-language and no-regeneration contract is missing.

- [ ] **Step 3: Add the bounded reflection instructions**

In Skill section 2, before the MCP call, add ASCII-only instructions with these exact requirements:

```text
Treat each listed session_id/revision pair as one evidence fetch and one submit
for the current Agent run. Use calibrated causal wording such as "may have
contributed", "is consistent with", or "could indicate". Never use certainty,
real-order, credential, prompt-injection, unsupported external-source, Hermes
marker, or entry delimiter content in any reflection field.
```

After the submit response validation, add:

```text
For any response whose ok is not exactly true, do not fetch, regenerate, or
submit the same `session_id` and `revision` again in the current Agent run, even if
the response suggests retrying. Report only the safe error and continue with
independent items.
```

Keep the existing evidence boundary and do not add reflection rewriting or sanitization.

- [ ] **Step 4: Run Skill/runbook tests and verify GREEN**

Run:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_review_verifier -v
```

Expected: all verifier and static contract tests PASS.

- [ ] **Step 5: Commit the Skill contract**

```bash
git add deploy/hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md tests/test_hermes_review_verifier.py
git commit -m "fix: constrain scheduled reflection retries"
```

### Task 5: Document Deferred Recovery and New-Date Acceptance

**Files:**
- Modify: `tests/test_hermes_review_verifier.py`
- Modify: `docs/hermes_integration.md`

- [ ] **Step 1: Write the failing runbook test**

Add this method to `HermesReviewVerifierTests`:

```python
def test_runbook_documents_reflection_retry_gate_recovery(self):
    text = RUNBOOK_PATH.read_text(encoding="utf-8")

    self.assertIn("REPORT_REFLECTION_RETRY_DEFERRED", text)
    self.assertIn("同一 UTC 日期最多消耗一次", text)
    self.assertIn("三个不同 UTC 日期", text)
    self.assertIn("保持 `attention_required` artifact 不变", text)
    self.assertIn("新的未使用历史日期", text)
    self.assertIn("不得直接修改 `report_memories/<session_id>.json`", text)
    self.assertIn("不得重新运行同一 item", text)
```

- [ ] **Step 2: Run the runbook test and verify RED**

Run:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_review_verifier.HermesReviewVerifierTests.test_runbook_documents_reflection_retry_gate_recovery -v
```

Expected: FAIL because the retry gate and new-date recovery are not documented.

- [ ] **Step 3: Add the runbook recovery section**

In `docs/hermes_integration.md`, after the v2 Agent failure handling and before production job activation, document:

```text
REPORT_REFLECTION_RETRY_DEFERRED 表示同一 session/revision 已在当前 UTC
日期消耗一次 bounded reflection attempt。同一 UTC 日期最多消耗一次；当前
Agent run 不得重新运行同一 item。第一次和第二次 rejected attempt 保持 pending，
只能由后续 UTC 日期的 scheduled run 各尝试一次；只有三个不同 UTC 日期均失败才进入
attention_required。

若验收项已进入 attention_required，保持 `attention_required` artifact 不变，
不得直接修改 `report_memories/<session_id>.json`、不得补写 Hermes memory、不得删除
report/review/index/schedule。修复并重新部署后选择新的未使用历史日期，从 v2 submit
开始重新验收；原失败 artifact 永久保留供审计。
```

Keep all four Cron jobs paused during this recovery and retain the existing rule that no later acceptance stage runs after a failed stage.

- [ ] **Step 4: Run documentation tests and verify GREEN**

Run:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_review_verifier -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit the runbook**

```bash
git add docs/hermes_integration.md tests/test_hermes_review_verifier.py
git commit -m "docs: add reflection retry gate recovery"
```

### Task 6: Verify the Complete Change

**Files:**
- Verify all files changed in Tasks 1-5.

- [ ] **Step 1: Run focused retry-gate modules**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_schemas tests.test_hermes_report_learning tests.test_hermes_mcp tests.test_hermes_review_verifier -v
```

Expected: all focused tests PASS with zero failures and zero errors.

- [ ] **Step 2: Run the Hermes integration suite**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest discover -s tests -p 'test_hermes*.py' -v
```

Expected: all Hermes tests PASS.

- [ ] **Step 3: Run the full repository suite**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest discover -s tests -v
```

Expected: all repository tests PASS.

- [ ] **Step 4: Run compile and whitespace checks**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m compileall -q tradingagents tests
git diff --check
```

Expected: both commands exit `0` with no error output.

- [ ] **Step 5: Review scope and history**

```bash
git status --short
git diff main...HEAD --stat
git log --oneline main..HEAD
```

Expected: only the approved schema, core, MCP, Skill, runbook, test, spec, and plan files changed; no server artifacts, memory files, credentials, or unrelated files appear.

- [ ] **Step 6: Record deployment acceptance commands**

Prepare the final handoff with exact commands to:

```text
1. pull the merged commit on the server;
2. reinstall the updated owner-only Skill;
3. keep all four Cron jobs paused;
4. select a new unused historical trade date at least 16 UTC days old;
5. repeat v2 submit/archive and staged T+1/T+7/T+15 acceptance;
6. verify the original attention_required artifact remains unchanged;
7. resume production jobs only after all acceptance and retention checks pass.
```

This step writes no server data and does not execute deployment from the local development workspace.
