---
name: tradingagents-scheduled-paper-reviews
description: Use when the attached Hermes Cron explicitly requests scheduled TradingAgents paper-review memory promotion.
---

# TradingAgents Scheduled Paper Reviews

Run this fixed, bounded protocol only when the attached Hermes Cron explicitly
requests scheduled TradingAgents paper-review memory promotion. This is research
and paper trading only, never a real order.

## 1. Drain legacy review memory (v1)

Run exactly once:

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap memory-pending --limit 18
```

Continue only for JSON with `ok: true`. Report `unavailable_count` and the
bounded `unavailable_review_ids` sample, and do not add or confirm unavailable
items. Continue with the valid items in `items`, in order. For each valid entry,
call the Hermes built-in memory tool exactly once with
`memory(action=add,target=memory,content=<exact hermes_memory_entry>)`.
Do not rewrite, summarize, combine, decorate, or print the entry. Only `Entry
added` or `Entry already exists` permits the matching read-only confirmation:

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap confirm-memory --review-id <review_id>
```

Accept only confirmation JSON with `ok: true` and `state: completed`. For any
other memory-tool response, do not call confirmation; leave the item in `memory_pending`.
Report only the safe error, and let an operator or a later run handle it. A
confirmation failure moves the project item to
`attention_required`. Do not retry, repair, or process either failed item again
in this run. Continue with independent later legacy items.

## 2. Reflect bounded report evidence (v2)

List metadata only, at most 18 items:

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap report-reflection-pending --limit 18
```

For each returned `session_id` and `revision`, fetch exactly one packet:

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap report-reflection-evidence --session-id <session_id> --revision <revision>
```

Use only that packet to produce one strict structured reflection. Do not search,
fetch another packet, infer missing facts, or include raw report/evidence text
in a summary. Call
`mcp__tradingagents_crypto__submit_report_reflection` once with the packet's
`session_id`, `revision` as `expected_revision`, and the structured reflection.
Continue only when the response has `ok: true`, `reflection_state: ready`, and
the returned project state is `add_pending` or `replace_pending`; otherwise
report only the safe error and continue with independent items.

## 3. Promote one report memory entry at a time

List report-memory metadata, bounded to 18:

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap report-memory-pending --limit 18
```

For each item, run exactly once:

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap begin-report-memory --session-id <session_id> --revision <revision>
```

Inspect only its returned `memory_state`, `action`, `content`, and (for
replacement) `old_text`:

- For `add_pending`, `replace_pending`, or `memory_call_started`, call Hermes
  built-in memory exactly once using the returned `action`, `content`, and
  `old_text` fields. T+1 must be
  `memory(action=add,target=memory,content=...)`. T+7 and T+15 must be
  `memory(action=replace,target=memory,old_text=<stable marker>,content=...)`.
- For `verification_pending`, do not mutate Hermes memory. Call
  `confirm-report-memory --session-id <session_id> --revision <revision>`
  directly; this is crash recovery after the mutation and prevents a second
  Hermes mutation.

For an add, accept only `Entry added` or `Entry already exists`. For a replace,
accept only Hermes's successful `Entry replaced` result. The result must match
the requested action; an ambiguous, missing, or other result is quarantined
without printing content:

```bash
/home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap quarantine-report-memory --session-id <session_id> --revision <revision> --error-code REPORT_MEMORY_RESULT_AMBIGUOUS
```

After an accepted mutation, call the read-only
`confirm-report-memory --session-id <session_id> --revision <revision>` command
and accept only `ok: true`. On verification failure, confirmation already persists `attention_required`;
report the safe status for operator investigation.
Do not call `quarantine-report-memory` after a confirmation failure, and never
call a memory mutation again for that revision in the same run. Process later
independent items.

## 4. Safety and reporting

Report only review/session IDs, symbols, revisions, states, counts, and
allowlisted safe error codes. Never print packet fields, evidence excerpts,
memory content, credentials, or filesystem paths. Never invoke the Hermes memory
tool to read or search long-term memory. Never edit Hermes `MEMORY.md` with a
shell, editor, terminal, or file-writing command; only the Hermes built-in
memory tool may mutate memory.

Do not perform external search or news retrieval, exchange access, credential
handling, real trading, order placement, or external messages. Do not turn a
paper-review lesson into trading instructions. Keep legacy and report queues
bounded and independent; an unavailable or quarantined item must not block the
rest of the run.
