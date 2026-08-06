# Hermes Report-Level Learning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aggregate each new paper report's T+1, T+7, and T+15 reviews into one evidence-bounded learning record that feeds later decisions and one progressively replaced Hermes memory entry.

**Architecture:** Keep `PaperDecisionReview` as immutable facts, add a locked report-learning store with at most three revision snapshots per session, and atomically upgrade each symbol index to a mixed v2 format that preserves legacy lessons. The 08:15 processor creates facts without an Agent; the 08:30 Hermes Agent submits strict structured reflections, then uses only the built-in memory tool for add/replace and a project read-only verifier for confirmation.

**Tech Stack:** Python 3.10+, Pydantic v2, FastMCP, filesystem JSON with `fcntl` locks and atomic replacement, `unittest`, Hermes Agent skills and Cron.

---

## Working Rules

Run every command from:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.worktrees/hermes-report-level-learning
```

Use this interpreter for every test command:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python
```

Do not stage or edit files outside this worktree. Read the approved design before
starting:

```bash
sed -n '1,480p' docs/superpowers/specs/2026-08-05-hermes-report-level-learning-design.md
```

## File Map

- `tradingagents/integrations/schemas.py`: shared strict Pydantic contracts and
  v1/v2 index compatibility.
- `tradingagents/integrations/hermes_learning.py`: legacy review index writes,
  atomic v2 index upgrade, report index writes, and balanced lesson selection.
- `tradingagents/integrations/hermes_report_learning.py`: report store, outcome
  aggregation, evidence packets, reflection validation, and deterministic
  rendering.
- `tradingagents/integrations/hermes_report_memory.py`: bounded reflection and
  memory queues plus revision state transitions.
- `tradingagents/integrations/hermes_report_memory_verifier.py`: read-only exact
  report-memory verification.
- `tradingagents/integrations/hermes_scheduled_reviews.py`: v1/v2 schedule
  dispatch after deterministic review creation.
- `tradingagents/integrations/hermes_scheduled_review_runner.py`: safe CLI modes
  used by the scheduled skill.
- `tradingagents/integrations/hermes_mcp.py`: v2 enrollment, strict reflection
  submission tool, and graph lesson loading.
- `tradingagents/agents/**`: prompt wording that treats report lessons as
  context whose applicability must be checked.
- `deploy/hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md`: bounded
  v1 drain, v2 reflection, and Hermes memory add/replace workflow.
- `docs/hermes_integration.md`: deployment, validation, recovery, and rollback.
- `tests/test_hermes_report_learning.py`: report aggregation, evidence,
  validation, rendering, and index selection.
- `tests/test_hermes_report_memory.py`: queues, transitions, ordering, and
  verifier behavior.
- Existing Hermes test modules: schema, MCP, schedule, runner, prompt, skill,
  runbook, and regression coverage.

### Task 1: Add Versioned Shared Contracts

**Files:**
- Modify: `tradingagents/integrations/schemas.py:189-426`
- Modify: `tests/test_hermes_schemas.py:101-160`

- [ ] **Step 1: Write failing schema tests**

Add imports for the new models and tests that establish archive version 2,
bounded revision snapshots, strict reflection fields, and mixed index rules:

```python
def test_report_learning_models_are_strict_and_revision_bounded(self):
    outcome = ReportLearningOutcome(
        review_id="review_0123456789abcdef",
        horizon_days=1,
        review_date="2026-08-06",
        raw_return_pct=1.25,
        verdict="correct",
    )
    reflection = ReportReflection(
        decision_thesis="Momentum supported the BUY decision.",
        technical_context="Price structure was constructive.",
        sentiment_context=None,
        news_context="Archived news evidence was mixed.",
        fundamental_context=None,
        overall_assessment="The first-day direction matched the proposal.",
        outcome_assessments=[ReportOutcomeAssessment(
            horizon_days=1,
            assessment="T+1 matched the proposed BUY direction.",
        )],
        reasoning_strengths=["The plan stated a directional thesis."],
        causal_hypotheses=[ReportCausalHypothesis(
            statement="Momentum may have supported the first-day move.",
            evidence=["report.market", "outcome.t1"],
            confidence="medium",
        )],
        mistakes_or_missed_opportunities=[],
        next_decision_checks=["Check whether momentum persists across horizons."],
    )
    revision = ReportLearningRevision(
        revision=1,
        outcome_review_ids=[outcome.review_id],
        reflection_state="ready",
        memory_state="add_pending",
        source_fields=[ReportSourceMetadata(
            name="report.market",
            sha256="b" * 64,
            truncated=False,
        )],
        reflection=reflection,
        lesson="BTC report lesson.",
        hermes_memory_entry="[TradingAgents paper report: hermes_0123456789abcdef]\nLesson.",
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    record = ReportLearningRecord(
        session_id="hermes_0123456789abcdef",
        symbol=" btc ",
        trade_date="2026-08-05",
        action="BUY",
        source_digest="a" * 64,
        desired_revision=1,
        reflected_revision=1,
        confirmed_revision=0,
        outcomes=[outcome],
        revisions=[revision],
        created_at=utc_now(),
        updated_at=utc_now(),
    )
    self.assertEqual(record.symbol, "BTC")
    self.assertEqual(record.revisions[0].memory_state, "add_pending")
    with self.assertRaises(ValidationError):
        ReportLearningRecord.model_validate(
            {**record.model_dump(), "revisions": [revision.model_dump()] * 4}
        )
    with self.assertRaises(ValidationError):
        ReportReflection.model_validate(
            {**reflection.model_dump(), "unexpected": "rejected"}
        )

def test_symbol_learning_index_v2_separates_reports_and_legacy_entries(self):
    legacy = SymbolLearningEntry(
        review_id="review_0123456789abcdef",
        session_id="hermes_0123456789abcdef",
        review_date="2026-08-06",
        lesson="Legacy lesson.",
    )
    report = ReportLearningIndexEntry(
        session_id="hermes_fedcba9876543210",
        trade_date="2026-08-05",
        maturity_days=15,
        reflected_revision=3,
        updated_at=utc_now(),
        lesson="Mature report lesson.",
    )
    index = SymbolLearningIndex(
        schema_version=2,
        symbol="BTC",
        updated_at=utc_now(),
        report_entries=[report],
        legacy_entries=[legacy],
    )
    self.assertEqual(index.entries, [])
    with self.assertRaises(ValidationError):
        SymbolLearningIndex.model_validate(
            {**index.model_dump(), "entries": [legacy.model_dump()]}
        )
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_schemas.HermesSchemaTests -v
```

