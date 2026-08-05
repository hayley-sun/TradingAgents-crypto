# Hermes Report-Level Learning Design

## Goal

Replace review-node learning with report-level learning for newly archived
TradingAgents paper reports. Each report progressively combines its T+1, T+7,
and T+15 outcomes with an evidence-bounded Hermes Agent reflection. The project
learning index exposes at most five balanced same-symbol report lessons to later
analyses, while Hermes long-term memory contains one progressively replaced entry
per report.

The feature remains research and paper trading only. It never places orders,
changes position sizes, or permits a script to modify Hermes `MEMORY.md`.

## Scope And Evidence Boundary

- Only daily report archives created after deployment with
  `scheduled_review_version: 2` use report-level learning.
- Version-1 schedules and memory entries are not backfilled, converted, deleted,
  or rewritten. In-flight version-1 schedules continue through their existing
  single-review workflow.
- The existing immutable T+1, T+7, and T+15 `PaperDecisionReview` records remain
  the canonical outcome facts.
- Reflection covers every outcome type: correct, incorrect, flat, and
  not-scored. Correct decisions receive a strengths and luck check; incorrect
  decisions receive mistake hypotheses; flat decisions receive signal-quality
  analysis; HOLD and unparseable decisions receive an uncertainty and missed-
  opportunity assessment.
- Reflection evidence is limited to the archived decision-time analyst reports,
  investment plan, Trader plan, final decision, processed signal, and canonical
  T+N outcomes.
- The Agent may not search later news, fetch new market commentary, call an
  exchange, or introduce external facts. A causal explanation is always labeled
  as an evidence-bounded hypothesis rather than established market causality.

## Architecture

The current no-agent processor continues to own deterministic price review. A
new report-learning layer separates immutable outcome facts from Agent-generated
reflection and Hermes-owned long-term memory mutation:

```text
12:00 new version-2 archive
  -> enroll T+1/T+7/T+15 schedule items

08:15 deterministic processor (--no-agent)
  -> create due immutable review
  -> upsert outcome into report_memories/<session_id>.json
  -> increment desired report revision
  -> mark reflection_pending

08:30 Hermes Agent + dedicated skill
  -> list bounded pending report revisions
  -> load one bounded evidence packet at a time
  -> submit structured reflection
  -> project validates and renders the report lesson
  -> project index immediately exposes the validated lesson
  -> Agent calls Hermes memory add or replace
  -> read-only verifier confirms the exact memory revision

Later same-symbol TradingAgents analysis
  -> select report lessons from the project symbol index
  -> inject lessons into the existing graph memory interface
```

The project learning path does not depend on querying Hermes `MEMORY.md`.
Hermes long-term memory provides cross-session continuity and auditability.

## Persistent Data

Project-owned state remains beneath the configured results directory:

```text
results/hermes/
  review_schedules/<YYYY-MM-DD>.json
  reviews/<review_id>.json
  report_memories/<session_id>.json
  memories/<SYMBOL>.json
```

### Immutable Outcome Reviews

`reviews/<review_id>.json` keeps the existing `PaperDecisionReview` format and
remains immutable. It records session, symbol, trade and review dates, horizon,
action, same-provider USD prices, return, verdict, creation time, and the legacy
single-review memory entry required by version-1 compatibility.

No Agent-generated market context or causal hypothesis is written into an
immutable review.

### Report Learning Record

`report_memories/<session_id>.json` is an atomically written,
concurrency-protected `ReportLearningRecord`. Its schema-version-1 fields are:

- identity: session ID, symbol, trade date, original action, and source digest;
- revision: monotonically increasing desired, reflected, and confirmed memory
  revisions;
- outcomes: unique canonical review projections ordered by T+1, T+7, and T+15;
- source metadata: allowed archived source-field names, field digests, and
  truncation flags;
- market context: decision thesis plus technical, sentiment, news, and
  fundamental context;
- reflection: overall assessment, reasoning strengths, bounded causal
  hypotheses, mistakes or missed opportunities, and next-decision checks;
- rendered lesson: deterministic project lesson and deterministic Hermes memory
  content for the reflected revision;
- reflection state and memory promotion state; and
- safe attempt metadata, timestamps, and error codes.

