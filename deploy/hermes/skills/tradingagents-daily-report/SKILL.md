---
name: tradingagents-daily-report
description: Submit or archive one scheduled TradingAgents daily crypto research report without external delivery or trading side effects.
---

# TradingAgents Daily Report

Run this workflow only when an operator explicitly invokes
`/tradingagents-daily-report` or a Hermes Cron prompt explicitly requests one
of the modes below. This workflow is research and paper trading only, never a
real order.

Never place an order, use exchange credentials, call any paper-decision review
tool, modify Hermes long-term memory, use external message delivery, or write
the report with terminal or file tools. The daily report MCP tools own batch
and archive persistence.

## Submit Mode

1. Determine the current `Asia/Shanghai` calendar date in `YYYY-MM-DD` form.
2. Call `mcp__tradingagents_crypto__start_daily_report_batch` once with:
   - `symbols=["BTC"]`
   - `analysts=["market", "news", "fundamentals"]`
   - `research_depth=1`
   - `llm_provider="deepseek"`
   - `quick_model="deepseek-v4-flash"`
   - `deep_model="deepseek-v4-pro"`
   - the current date as `trade_date`.
3. Report the opaque batch ID and per-symbol opaque session IDs or safe
   submission errors. Do not poll, retry, or start another batch in this run.

## Archive Mode

1. Determine the current `Asia/Shanghai` calendar date in `YYYY-MM-DD` form.
2. Call `mcp__tradingagents_crypto__get_daily_report_batch` once for that
   date.
3. If the returned summary state is `active`, report that the batch is still
   running and stop. Do not write a partial report.
4. If the state is `ready` or `degraded`, write a concise Chinese narrative
   from the returned safe per-symbol signals, decisions, errors, and previous
   report snapshot. Include signal changes, major risks, and confidence limits.
5. Call `mcp__tradingagents_crypto__archive_daily_report` exactly once with
   the date and the narrative. Report the returned filename, state, and digest.
   On an archive conflict, keep the existing archive and stop; never overwrite
   it.

Do not call this skill to create a trade instruction. The server adds the
paper-trading disclaimer to every archived report.