Expected: import errors for `ReportLearningOutcome`, `ReportReflection`,
`ReportLearningRevision`, `ReportLearningRecord`, and
`ReportLearningIndexEntry`.

- [ ] **Step 3: Implement the strict contracts**

Add the following model family to `schemas.py`, preserving `_StrictModel` and
the existing identity validators:

```python
class ReportSourceMetadata(_StrictModel):
    name: str = Field(min_length=1, max_length=100)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    truncated: bool

class ReportLearningOutcome(_StrictModel):
    review_id: str
    horizon_days: Literal[1, 7, 15]
    review_date: date
    raw_return_pct: float
    verdict: Literal["correct", "incorrect", "flat", "not_scored"]

class ReportCausalHypothesis(_StrictModel):
    statement: str = Field(min_length=1, max_length=400)
    evidence: list[str] = Field(min_length=1, max_length=4)
    confidence: Literal["low", "medium", "high"]

class ReportOutcomeAssessment(_StrictModel):
    horizon_days: Literal[1, 7, 15]
    assessment: str = Field(min_length=1, max_length=400)

class ReportReflection(_StrictModel):
    decision_thesis: str = Field(min_length=1, max_length=600)
    technical_context: str | None = Field(default=None, max_length=600)
    sentiment_context: str | None = Field(default=None, max_length=600)
    news_context: str | None = Field(default=None, max_length=600)
    fundamental_context: str | None = Field(default=None, max_length=600)
    overall_assessment: str = Field(min_length=1, max_length=800)
    outcome_assessments: list[ReportOutcomeAssessment] = Field(min_length=1, max_length=3)
    reasoning_strengths: list[str] = Field(max_length=3)
    causal_hypotheses: list[ReportCausalHypothesis] = Field(min_length=1, max_length=3)
    mistakes_or_missed_opportunities: list[str] = Field(max_length=3)
    next_decision_checks: list[str] = Field(min_length=1, max_length=5)

class ReportLearningRevision(_StrictModel):
    revision: int = Field(ge=1, le=3)
    outcome_review_ids: list[str] = Field(min_length=1, max_length=3)
    reflection_state: Literal["pending", "ready", "attention_required"]
    memory_state: Literal[
        "blocked", "add_pending", "replace_pending", "memory_call_started",
        "verification_pending", "confirmed", "attention_required",
    ]
    source_fields: list[ReportSourceMetadata] = Field(min_length=1, max_length=8)
    reflection_attempt_count: int = Field(default=0, ge=0)
    last_error_code: str | None = Field(default=None, max_length=100)
    reflection: ReportReflection | None = None
    lesson: str | None = Field(default=None, max_length=6000)
    hermes_memory_entry: str | None = Field(default=None, max_length=6000)
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None = None

class ReportLearningRecord(_StrictModel):
    schema_version: Literal[1] = 1
    session_id: str
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{2,20}$")
    trade_date: date
    action: Literal["BUY", "SELL", "HOLD", "UNPARSEABLE"]
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    desired_revision: int = Field(ge=1, le=3)
    reflected_revision: int = Field(default=0, ge=0, le=3)
    confirmed_revision: int = Field(default=0, ge=0, le=3)
    outcomes: list[ReportLearningOutcome] = Field(min_length=1, max_length=3)
    revisions: list[ReportLearningRevision] = Field(min_length=1, max_length=3)
    created_at: datetime
    updated_at: datetime

class ReportLearningIndexEntry(_StrictModel):
    session_id: str
    trade_date: date
    maturity_days: Literal[1, 7, 15]
    reflected_revision: int = Field(ge=1, le=3)
    updated_at: datetime
    lesson: str = Field(min_length=1, max_length=6000)
```

Add validators for finite returns, opaque review/session IDs, normalized
symbols, 400-character bounds for every string inside reflection lists, unique
source names, unique outcome-assessment horizons, unique ordered
horizons/revisions, and
`confirmed_revision <= reflected_revision <= desired_revision`. Extend
`DailyReportArchive.scheduled_review_version` to `Literal[1, 2] | None`, add
`workflow_version: Literal[1, 2] = 1` to `ScheduledReviewPlan`, and add optional
`session_id` to `SymbolLearningEntry` for one-per-session legacy fallback.

Upgrade `SymbolLearningIndex` with `schema_version: Literal[1, 2]`, legacy
`entries`, v2 `report_entries`, and v2 `legacy_entries`. Its model validator must
require only `entries` for version 1 and require `entries == []` for version 2.

- [ ] **Step 4: Run schema tests**