Each outcome projection contains only its review ID, horizon, review date,
return, and verdict. The canonical review remains the authority for prices and
all other facts.

Each causal hypothesis contains:

```json
{
  "statement": "Short-term momentum may have been treated as persistent.",
  "evidence": ["market_report", "outcome_t7"],
  "confidence": "medium"
}
```

Allowed confidence values are `low`, `medium`, and `high`. Evidence identifiers
must exist in the evidence packet for the same expected revision.

### Symbol Learning Index

`memories/<SYMBOL>.json` moves to schema version 2 with separate
`report_entries` and `legacy_entries` collections. Each report entry identifies
one session and contains trade date, maturity, reflected revision, update time,
and the current deterministic lesson. Each legacy entry preserves the complete
version-1 review ID, review date, and lesson.

The first version-2 upsert for a symbol atomically upgrades its version-1 index
to version 2 and copies every existing entry into `legacy_entries`; it does not
create a report reflection from that legacy data. Version-1 schedule processing
remains able to append or repair `legacy_entries` after the upgrade. Both
collections retain their complete backlogs without a storage cap. Report entries
are derived and repairable from report learning records, while canonical reviews
remain the authority for legacy entries.

## Evidence Packet

The Agent processes one report revision at a time. A project read command returns
a bounded evidence packet containing:

- session ID, expected revision, symbol, trade date, and original action;
- each available canonical T+N outcome;
- bounded excerpts from market, social, news, and fundamentals reports;
- bounded excerpts from the investment plan, Trader plan, final decision, and
  processed signal; and
- a digest and `truncated` flag for every source field.

Excerpt selection is deterministic. For a field over its limit, the packet keeps
bounded leading and trailing sections so that premises and final conclusions are
both represented. The packet declares omitted content; the Agent may not infer
that an omitted section supports a claim. Per-field and total-packet byte limits
prevent a normal nine-revision day from exceeding the Cron Agent context budget.

Command output and normal logs expose only queue metadata. Raw evidence is
returned only for the explicitly selected work item and is never printed by the
final Cron status summary.

## Reflection Protocol

The scheduled skill performs this sequence for each bounded pending item:

1. Request the evidence packet for the session and expected revision.
2. Produce the required structured reflection fields.
3. Submit the structured payload through a strict project API with the session
   ID and expected revision.
4. Continue only when the project validates, persists, indexes, and renders that
   exact revision.
5. Use the returned memory operation metadata to call the Hermes built-in memory
   tool once.
6. Run project confirmation for the session and revision only after an accepted
   memory-tool result.

The Agent does not directly author the final project lesson or Hermes memory
entry. A deterministic project renderer owns both texts after validating:

- all current outcomes are represented exactly;
- evidence names exist in the current packet;
- hypotheses contain a confidence value and inference wording;
- required assessment sections are present for the verdict types;
- list counts, item lengths, and total lengths are bounded;
- no real-order instruction, exchange credential, or unsupported source appears;
  and
- submitted expected revision still equals the record's desired revision.

A stale payload is rejected without changing the record or index. Repeated
submission of the identical reflection for the same revision is idempotent.

Market context and reflection are regenerated from the original evidence plus
all outcomes at every horizon. T+7 and T+15 are not appended mechanically to the
T+1 prose.

## Deterministic Lesson And Memory Rendering

One compact project lesson contains:

- report identity, original action, and current maturity;
- decision-time market context;
- all currently available T+N outcomes;
- overall assessment;
- evidence-bounded hypotheses with confidence;
- next-decision checks; and
- paper-trading and causality disclaimers.

The renderer enforces per-section and total byte limits using deterministic list
and text truncation. It never calls an LLM. Equal structured records always
produce equal output.

Hermes memory uses the same information and starts with a stable unique marker:

```text
[TradingAgents paper report: hermes_0123456789abcdef]
```

Every revision preserves the marker. T+1 uses
`memory(action=add, target=memory, content=<desired content>)`. T+7 and T+15 use
`memory(action=replace, target=memory, old_text=<stable marker>,
content=<desired content>)`. `old_text` therefore identifies exactly one report
without requiring the Agent to read raw Hermes memory.

No shell command, project runner, verifier, or skill may write `MEMORY.md`.

