# Hermes MCP Async Analysis Design

## Goal

Make `analyze_crypto` accept a paper-trading request and return promptly. A
detached worker runs the long TradingAgents graph, and `get_analysis_result`
remains the status and result lookup API.

## Problem

The current MCP handler calls `TradingAgentsGraph.propagate()` in the stdio MCP
process. A long graph blocks keepalives, Hermes reconnects the stdio server,
and the client times out while the session can remain incorrectly `running`.

## Chosen Design

`analyze_crypto` validates the request and provider credential, persists an
atomic `queued` session, starts a detached Python worker using the MCP
interpreter, records its PID, and returns the session ID immediately. It never
waits for a provider or data API response.

The worker receives only the opaque session ID. It takes an exclusive
cross-process file lock under `results/hermes`, changes the session to
`running`, and invokes the existing graph logic. It atomically persists a
completed result or the existing sanitized `ANALYSIS_FAILED` error. Its stdout
and stderr go to a per-session log file, not MCP stdout, and it starts a new
process session so an MCP reconnect cannot terminate it.

Only one worker owns the graph lock at a time; accepted later requests remain
`queued`. This keeps the current serialized execution policy without blocking
MCP operations.

## Session Contract

`AnalysisSession.status` adds `queued` to the existing `running`, `completed`,
and `failed` values. `started_at` and `worker_pid` are optional so existing
schema-version-1 JSON files stay readable. `get_analysis_result` returns any
state. When it observes a queued or running session with a recorded dead PID,
it atomically stores a sanitized `WORKER_EXITED` failure instead of reporting a
permanent non-terminal state.

## Error Handling

Invalid input, unavailable storage, and missing credentials fail before a
session is queued. A `Popen` failure marks the persisted session
`WORKER_START_FAILED`. Runtime worker exceptions retain the existing
`ANALYSIS_FAILED` contract. No exception text, credentials, or environment is
returned by an MCP tool.

## Compatibility

The internal synchronous `execute_analysis` helper remains for existing tests
and the current fallback runner. Only public MCP `analyze_crypto` becomes
asynchronous. The runbook changes to polling after submission. No new queue
broker, database, network service, or public port is introduced.