Run the Task 1 command again. Expected: all `HermesSchemaTests` pass.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/integrations/schemas.py tests/test_hermes_schemas.py
git commit -m "feat: add report-level learning schemas"
```

### Task 2: Upgrade The Symbol Learning Index Atomically

**Files:**
- Modify: `tradingagents/integrations/hermes_learning.py:106-168`
- Modify: `tradingagents/integrations/hermes_review_verifier.py:32-70`
- Modify: `tests/test_hermes_learning.py:30-180`
- Modify: `tests/test_hermes_review_verifier.py:91-132`
- Create: `tests/test_hermes_report_learning.py`

- [ ] **Step 1: Write failing index migration and selection tests**

Create `tests/test_hermes_report_learning.py` with shared builders and these
behaviors:

```python
class HermesReportLearningTests(unittest.TestCase):
    def test_first_report_upsert_preserves_every_v1_entry(self):
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            first = paper_review(1, session_id="hermes_0000000000000001")
            second = paper_review(7, session_id="hermes_0000000000000002")
            store.upsert(first)
            store.upsert(second)
            store.upsert_report(reflected_record("hermes_0000000000000003", 15))
            index = store.load("BTC")
        self.assertEqual(index.schema_version, 2)
        self.assertEqual(
            {entry.review_id for entry in index.legacy_entries},
            {first.review_id, second.review_id},
        )
        self.assertEqual(len(index.report_entries), 1)

    def test_v1_upsert_after_upgrade_updates_legacy_collection(self):
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            store.upsert_report(reflected_record("hermes_0000000000000001", 1))
            legacy = paper_review(7, session_id="hermes_0000000000000002")
            store.upsert(legacy)
            index = store.load("BTC")
        self.assertEqual(index.legacy_entries[0].review_id, legacy.review_id)

    def test_lessons_balance_three_recent_and_two_mature_reports(self):
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            records = [
                reflected_record(f"hermes_{number:016x}", maturity)
                for number, maturity in enumerate((15, 15, 1, 1, 1, 1), start=1)
            ]
            for record in records:
                store.upsert_report(record)
            lessons = store.lessons_for("BTC", limit=5)
        self.assertEqual(len(lessons), 5)
        self.assertLessEqual(sum(len(lesson) for lesson in lessons), 12000)
        self.assertTrue(all("report lesson" in lesson for lesson in lessons))
        self.assertIn(records[0].revisions[-1].lesson, lessons)
        self.assertIn(records[1].revisions[-1].lesson, lessons)
```

Also add a test proving v1 fallback adds at most one lesson for each non-null
`session_id` plus at most one legacy entry whose session is unknown, without
modifying the file.

- [ ] **Step 2: Run tests and verify failure**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_learning tests.test_hermes_report_learning -v
```

Expected: failure because `upsert_report` and mixed-schema loading do not exist.

- [ ] **Step 3: Implement migration, writes, and balanced reads**

Use the revised `SymbolLearningIndex.model_validate` to load both versions. Keep
the existing exclusive lock and atomic writer. Add:

```python
REPORT_LESSON_LIMIT = 5
RECENT_REPORT_LIMIT = 3
MATURE_REPORT_LIMIT = 2
GRAPH_LESSON_TOTAL_MAX_CHARS = 12000

def _legacy_entry(review: PaperDecisionReview) -> SymbolLearningEntry:
    return SymbolLearningEntry(
        review_id=review.review_id,
        session_id=review.session_id,
        review_date=review.review_date,
        lesson=review.hermes_memory_entry,
    )
```

Add `LearningStore.upsert_report(record)`. It must select
`record.revisions[record.reflected_revision - 1]`, reject a snapshot whose
reflection is not ready or whose lesson is missing, convert v1 `entries` to
v2 `legacy_entries` under the existing exclusive lock, upsert one
`ReportLearningIndexEntry` by session ID, sort by trade date then session ID
descending, and atomically write schema version 2.

Implement `lessons_for` by selecting three newest report entries, two newest
entries with `maturity_days == 15`, deduplicating session IDs, and filling from
remaining reports. Only then fill from legacy entries, with at most one entry per
legacy session ID and at most one entry with a missing legacy session ID.
Preserve the current five-lesson default. Update
`verify_review_consistency` to inspect `entries` for schema v1 and
`legacy_entries` for schema v2 so an in-flight v1 schedule remains verifiable
after an index upgrade. Stop adding selected lessons when the shared 12000-
character graph budget is exhausted; never split or rewrite a stored lesson.

- [ ] **Step 4: Run learning tests**

Run the Task 2 command again. Expected: all tests pass, including existing
25-entry retention and concurrent-upsert tests.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/integrations/hermes_learning.py tradingagents/integrations/hermes_review_verifier.py tests/test_hermes_learning.py tests/test_hermes_report_learning.py tests/test_hermes_review_verifier.py
git commit -m "feat: add versioned report learning index"
```

### Task 3: Persist Report Facts And Revision Snapshots

**Files:**
- Create: `tradingagents/integrations/hermes_report_learning.py`
- Modify: `tests/test_hermes_report_learning.py`

- [ ] **Step 1: Add failing report-store tests**

Add tests for T+1/T+7/T+15 aggregation, idempotence, source mismatch, and stale
concurrency:

```python
def test_report_store_progressively_aggregates_three_reviews(self):
    with TemporaryDirectory() as directory:
        store = ReportLearningStore(Path(directory))
        session = completed_session()
        records = [
            record_review_fact(store, session, paper_review(horizon))
            for horizon in (1, 7, 15)
        ]
        repeated = record_review_fact(store, session, paper_review(15))
    self.assertEqual([record.desired_revision for record in records], [1, 2, 3])
    self.assertEqual([item.horizon_days for item in records[-1].outcomes], [1, 7, 15])
    self.assertEqual(len(records[-1].revisions), 3)
    self.assertEqual(repeated.model_dump(), records[-1].model_dump())

def test_report_store_rejects_review_identity_or_source_change(self):
    with TemporaryDirectory() as directory:
        store = ReportLearningStore(Path(directory))
        session = completed_session()
        record_review_fact(store, session, paper_review(1))
        with self.assertRaises(ReportLearningConflict):
            record_review_fact(
                store,
                session.model_copy(update={"result": changed_result()}),
                paper_review(7),
            )
```

Add a multiprocessing test analogous to
`test_concurrent_upserts_retain_each_review`, asserting that two processes
adding T+1 and T+7 cannot lose an outcome and produce monotonically ordered
revisions.

- [ ] **Step 2: Run tests and verify failure**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_report_learning.HermesReportLearningTests -v
```

