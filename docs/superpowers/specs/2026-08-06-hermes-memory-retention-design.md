# Hermes Memory Retention Design

## Goal

Keep every report's immutable reviews, report-learning record, and project
learning-index entry indefinitely, while keeping Hermes built-in `MEMORY.md`
within a configured capacity. A report still has one marker-delimited Hermes
entry while it is active and, after T+15 confirmation, while it is one of the
five retained completed reports for its symbol.

## Scope

This revision applies only to version-2 report-level Hermes memory entries. It
does not alter version-1 reviews, their existing memory workflow, report facts,
reflections, or the project learning-index selection used by TradingAgents.
Project code and scripts remain prohibited from writing `MEMORY.md`; every add,
replace, and removal remains an Hermes Agent built-in memory-tool operation.

## Retention Rules

For a symbol, an active report is any report whose revision 3 has not been
confirmed through the existing exact-content verifier. Active reports are never
retired. This guarantees that a T+1 add remains available for the required T+7
and T+15 marker-based replacements.

After a report's revision 3 is confirmed, it becomes a completed report. Hermes
retains the five newest completed reports for that symbol, ordered by trade
date descending and session ID descending as the deterministic tie-breaker.
Older completed reports are retirement candidates. Their project artifacts stay
unchanged and available forever; only their Hermes-memory copy is removed.

At daily BTC, ETH, and SOL cadence, each symbol has at most fifteen active
reports plus five retained completed reports. The deployment capacity calculation
therefore reserves 60 report entries across the three configured symbols.

## Retirement Journal

`results/hermes/report_memory_retirements/<SYMBOL>.json` is a locked,
atomically-written project journal. A retirement item contains the report
session ID, symbol, trade date, fixed revision 3, stable marker, and timestamps.
Its states are `pending`, `memory_call_started`, `verification_pending`,
`retired`, and `attention_required`.

Synchronizing a symbol's journal derives candidates only from report records
whose revision 3 is both reflected and confirmed. It creates at most one item
per report and never deletes journal history. The journal's `retired` state is
an audit record; it does not change `confirmed_revision`, immutable reviews, or
the report-learning index.

The Agent processes retirements after report-memory promotions in the same Cron
run. It requests one bounded pending retirement, calls only
`memory(action=remove, target=memory, old_text=<stable marker>)`, and accepts
only `Entry removed` for that operation. The project then read-only verifies
that the marker occurs zero times before marking the journal item `retired`.
Unexpected results, duplicate markers, missing markers, or failed verification
enter `attention_required` and preserve all project data. A failed retirement
does not block unrelated symbols or active report revisions.

## Compact Hermes Entry And Capacity

The project lesson remains bounded independently and contains the full report
level context used by future TradingAgents decisions. The Hermes-memory rendering
is a compact, deterministic derivative limited to 512 Unicode characters and
1536 UTF-8 bytes. It includes the stable marker, report identity and maturity,
all available outcomes, clipped decision-time market context, the most relevant
strength or mistake, one evidence-bounded hypothesis, one next-decision check,
and the paper-trading disclaimer. It never splits UTF-8 sequences.

Hermes built-in memory counts characters across the whole store and uses
`\n§\n` between entries. The runbook will configure
`memory.memory_char_limit: 40000`. Sixty maximum-size report entries plus their
delimiters occupy at most 30,897 characters, leaving at least 9,103 characters
for pre-existing operator memory. Before cutover, a read-only capacity verifier
must confirm that `MEMORY.md` has at most 9,000 characters and that the configured
limit is 40,000 or greater. It returns counts only and never exposes memory text.

The fixed deployment is deliberately bounded to BTC, ETH, and SOL. Adding
symbols requires recomputing and raising the configured limit before enabling
their report schedules; a runner must reject a capacity request above the
source-controlled bound rather than silently overcommit memory.

## Runner And Skill Protocol

The scheduled-review runner adds bounded, metadata-only commands to list,
begin, confirm, and quarantine report-memory retirements. Only the `begin`
command returns the marker needed by the built-in remove call. Normal logs show
only symbol, session ID, revision, state, and safe error codes.

The scheduled skill drains legacy v1 work, then report reflections and report
promotions exactly as today. It subsequently drains at most 18 retirement items.
It never reads raw memory and never calls a second mutation during a
`verification_pending` recovery. The deployment acceptance sequence validates
one complete report lifecycle, then creates enough completed fixtures to prove
that a sixth completed report retires only the oldest final report while active
reports remain untouched.

## Testing And Acceptance

Tests use temporary report stores and a fake Hermes adapter whose entries use
the real `\n§\n` delimiter. Coverage includes:

- no retirement before revision-3 confirmation;
- deterministic five-completed-per-symbol selection and no cross-symbol impact;
- active reports surviving retention reconciliation and receiving T+7/T+15
  replacements;
- exact marker-only removal, duplicate or missing-marker failure, retry state,
  and read-only absence verification;
- retention-journal atomicity and duplicate synchronization;
- compact entry character and UTF-8-byte limits with Chinese content;
- capacity arithmetic, safe metadata-only preflight, and rejection of requests
  beyond the fixed BTC/ETH/SOL deployment bound; and
- end-to-end promotion plus retirement without reading or writing a real Hermes
  memory file.

Acceptance requires all existing v1/v2 tests plus this coverage to pass. The
deployment runbook must configure the capacity, pass the count-only preflight,
and keep the old jobs paused until both report promotion and final-report
retirement verification succeed.