## Progressive State And Recovery

Report learning and Hermes synchronization advance independently:

```text
Learning:
facts_ready -> reflection_pending -> reflection_ready

Hermes memory:
add_pending / replace_pending
  -> memory_call_started
  -> verification_pending
  -> confirmed
```

The project index may expose a validated `reflection_ready` revision before its
Hermes memory promotion is confirmed. This allows the next TradingAgents
analysis to learn even during a Hermes memory outage.

Memory revisions are promoted in order. A later reflection may be prepared while
an earlier memory revision is unresolved, but it may not overtake that revision.
On a clean path:

- T+1 creates revision 1 and adds the first report entry;
- T+7 creates revision 2 and replaces revision 1; and
- T+15 creates revision 3 and replaces revision 2.

Accepted add results are `Entry added` and `Entry already exists`. The accepted
replace result is Hermes's successful replacement response. Every accepted
mutation enters read-only verification before confirmation.

The verifier requires:

- the stable marker occurs exactly once;
- the one complete entry equals the desired content for the expected revision;
- the corresponding report learning record and symbol index agree; and
- confirmed revision advances monotonically.

An Agent-generation failure or validation failure leaves reflection pending for
a later bounded retry and records only a safe error code. Repeated validation
failures reach an attention state after a fixed bounded attempt count.

An ambiguous memory result, missing or non-unique replacement target, or failed
post-write verification enters `attention_required`. Automatic memory mutation
stops for that report until an operator resolves it through the Hermes memory
tool. New canonical outcomes may still accumulate, and project reflection may
still advance.

All state writes are atomic and protected by report-level or store-level locks.
Expected-revision checks prevent an older Agent result from overwriting a newer
outcome set.

## Learning Selection For Later Decisions

The graph loads at most five same-symbol report lessons instead of five review
nodes. Selection balances recent context with mature outcomes:

1. select up to three newest version-2 reports with validated reflections;
2. select up to two newest version-2 reports that have a T+15 outcome;
3. deduplicate by session ID; and
4. fill any remaining positions by descending trade date.

If fewer than five version-2 reports exist, version-1 lessons may fill remaining
positions. Version-1 fallback contributes at most one lesson per source session
so one report's T+1, T+7, and T+15 nodes cannot crowd out other reports.

The renderer enforces a per-report lesson limit and a total five-report context
budget. The existing `hermes_review_lessons` graph configuration and
`FinancialSituationMemory` interface remain the delivery path. Trader, research,
and risk agents receive the lessons as reasoning context, not hard rules.

Historical lessons never override current market data, force BUY/SELL/HOLD,
change position size, or trigger an order. Prompts require agents to explain
whether a historical lesson applies to the current decision-time context rather
than copying it mechanically.

## Version-2 Enrollment And Compatibility

New archives created after deployment receive `scheduled_review_version: 2` and
enroll in report-level aggregation. Existing unmarked and version-1 archives are
not scanned or backfilled.

Version-1 schedules continue through the version-1 processor and single-entry
Hermes promotion until completion. Existing reviews, schedules, legacy index
entries, and Hermes memory entries remain available for audit. The one allowed
project-side conversion is the atomic symbol-index schema upgrade described
above; it preserves every version-1 index entry and does not create Agent
reflections or mutate Hermes memory.

Rollback pauses the version-2 Cron path and restores the previous deployed skill
and code. It never deletes reports, reviews, report memories, indexes, schedules,
or Hermes memory.

## Queue Bounds And Daily Capacity

With daily BTC, ETH, and SOL reports, a mature deployment normally receives nine
report revisions per day: three symbols at each of T+1, T+7, and T+15. The
reflection queue limit is 18 items so one missed run can drain alongside the
next normal run. Each evidence packet is capped at 4096 UTF-8 bytes and loaded
one at a time, keeping the worst-case Cron context bounded.

Work is ordered by desired revision creation time, trade date, symbol, and
session ID. The skill loads evidence one item at a time. Invalid or quarantined
items do not prevent independent later reports from progressing.

The limit of 18 and the evidence budget are source-controlled constants shared
by runner, schema validation, skill, tests, and deployment documentation. A
caller cannot raise either value through a Cron prompt.

## Security And Diagnostics