Expected: import failure for `ReportLearningStore` and `record_review_fact`.

- [ ] **Step 3: Implement the locked report store and fact recorder**

Create the module with these public interfaces:

```python
REPORT_MEMORY_MARKER = "[TradingAgents paper report: {session_id}]"
MAX_REPORT_REVISIONS = 3

class ReportLearningError(RuntimeError):
    pass

class ReportLearningConflict(ReportLearningError):
    pass
```

`ReportLearningStore` must expose `from_environment`, `path_for`, `load`,
`save`, `records`, and locked `update` methods. Add
`record_review_fact(store, session, review) -> ReportLearningRecord` as the only
public fact-aggregation function.

Use ASCII JSON with `ensure_ascii=True`, fsync, `os.replace`, and one lock file
under `report_memories`. Validate session completion, exact session/symbol/trade
date/action identity, allowed horizon, matching review date, and immutable source
digest. Sort outcomes by horizon and reject a fourth distinct outcome. A repeated
review ID returns the existing record without adding a revision. Populate
the eight source-field digests deterministically. Each revision owns its own
`source_fields`; Task 4 will calculate packet-specific truncation flags while
retaining the same digests.

Each new fact appends a `ReportLearningRevision` with the complete current set of
outcome review IDs, `reflection_state="pending"`, and
`memory_state="blocked"`. Do not call the Agent, index, or Hermes memory here.

- [ ] **Step 4: Run the report-learning tests**

Run the Task 3 command again. Expected: all report-store tests pass.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/integrations/hermes_report_learning.py tests/test_hermes_report_learning.py
git commit -m "feat: aggregate paper reviews by report"
```

### Task 4: Build Bounded Evidence And Deterministic Reflections

**Files:**
- Modify: `tradingagents/integrations/schemas.py`
- Modify: `tradingagents/integrations/hermes_report_learning.py`
- Modify: `tests/test_hermes_report_learning.py`

- [ ] **Step 1: Write failing evidence and reflection tests**

Add tests that use long archived fields, dynamic evidence validation, stale
revision rejection, verdict coverage, deterministic rendering, and unsafe text:

```python
def test_evidence_packet_is_deterministic_bounded_and_marks_truncation(self):
    session = completed_session(market_report="start " + "x" * 9000 + " conclusion")
    record = record_with_pending_revision(session)
    first = build_evidence_packet(record, session, revision=1)
    second = build_evidence_packet(record, session, revision=1)
    encoded = json.dumps(first.model_dump(mode="json"), ensure_ascii=True).encode("utf-8")
    self.assertEqual(first, second)
    self.assertLessEqual(len(encoded), EVIDENCE_PACKET_MAX_BYTES)
    market = next(field for field in first.fields if field.name == "report.market")
    self.assertTrue(market.truncated)
    self.assertEqual(len(market.sha256), 64)
    self.assertIn("start", market.excerpt)
    self.assertIn("conclusion", market.excerpt)

def test_submit_reflection_rejects_unknown_evidence_and_stale_revision(self):
    with TemporaryDirectory() as directory:
        report_store, index_store, session = pending_report_fixture(directory)
        payload = valid_reflection_payload()
        payload["causal_hypotheses"][0]["evidence"] = ["external.news"]
        with self.assertRaises(ReportReflectionRejected):
            submit_report_reflection(report_store, index_store, session, 1, payload)
        with self.assertRaises(ReportLearningConflict):
            submit_report_reflection(
                report_store, index_store, session, 0, valid_reflection_payload()
            )

def test_renderer_is_stable_and_contains_all_outcomes_and_disclaimers(self):
    first = render_report_lesson(ready_record(), revision=3)
    second = render_report_lesson(ready_record(), revision=3)
    self.assertEqual(first, second)
    self.assertLessEqual(len(first.lesson), REPORT_LESSON_MAX_CHARS)
    self.assertIn("T+1", first.lesson)
    self.assertIn("T+7", first.lesson)
    self.assertIn("T+15", first.lesson)
    self.assertIn("hypotheses", first.lesson.lower())
    self.assertTrue(first.hermes_memory_entry.startswith("[TradingAgents paper report:"))
    self.assertIn("paper trading", first.hermes_memory_entry.lower())
```

Include separate cases for incorrect, correct, flat, and not-scored outcomes.
Reject reflection content matching explicit real-order instructions or certainty
terms such as `guaranteed`, `proved`, or `caused`.

- [ ] **Step 2: Run tests and verify failure**

Run the Task 3 test command. Expected: missing evidence and reflection functions.

- [ ] **Step 3: Add evidence schemas and constants**

Add strict `ReportEvidenceField` and `ReportEvidencePacket` models. Define these
source-controlled limits in `hermes_report_learning.py`:

```python
EVIDENCE_PACKET_MAX_BYTES = 4096
REPORT_LESSON_MAX_CHARS = 2400
HERMES_REPORT_MEMORY_MAX_CHARS = 4000
MAX_REFLECTION_ATTEMPTS = 3
EVIDENCE_FIELD_ORDER = (
    "report.market", "report.sentiment", "report.news", "report.fundamentals",
    "investment_plan", "trader_plan", "final_decision", "processed_signal",
)
```

Implement deterministic head-and-tail excerpting and shrink lower-priority
fields until the serialized packet fits 4096 UTF-8 bytes. Outcome identifiers are
`outcome.t1`, `outcome.t7`, and `outcome.t15` and are always included.
Refactor `record_review_fact` to populate each new revision's `source_fields`
from the same excerpting helper, so stored digests and truncation flags exactly
match the packet later returned for that revision.

- [ ] **Step 4: Implement strict submission and rendering**

Add `build_evidence_packet(record, session, revision) -> ReportEvidencePacket`,
`submit_report_reflection(report_store, learning_store, session,
expected_revision, reflection_data) -> ReportLearningRecord`, and
`render_report_lesson(record, revision) -> RenderedReportLesson`. Define the
return value as:

```python
@dataclass(frozen=True)
class RenderedReportLesson:
    lesson: str
    hermes_memory_entry: str
