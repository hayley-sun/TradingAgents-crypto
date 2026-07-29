# Hermes MCP Async Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Return an accepted analysis request immediately from `analyze_crypto` and execute its TradingAgents graph in a durable, serialized worker process.

**Architecture:** The MCP process writes a `queued` session and detaches a worker using the same Python interpreter. The worker takes a file lock, transitions the session to a terminal state through atomic session-store writes, and `get_analysis_result` reads or repairs that persisted state.

**Tech Stack:** Python 3, Pydantic, FastMCP, `subprocess`, `fcntl`, `unittest`.

---

### Task 1: Extend The Session Contract

**Files:**
- Modify: `tradingagents/integrations/schemas.py:101-116`
- Test: `tests/test_hermes_schemas.py:93-120`

- [ ] Write a failing test that constructs an `AnalysisSession` with
  `status="queued"` and asserts `worker_pid` and `started_at` default to
  `None`.
- [ ] Run `/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_schemas -v` and observe the unsupported-status failure.
- [ ] Add `queued`, `started_at`, and positive optional `worker_pid` while
  retaining `schema_version: 1`.
- [ ] Re-run the schema tests and commit `feat: add queued Hermes analysis sessions`.

### Task 2: Queue A Detached Worker

**Files:**
- Modify: `tradingagents/integrations/hermes_mcp.py:62-417`
- Create: `tradingagents/integrations/hermes_analysis_worker.py`
- Test: `tests/test_hermes_mcp.py:243-343`

- [ ] Write failing tests for `start_analysis`: it creates `queued`, returns a
  session ID without calling the graph, persists the launcher PID, and marks a
  failed session with `WORKER_START_FAILED` when the injected launcher raises
  `OSError`.
- [ ] Run the focused test and observe that `start_analysis` is missing.
- [ ] Implement the queue entry point and launch with
  `subprocess.Popen([sys.executable, "-m", "tradingagents.integrations.hermes_analysis_worker", session_id], cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)`.
- [ ] Make `analyze_crypto` call `start_analysis`, re-run the focused tests,
  and commit `feat: run Hermes analyses in detached workers`.

### Task 3: Run And Recover Worker Sessions

**Files:**
- Modify: `tradingagents/integrations/hermes_mcp.py:179-417`
- Modify: `tradingagents/integrations/hermes_analysis_worker.py`
- Test: `tests/test_hermes_mcp.py:243-343`

- [ ] Write failing tests that run a queued session with `FakeGraph` to
  completion and turn a persisted non-live worker PID into `WORKER_EXITED`
  during result lookup.
- [ ] Run the focused tests and observe the missing worker runner failure.
- [ ] Factor graph execution into a shared runner. Keep synchronous
  `execute_analysis` behavior, but make the worker obtain an `fcntl.flock`,
  move the session to `running`, and use the shared runner.
- [ ] Re-run the focused tests and commit
  `fix: persist Hermes async analysis worker states`.

### Task 4: Document And Verify Operations

**Files:**
- Modify: `docs/hermes_integration.md:116-137`
- Test: `tests/test_hermes_mcp.py`

- [ ] Document submission followed by polling and the four session states.
- [ ] Run `/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest tests.test_hermes_mcp tests.test_hermes_schemas -v`.
- [ ] Run `/Users/xiashan/Site/0-ext/TradingAgents-crypto/.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v` and `git diff --check`.
- [ ] Commit `docs: describe asynchronous Hermes analysis polling`.
