---
name: tradingagents-scheduled-paper-reviews
description: Promote due, deterministic TradingAgents paper-review lessons through Hermes memory and confirm persisted consistency.
---

# TradingAgents Scheduled Paper Reviews

Run this workflow only when the attached Hermes Cron prompt explicitly requests
scheduled paper-review memory promotion. This is research and paper trading only,
never a real order.

1. Run exactly this project command once to obtain bounded pending work:

   ```bash
   /home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap memory-pending --limit 18
   ```

2. Continue only when the command returns JSON with `ok: true`. Process the
   returned items in order. Do not search or read raw Hermes memory.
3. For each item, call the Hermes built-in memory tool exactly once with
   `action=add`, target `memory`, and the exact `hermes_memory_entry` as content.
   Do not rewrite, summarize, combine, or decorate the entry.
4. If the memory tool returns `Entry added` or `Entry already exists`, run this
   confirmation command with that item's exact review ID:

   ```bash
   /home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_scheduled_review_bootstrap confirm-memory --review-id <review_id>
   ```

5. Treat only confirmation JSON with `ok: true` and `state: completed` as
   success. Report the review ID, symbol, horizon, and state without printing
   the memory entry.
6. For any other memory-tool result, do not call add again for that item in the
   same run and do not confirm it. Continue with independent later items.
7. On confirmation failure, do not retry, edit files, or attempt a repair in
   this run. The project quarantines the item for operator investigation.

Never edit Hermes `MEMORY.md` with terminal, shell, or file-writing tools. Never
expose credentials, connect exchange credentials, place orders, send external
messages, or turn a paper-review lesson into trading instructions.