```

Parse `reflection_data` with `ReportReflection.model_validate`. Require every
evidence reference to occur in the current packet, require the
`outcome_assessments` horizon set to exactly equal the snapshot outcome horizon
set, and require verdict-appropriate sections. Use a
small denylist only for explicit certainty and real-order phrases; do not attempt
general natural-language causal parsing.

Add `ReportReflectionRejected(ReportLearningError)` for payloads that fail these
domain checks; keep `ReportLearningConflict` for absent, duplicate-different, or
out-of-order revisions.

On validation failure, atomically increment that snapshot's
`reflection_attempt_count` and store an allowlisted error code. The first two
failures remain pending; the third sets `reflection_state="attention_required"`.
The validator must cover English and Chinese imperative real-order phrases. The
scheduled skill asks for concise English fields so renderer output remains
consistent, but non-ASCII archived evidence stays supported.

Under the report-store lock, require `expected_revision` to be the next pending
snapshot after `reflected_revision`, then recheck its source digest and exact
outcome review IDs. Save reflection and
rendered strings into that snapshot, set `reflection_state="ready"`, set
`memory_state` to `add_pending` for revision 1 or `replace_pending` otherwise,
advance `reflected_revision`, and call `LearningStore.upsert_report`. Identical
resubmission is idempotent; different content for an already-ready revision is a
conflict.

- [ ] **Step 5: Run tests**

Run the Task 3 command. Expected: all evidence, reflection, rendering, and index
tests pass.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/integrations/schemas.py tradingagents/integrations/hermes_report_learning.py tests/test_hermes_report_learning.py
git commit -m "feat: validate and render report reflections"
```

### Task 5: Dispatch Version-1 And Version-2 Review Processing

**Files:**
- Modify: `tradingagents/integrations/hermes_learning.py:246-310`
- Modify: `tradingagents/integrations/hermes_scheduled_reviews.py:156-408`
- Modify: `tradingagents/integrations/hermes_scheduled_review_runner.py:34-66`
- Modify: `tradingagents/integrations/hermes_mcp.py:513-546,951-1067`
- Modify: `tests/test_hermes_learning.py`
- Modify: `tests/test_hermes_scheduled_reviews.py`
- Modify: `tests/test_hermes_scheduled_review_runner.py`
- Modify: `tests/test_hermes_mcp.py:385-430`

- [ ] **Step 1: Write failing version-dispatch tests**

Add tests proving new archives are v2, old v1 schedules still reach
`memory_pending`, and v2 review items aggregate facts without a legacy index
entry:

```python
def test_v2_due_review_completes_fact_and_enqueues_report_reflection(self):
    store, plan, session, review = versioned_schedule_fixture(workflow_version=2)
    seen = []
    report = process_due_reviews(
        store,
        date(2026, 8, 7),
        lambda _session, _date, version: successful_review_result(review, version),
        fact_recorder=lambda candidate: seen.append(candidate.review_id),
    )
    item = store.find_item(review.review_id)[1]
    self.assertEqual(seen, [review.review_id])
    self.assertEqual(item.state, "completed")
    self.assertEqual(report.report_fact_count, 1)

def test_v1_due_review_keeps_existing_memory_pending_path(self):
    store, plan, session, review = versioned_schedule_fixture(workflow_version=1)
    process_due_reviews(
        store,
        date(2026, 8, 7),
        lambda _session, _date, _version: successful_review_result(review, 1),
    )
    self.assertEqual(store.find_item(review.review_id)[1].state, "memory_pending")
```

Update the archive MCP test to assert
`plan.workflow_version == 2` and
`batch.archive.scheduled_review_version == 2`. Add a learning test showing
`review_completed_session` with `learning_store=None` writes the immutable review
without a legacy lesson.

- [ ] **Step 2: Run focused tests and verify failure**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_learning tests.test_hermes_scheduled_reviews tests.test_hermes_scheduled_review_runner tests.test_hermes_mcp -v
```

Expected: callback signature and version assertions fail.

- [ ] **Step 3: Make legacy learning optional**

Change `review_completed_session` to accept
`learning_store: LearningStore | None`. Skip both existing-review repair and new
review index upsert when it is `None`. Add a keyword-only
`write_legacy_learning: bool = True` to `review_paper_decision_impl`; pass `None`
for the learning store only when it is false. Manual review behavior remains
unchanged.

- [ ] **Step 4: Add schedule version dispatch**

Set `ScheduledReviewPlan.workflow_version` from the archive marker in
`create_or_load`. Change the reviewer callback to:

```python
Callable[[str, date, int], dict[str, Any]]
```

For v1 success, retain `memory_pending`. For v2 success, call the injected
`fact_recorder(PaperDecisionReview)`, then mark the schedule item `completed`
with `verified_at`. A fact-recording failure remains retryable; identity mismatch
still becomes `attention_required`. Add `report_fact_count` to the safe process
summary.

In the runner, load the session and call `record_review_fact` only for v2. Call
`review_paper_decision_impl` with
`write_legacy_learning=workflow_version == 1`.
Set new archives to `scheduled_review_version=2`; keep repair enrollment for both
versions.

- [ ] **Step 5: Run focused tests**

Run the Task 5 command again. Expected: all four modules pass.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/integrations/hermes_learning.py tradingagents/integrations/hermes_scheduled_reviews.py tradingagents/integrations/hermes_scheduled_review_runner.py tradingagents/integrations/hermes_mcp.py tests/test_hermes_learning.py tests/test_hermes_scheduled_reviews.py tests/test_hermes_scheduled_review_runner.py tests/test_hermes_mcp.py
git commit -m "feat: route new reviews into report learning"
```