- The no-agent processor uses only configured historical-price providers and
  project storage.
- The Agent evidence API rejects unknown fields and stale revisions before
  persisting content.
- Cron prompts and skills prohibit external searches, exchange access, real
  trading, credential use, and external messaging.
- Safe status output contains IDs, revisions, counts, states, horizons, and
  allowlisted error codes. It does not print raw reports, rendered memory text,
  credentials, private paths, or raw exceptions.
- Hermes memory mutation remains exclusively owned by the built-in memory tool.
- Read-only verification may inspect the configured memory file but cannot
  modify it.

## Deployment

The existing schedules remain:

```text
08:15 Asia/Shanghai  deterministic scheduled-review processor
08:30 Asia/Shanghai  Hermes Agent reflection and memory promoter
```

No additional market-data API or news provider is required. Deployment updates:

- project schemas, report-learning domain/store, runner, verifier, MCP boundary,
  and graph lesson selection;
- the scheduled paper-review skill installed under `~/.hermes/skills`;
- fixed Cron prompt or script references required by the revised skill; and
- the Hermes integration runbook.

The replacement 08:15 processor dispatches both version-1 review nodes and
version-2 report facts. The replacement 08:30 skill drains bounded version-1
single-entry promotions first, then bounded version-2 reflection and report-
memory promotions. This lets in-flight version-1 schedules finish without a
second active Cron pair.

Replacement jobs are created paused and validated with both a version-1 fixture
and a newly archived version-2 test report. The old jobs are paused before
validation and removed only after the replacement pair succeeds. Enabling the
replacement jobs is the final deployment step.

## Testing

Unit and integration tests use temporary results directories, fake historical
price resolvers, structured fake Agent payloads, and an isolated fake Hermes
memory adapter. They never access the user's real `MEMORY.md`.

Coverage includes:

- report-learning schema validation, identity, revision, and field bounds;
- T+1/T+7/T+15 merge by session, deterministic ordering, duplicate idempotency,
  and out-of-order input;
- concurrent updates for different reports and stale-revision rejection for the
  same report;
- deterministic evidence excerpting, hashes, truncation flags, and total budget;
- rejection of unknown evidence, missing outcomes, invalid confidence,
  unsupported causal claims, and real-order instructions;
- deterministic renderer equality and per-report and total context budgets;
- balanced three-recent plus two-mature selection, deduplication, fill order,
  and version-1 fallback;
- memory add, replace, restart recovery, duplicate result, missing target,
  multiple targets, stale revision, and failed verification;
- exact-one marker and exact desired-content verification;
- queue bounds, normal nine-revision daily capacity, backlog ordering, and
  independent-item progress;
- skill, wrapper, and runbook assertions that forbid direct `MEMORY.md` writes,
  raw evidence output, external searches, and trading actions;
- version-2 enrollment only for new archives and continued version-1 behavior;
  and
- regression coverage for daily submit/archive, manual review, maintenance,
  existing graph memory, and current scheduled-review workflows.

An end-to-end test advances one version-2 report through T+1, T+7, and T+15. It
asserts that canonical review count reaches three, project report-memory and
symbol-index counts remain one, report revision reaches three, memory operations
are add then replace then replace, and the final fake Hermes memory contains one
exact report entry.

## Acceptance Criteria

1. A newly archived version-2 report receives one report learning record that
   progressively accumulates its canonical T+1, T+7, and T+15 outcomes.
2. Every outcome type receives a validated, evidence-bounded structured
   reflection based only on archived decision-time material and canonical price
   results.
3. The symbol learning index contains one current lesson per version-2 report,
   and later analyses receive at most five balanced same-symbol report lessons.
4. Hermes memory contains one stable entry per report. T+1 adds it, while T+7
   and T+15 replace it through the built-in memory tool.
5. Project scripts never modify `MEMORY.md`; exact post-write verification is
   read-only and fail-closed.
6. Reflection and memory failures preserve immutable reviews, do not lose newer
   outcomes, and recover or quarantine according to the defined state machine.
7. Version-1 schedules and artifacts remain compatible and are not backfilled or
   rewritten.
8. The full automated test suite passes, and deployment validation succeeds on
   a new isolated version-2 report before the revised Cron workflow is enabled.
