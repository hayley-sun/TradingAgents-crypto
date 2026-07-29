---
name: tradingagents-paper-review
description: Review one completed TradingAgents paper decision, promote one verified lesson to Hermes memory, and verify all persisted records.
---

# TradingAgents Paper Review

Run this workflow only when the user explicitly invokes `/tradingagents-paper-review`.
Do not run it merely because `review_paper_decision` was called directly.

1. Confirm that the request names a completed Hermes analysis session and a UTC
   review date after its trade date. This workflow is research and paper trading
   only, never a real order.
2. Call `mcp__tradingagents_crypto__review_paper_decision` with the session ID
   and review date. Stop and report the safe MCP error if the call fails.
3. Keep the returned `review.review_id` and exact `hermes_memory_entry`. Call
   the Hermes built-in memory tool exactly once with `action=add`, target
   `memory`, and the exact entry as content. Do not search, parse, or count raw
   `MEMORY.md` text before this call; the memory tool owns exact-entry
   deduplication and concurrent-write handling.
4. Continue only when the memory tool reports either `Entry added` or `Entry
   already exists`. For any other memory-tool result, stop and report the safe
   result. Do not retry `action=add` in the same workflow.
5. Run the read-only verifier with the review ID:

   ```bash
   /home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_review_verifier --review-id <review_id> --results-dir /home/ubuntu/workspace/TradingAgents-crypto/results --hermes-memory-path /home/ubuntu/.hermes/memories/MEMORY.md
   ```

6. Report success only when the verifier returns JSON with `ok: true` and
   `hermes_memory_occurrences: 1`. On a verifier failure, do not call
   `action=add` again and do not repair files manually. Use only the Hermes
   memory tool's targeted `replace` or `remove` operations for an approved
   memory repair, then rerun the verifier.

Never use terminal commands or file-writing tools to edit Hermes `MEMORY.md`.
Never expose credentials, place orders, connect exchange credentials, or turn a
paper-trading result into trading instructions.