### Task 6: Expose Bounded Reflection Work And Strict MCP Submission

**Files:**
- Modify: `tradingagents/integrations/hermes_report_learning.py`
- Modify: `tradingagents/integrations/hermes_scheduled_review_runner.py:69-205`
- Modify: `tradingagents/integrations/hermes_mcp.py:1069-1275`
- Modify: `tests/test_hermes_scheduled_review_runner.py`
- Modify: `tests/test_hermes_mcp.py`

- [ ] **Step 1: Write failing queue and MCP tests**

Add runner tests for safe metadata listing, explicit bounded evidence retrieval,
limit 19 rejection, and redacted failures. Add an MCP strict-schema test:

```python
def test_submit_report_reflection_tool_rejects_unknown_fields(self):
    tool = MCP._tool_manager.get_tool("submit_report_reflection")
    self.assertIs(tool.parameters["additionalProperties"], False)
    _, result = asyncio.run(MCP.call_tool(
        "submit_report_reflection",
        {
            "session_id": "hermes_0123456789abcdef",
            "expected_revision": 1,
            "reflection": valid_reflection_payload(),
            "unexpected": True,
        },
    ))
    self.assertFalse(result["ok"])
    self.assertEqual(result["error"]["code"], "INVALID_REPORT_REFLECTION")

def test_reflection_pending_output_contains_no_raw_evidence(self):
    code, payload = runner.run_report_reflection_pending(
        18, lister=lambda _limit: [pending_reflection_work()]
    )
    self.assertEqual(code, 0)
    self.assertEqual(
        set(payload["items"][0]),
        {"session_id", "symbol", "trade_date", "revision", "maturity_days"},
    )
```

- [ ] **Step 2: Run focused tests and verify failure**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_scheduled_review_runner tests.test_hermes_mcp -v
```

Expected: missing runner modes and MCP tool.

- [ ] **Step 3: Implement reflection queue commands**

Add `MAX_REPORT_ITEMS = 18` and these runner modes:

```text
report-reflection-pending --limit 18
report-reflection-evidence --session-id <id> --revision <1|2|3>
```

The pending command returns all pending snapshots in report/revision order but
only includes session, symbol, trade date, revision, and maturity. This allows a
missed T+1 snapshot to be submitted before an already-created T+7 snapshot. The
evidence command loads the exact completed session and calls
`build_evidence_packet`; it returns only that one packet. Reject invalid IDs,
revisions, and limits before store access. Keep safe JSON envelopes and do not
include paths or raw exceptions.

- [ ] **Step 4: Implement strict reflection MCP submission**

Add `submit_report_reflection_impl(request_data, session_store=None,
report_store=None, learning_store=None) -> dict[str, Any]` and register
`submit_report_reflection(session_id, expected_revision, reflection)` with
`@MCP.tool()`.

Create a strict `_SubmitReportReflectionArguments` wrapper like the existing
review wrapper and force `additionalProperties=False`. Return only session ID,
revision, reflection state, memory state, and the paper-trading disclaimer. Map
validation, stale revision, missing session, and storage failures to distinct
safe error codes; never return the lesson or evidence in final tool status.

- [ ] **Step 5: Run runner and MCP tests**

Run the Task 6 command again. Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add tradingagents/integrations/hermes_report_learning.py tradingagents/integrations/hermes_scheduled_review_runner.py tradingagents/integrations/hermes_mcp.py tests/test_hermes_scheduled_review_runner.py tests/test_hermes_mcp.py
git commit -m "feat: add scheduled report reflection API"
```

### Task 7: Add Ordered Hermes Report-Memory Promotion

**Files:**
- Create: `tradingagents/integrations/hermes_report_memory.py`
- Create: `tradingagents/integrations/hermes_report_memory_verifier.py`
- Create: `tests/test_hermes_report_memory.py`
- Modify: `tradingagents/integrations/hermes_scheduled_review_runner.py`

- [ ] **Step 1: Write failing promotion and verifier tests**

Create tests for add/replace ordering, crash recovery, exact-one marker,
index consistency, and quarantine:

```python
def test_memory_queue_exposes_only_earliest_unconfirmed_revision(self):
    store = report_store_with_ready_revisions(1, 2)
    work = list_pending_report_memory(store, limit=18)
    self.assertEqual([(item.revision, item.action) for item in work], [(1, "add")])

def test_confirmed_t1_unlocks_t7_replace(self):
    store = report_store_with_ready_revisions(1, 2)
    started = begin_report_memory(store, SESSION_ID, 1)
    self.assertEqual(started.action, "add")
    confirm_report_memory(store, SESSION_ID, 1, verifier=lambda *_args: True)
    replacement = begin_report_memory(store, SESSION_ID, 2)
    self.assertEqual(replacement.action, "replace")
    self.assertEqual(replacement.old_text, f"[TradingAgents paper report: {SESSION_ID}]")

def test_report_verifier_requires_one_marker_and_exact_revision_content(self):
    with TemporaryDirectory() as directory:
        results, memory_path, record = persisted_ready_report(directory)
        memory_path.write_text(record.revisions[0].hermes_memory_entry, encoding="utf-8")
        result = verify_report_memory_consistency(
            SESSION_ID, 1, results, memory_path
        )
    self.assertEqual(result.marker_occurrences, 1)
    self.assertTrue(result.index_matches_latest_reflection)
```

Add failures for zero/two markers, wrong exact content, missing index record,
stale confirmation, memory result ambiguity, and a restarted
`memory_call_started` revision returning the same operation idempotently.

- [ ] **Step 2: Run tests and verify failure**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_report_memory -v
```

Expected: modules do not exist.

- [ ] **Step 3: Implement the promotion state machine**

Create:

```python
@dataclass(frozen=True)
class ReportMemoryWork:
    session_id: str
    symbol: str
    trade_date: date
    revision: int
    maturity_days: int
    action: Literal["add", "replace"]

