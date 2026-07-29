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
3. Keep the returned `review.review_id` and exact `hermes_memory_entry`.
   Use the Hermes built-in memory tool to search the current long-term memory
   for that exact entry before writing anything.
4. If the exact entry occurs zero times, use the Hermes memory tool to write it
   exactly once. If it occurs once, do not write it again. If it occurs more
   than once, do not write anything; report the duplicate state for operator
   remediation.
5. Run the read-only verifier with the review ID:

   ```bash
   /home/ubuntu/workspace/TradingAgents-crypto/.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_review_verifier --review-id <review_id> --results-dir /home/ubuntu/workspace/TradingAgents-crypto/results --hermes-memory-path /home/ubuntu/.hermes/memories/MEMORY.md
   ```

6. Report success only when the verifier returns JSON with `ok: true` and
   `hermes_memory_occurrences: 1`. On a verifier failure, report the safe
   result and do not repair files manually.

Never use terminal commands or file-writing tools to edit Hermes `MEMORY.md`.
Never expose credentials, place orders, connect exchange credentials, or turn a
paper-trading result into trading instructions.