@dataclass(frozen=True)
class ReportMemoryOperation(ReportMemoryWork):
    content: str
    old_text: str | None
```

Expose `list_pending_report_memory(store, limit=18)`,
`begin_report_memory(store, session_id, revision)`,
`confirm_report_memory(store, session_id, revision, verifier)`, and
`quarantine_report_memory(store, session_id, revision, error_code)` with the
return types shown by the dataclasses and `ReportLearningRecord`.

Only the earliest ready revision after `confirmed_revision` is listable. `begin`
transitions pending to `memory_call_started` and is idempotent from that state.
Revision 1 returns add with no `old_text`; later revisions return replace with the
stable marker. Confirmation sets the snapshot confirmed and advances only one
revision. `confirm_report_memory` first changes `memory_call_started` to
`verification_pending`, then invokes the read-only verifier. Verification failure
sets that revision to `attention_required` and blocks later revisions.

- [ ] **Step 4: Implement the read-only verifier**

Add `verify_report_memory_consistency(session_id, revision, results_root,
memory_path)`. Load the report record and v2 symbol index, require the target
snapshot ready, require one stable marker and one exact desired entry, and require
the index to match the record's latest reflected revision. Return only safe
booleans, counts, identity, and revision. Never write the memory file.

- [ ] **Step 5: Add safe runner modes**

Add:

```text
report-memory-pending --limit 18
begin-report-memory --session-id <id> --revision <1|2|3>
confirm-report-memory --session-id <id> --revision <1|2|3>
quarantine-report-memory --session-id <id> --revision <1|2|3> --error-code <allowlisted>
```

Only `begin-report-memory` returns `content` and optional `old_text`, because the
Agent needs the exact values for the built-in memory call. Pending and
confirmation outputs remain metadata-only. `confirm` uses the default
`~/.hermes/memories/MEMORY.md` read-only path with the same optional test override
pattern as the legacy command.

- [ ] **Step 6: Run promotion and runner tests**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_report_memory tests.test_hermes_scheduled_review_runner -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add tradingagents/integrations/hermes_report_memory.py tradingagents/integrations/hermes_report_memory_verifier.py tradingagents/integrations/hermes_scheduled_review_runner.py tests/test_hermes_report_memory.py tests/test_hermes_scheduled_review_runner.py
git commit -m "feat: promote report revisions to Hermes memory"
```

### Task 8: Feed Balanced Report Lessons Into Decision Agents

**Files:**
- Modify: `tradingagents/integrations/hermes_mcp.py:609-618,792-805`
- Modify: `tradingagents/agents/trader/trader.py:16-34`
- Modify: `tradingagents/agents/researchers/bull_researcher.py:18-43`
- Modify: `tradingagents/agents/researchers/bear_researcher.py:18-45`
- Modify: `tradingagents/agents/managers/research_manager.py:15-36`
- Modify: `tradingagents/agents/managers/risk_manager.py:18-44`
- Modify: `tests/test_hermes_mcp.py:500-550`
- Create: `tests/test_hermes_report_lesson_prompts.py`

- [ ] **Step 1: Write failing graph and prompt tests**

Extend the MCP test so `_load_review_lessons("BTC")` receives the balanced five
rendered by `LearningStore`. Add lightweight fake-LLM tests for Trader and Risk
Manager:

```python
def test_trader_requires_applicability_check_without_forcing_action(self):
    llm = CapturingLlm("FINAL TRANSACTION PROPOSAL: **HOLD**")
    memory = StaticMemory(["BTC report lesson."])
    create_trader(llm, memory)(trader_state(), "Trader")
    prompt = llm.calls[0][0]["content"]
    self.assertIn("assess whether each historical lesson applies", prompt)
    self.assertIn("must not override current evidence", prompt)

def test_risk_manager_treats_report_lessons_as_hypotheses(self):
    llm = CapturingLlm("HOLD")
    create_risk_manager(llm, StaticMemory(["BTC report lesson."]))(
        risk_manager_state()
    )
    self.assertIn("evidence-bounded hypotheses", llm.calls[0])
```

Use source-text assertions for Bull, Bear, and Research Manager to require the
same applicability instruction without building their full debate states.

- [ ] **Step 2: Run tests and verify failure**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_mcp tests.test_hermes_report_lesson_prompts -v
```

Expected: new prompt assertions fail.

- [ ] **Step 3: Update graph loading and prompts**

Keep the `hermes_review_lessons` config key for compatibility, but rename the
private loader to `_load_learning_lessons`. It must call the balanced
`LearningStore.lessons_for(symbol, limit=5)` and retain the fail-closed empty
fallback.

Add this instruction, adapted to each existing prompt without changing its
required output format:

```text
Treat report-level lessons as evidence-bounded historical hypotheses. Assess
whether each lesson applies to the current market context, explain any mismatch,
and do not let historical outcomes override current evidence or mechanically
force BUY, SELL, or HOLD.
```

- [ ] **Step 4: Run tests**

Run the Task 8 command again. Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add tradingagents/integrations/hermes_mcp.py tradingagents/agents/trader/trader.py tradingagents/agents/researchers/bull_researcher.py tradingagents/agents/researchers/bear_researcher.py tradingagents/agents/managers/research_manager.py tradingagents/agents/managers/risk_manager.py tests/test_hermes_mcp.py tests/test_hermes_report_lesson_prompts.py
git commit -m "feat: inject balanced report lessons into decisions"
```

### Task 9: Update The Scheduled Hermes Skill And Deployment Runbook

**Files:**
- Modify: `deploy/hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md`
- Modify: `docs/hermes_integration.md:332-395`
- Modify: `tests/test_hermes_review_verifier.py:185-280`

- [ ] **Step 1: Write failing source-controlled workflow tests**

Update the skill/runbook test to require:

```python
def test_scheduled_skill_promotes_legacy_then_report_memory(self):
    skill = SCHEDULED_REVIEW_SKILL_PATH.read_text(encoding="ascii")
    self.assertIn("memory-pending --limit 18", skill)
    self.assertIn("report-reflection-pending --limit 18", skill)
    self.assertIn("report-reflection-evidence", skill)
    self.assertIn("submit_report_reflection", skill)
    self.assertIn("begin-report-memory", skill)
    self.assertIn("action=add", skill)
    self.assertIn("action=replace", skill)
    self.assertIn("confirm-report-memory", skill)
    self.assertIn("quarantine-report-memory", skill)
    self.assertNotIn("memory(action=read", skill)
    self.assertNotIn("edit MEMORY.md", skill)

def test_runbook_documents_v2_cutover_and_single_entry_acceptance(self):
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    self.assertIn("scheduled_review_version: 2", text)
    self.assertIn("report_memories/<session_id>.json", text)
    self.assertIn("T+1 add", text)
    self.assertIn("T+7/T+15 replace", text)
    self.assertIn("旧 v1", text)
    self.assertIn("只有一个 Hermes memory 条目", text)
```

- [ ] **Step 2: Run tests and verify failure**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_review_verifier -v
```

Expected: v2 workflow assertions fail.

- [ ] **Step 3: Rewrite the scheduled skill as a fixed bounded protocol**

The skill must:

1. drain bounded legacy `memory-pending` work using the existing exact add and
   confirmation behavior;
2. list at most 18 report reflections;
3. fetch one exact evidence packet, create a structured reflection using only
   that packet, and call `mcp__tradingagents_crypto__submit_report_reflection`;
4. list report-memory work, call `begin-report-memory`, then call Hermes memory
   exactly once with returned add/replace arguments;
5. accept `Entry added`, `Entry already exists`, or `Entry replaced` only for the
   matching action;
6. confirm accepted mutations; quarantine any other memory response without
   printing its content; and
7. report only IDs, symbols, revisions, states, counts, and safe errors.

Explicitly prohibit external searches, exchange access, real trading, external
messages, raw memory reads, and terminal writes to `MEMORY.md`.

- [ ] **Step 4: Update the deployment runbook**

Document source installation, paused replacement jobs, the shared v1/v2 job
pair, a newly archived v2 acceptance report, T+1 add and T+7/T+15 replace checks,
exact-one marker verification, failure quarantine, rollback by pausing jobs, and
retention of all project and Hermes artifacts. Keep 08:15 and 08:30
`Asia/Shanghai`; add no API keys.

- [ ] **Step 5: Run workflow tests**

Run the Task 9 command again. Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add deploy/hermes/skills/tradingagents-scheduled-paper-reviews/SKILL.md docs/hermes_integration.md tests/test_hermes_review_verifier.py
git commit -m "docs: deploy report-level Hermes reviews"
```

### Task 10: Add End-To-End Coverage And Verify The Branch

**Files:**
- Modify: `tests/test_hermes_scheduled_reviews.py`
- Modify: `tests/test_hermes_report_learning.py`
- Modify: `tests/test_hermes_report_memory.py`
- Modify: `docs/superpowers/plans/2026-08-05-hermes-report-level-learning.md` only to mark completed checkboxes during execution

- [ ] **Step 1: Write the full lifecycle integration test**

Add a test that uses temporary report batches, sessions, reviews, report stores,
symbol indexes, and fake memory text:

```python
def test_v2_report_lifecycle_keeps_one_project_and_memory_entry(self):
    harness = VersionTwoLifecycleHarness()
    harness.archive_new_report()
    harness.process_horizon(1, return_pct=1.2, verdict="correct")
    harness.reflect_and_promote(expected_action="add")
    harness.process_horizon(7, return_pct=-4.2, verdict="incorrect")
    harness.reflect_and_promote(expected_action="replace")
    harness.process_horizon(15, return_pct=3.1, verdict="correct")
    harness.reflect_and_promote(expected_action="replace")

    self.assertEqual(len(harness.review_store_files()), 3)
    self.assertEqual(len(harness.report_store_files()), 1)
    self.assertEqual(len(harness.index().report_entries), 1)
    self.assertEqual(harness.record().desired_revision, 3)
    self.assertEqual(harness.record().confirmed_revision, 3)
    self.assertEqual(harness.memory_marker_count(), 1)
    self.assertEqual(harness.memory_entry_count_for_report(), 1)
```

Implement the harness with project functions and a tiny fake memory adapter that
matches Hermes exact add-dedup and marker-based replace semantics. Do not import
or touch the user's Hermes installation or memory file.

- [ ] **Step 2: Run all Hermes integration tests**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest discover -s tests -p 'test_hermes*.py' -v
```

Expected: all Hermes tests pass.

- [ ] **Step 3: Run the complete test suite**

```bash
/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest discover -s tests -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 4: Run static repository checks**

```bash
git diff --check
rg -n "MEMORY\.md" deploy/hermes tradingagents/integrations tests
rg -n "scheduled_review_version" tradingagents tests docs/hermes_integration.md
git status --short
```

Expected: no whitespace errors; every `MEMORY.md` mutation prohibition is still
present; version dispatch references are intentional; status contains only the
planned feature changes.

- [ ] **Step 5: Commit integration coverage**

```bash
git add tests/test_hermes_scheduled_reviews.py tests/test_hermes_report_learning.py tests/test_hermes_report_memory.py
git commit -m "test: cover report-level learning lifecycle"
```

- [ ] **Step 6: Request code review before integration**

Use `superpowers:requesting-code-review` against the merge base with `main`.
Address verified findings with `superpowers:receiving-code-review`, rerun the
complete suite, and use `superpowers:verification-before-completion` before
claiming the branch is ready.
