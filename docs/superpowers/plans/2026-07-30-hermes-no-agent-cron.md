# Hermes No-Agent Daily Report Cron Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (\`- [ ]\`) syntax for tracking.

**Goal:** Replace LLM-driven daily-report Cron jobs with deterministic Hermes \`--no-agent\` scripts that submit and archive local paper-trading reports safely.

**Architecture:** A standard-library bootstrap is the only boundary between Hermes scripts and the runner, so import failures still return a safe JSON envelope. The runner computes an Asia/Shanghai trade date, invokes existing internal submit/lookup/archive functions, and renders a bounded deterministic Chinese narrative with secret-like-token redaction. Version-controlled shell wrappers invoke the bootstrap through the project virtual environment; a root-only Gateway \`EnvironmentFile\` supplies the values required by the detached analysis workers, and the runbook creates and pauses replacement jobs before removing paused agent jobs.

**Tech Stack:** Python 3.10+, stdlib \`argparse\`/\`json\`/\`zoneinfo\`, existing Pydantic report schemas and MCP implementation, Bash, Hermes Agent Cron, \`unittest\`.

---

## File Structure

| File | Responsibility |
| --- | --- |
| \`tradingagents/integrations/hermes_daily_report_bootstrap.py\` | Standard-library bootstrap that turns runner import failures into safe JSON. |
| \`tradingagents/integrations/hermes_daily_report_runner.py\` | Deterministic submit/archive CLI, date normalization, safe JSON output, bounded Chinese narrative. |
| \`tests/test_hermes_daily_report_runner.py\` | Unit tests without real workers, providers, or writes outside temporary directories. |
| \`deploy/hermes/scripts/tradingagents-daily-report-submit.sh\` | Owner-executable no-agent wrapper for submit mode. |
| \`deploy/hermes/scripts/tradingagents-daily-report-archive.sh\` | Owner-executable no-agent wrapper for archive mode. |
| \`tests/test_hermes_review_verifier.py\` | Static no-agent and local-only deployment safeguards. |
| \`docs/hermes_integration.md\` | Cloud replacement and manual validation runbook. |

### Task 1: Build The Submit Runner

**Files:**
- Create: \`tests/test_hermes_daily_report_runner.py\`
- Create: \`tradingagents/integrations/hermes_daily_report_runner.py\`

- [ ] **Step 1: Write failing tests for Asia/Shanghai date conversion and fixed submit input.**

~~~python
def test_shanghai_trade_date_converts_utc_before_selecting_day(self):
    instant = datetime(2026, 7, 30, 16, 30, tzinfo=timezone.utc)

    self.assertEqual(runner.shanghai_trade_date(instant).isoformat(), "2026-07-31")


def test_submit_uses_fixed_paper_research_request(self):
    captured = {}

    def submit(request):
        captured.update(request)
        return {
            "ok": True,
            "data": {"batch": {"batch_id": "report_0000000000000000"}},
        }

    code, payload = runner.run_submit(date(2026, 7, 30), submit)

    self.assertEqual(code, 0)
    self.assertEqual(captured["symbols"], ["BTC", "ETH", "SOL"])
    self.assertEqual(captured["analysts"], ["market", "news", "fundamentals"])
    self.assertEqual(captured["research_depth"], 1)
    self.assertEqual(payload["batch_id"], "report_0000000000000000")
~~~

- [ ] **Step 2: Run the focused tests and confirm RED.**

Run:

~~~bash
$VENV -m unittest \
  tests.test_hermes_daily_report_runner.HermesDailyReportRunnerTests.test_shanghai_trade_date_converts_utc_before_selecting_day \
  tests.test_hermes_daily_report_runner.HermesDailyReportRunnerTests.test_submit_uses_fixed_paper_research_request \
  -v
~~~

Expected: failure because the runner module does not exist.

- [ ] **Step 3: Implement only the submit primitives.**

~~~python
SHANGHAI = ZoneInfo("Asia/Shanghai")
FIXED_REQUEST = {
    "symbols": ["BTC", "ETH", "SOL"],
    "analysts": ["market", "news", "fundamentals"],
    "research_depth": 1,
    "llm_provider": "deepseek",
    "quick_model": "deepseek-v4-flash",
    "deep_model": "deepseek-v4-pro",
}


def shanghai_trade_date(now: datetime | None = None) -> date:
    instant = now or datetime.now(SHANGHAI)
    return instant.astimezone(SHANGHAI).date()


def run_submit(
    trade_date: date, submit=start_daily_report_batch_impl
) -> tuple[int, dict]:
    result = submit({**FIXED_REQUEST, "trade_date": trade_date.isoformat()})
    if not result.get("ok"):
        return 1, {"ok": False, "mode": "submit", "error": result["error"]}
    return 0, {
        "ok": True,
        "mode": "submit",
        "trade_date": trade_date.isoformat(),
        "batch_id": result["data"]["batch"]["batch_id"],
    }
~~~

Import the three existing \`*_daily_report_*_impl\` functions from
\`hermes_mcp\`. Do not start FastMCP, use terminal tools, or read config files
directly. Preserve the safe error envelope returned by the implementation.

- [ ] **Step 4: Add the safe-error test and verify GREEN.**

~~~python
def test_submit_returns_nonzero_and_safe_error(self):
    code, payload = runner.run_submit(
        date(2026, 7, 30),
        lambda _request: {
            "ok": False,
            "error": {"code": "REPORT_BATCH_UNREADABLE"},
        },
    )

    self.assertEqual(code, 1)
    self.assertEqual(payload["error"]["code"], "REPORT_BATCH_UNREADABLE")
~~~

Run:

~~~bash
$VENV -m unittest tests.test_hermes_daily_report_runner -v
~~~

Expected: all submit tests pass.

- [ ] **Step 5: Commit the submit runner.**

~~~bash
git add tradingagents/integrations/hermes_daily_report_runner.py tests/test_hermes_daily_report_runner.py
git commit -m "feat: add no-agent daily report submit runner"
~~~

### Task 2: Build Deterministic Archive Behavior

**Files:**
- Modify: \`tradingagents/integrations/hermes_daily_report_runner.py\`
- Modify: \`tests/test_hermes_daily_report_runner.py\`

- [ ] **Step 1: Write failing tests for Chinese narrative rendering and an active batch.**

~~~python
def test_narrative_is_deterministic_and_chinese(self):
    summary = {
        "state": "ready",
        "items": [{
            "symbol": "BTC",
            "status": "completed",
            "processed_signal": "BUY",
            "final_trade_decision": "买入",
            "error": None,
        }],
    }

    first = runner.render_archive_narrative(summary, None)
    second = runner.render_archive_narrative(summary, None)

    self.assertEqual(first, second)
    self.assertIn("批次状态：ready", first)
    self.assertIn("BTC：状态 completed", first)
    self.assertIn("仅用于研究和模拟交易", first)


def test_archive_active_returns_zero_without_calling_archive(self):
    archive_called = False

    def archive(_trade_date, _narrative):
        nonlocal archive_called
        archive_called = True
        return {"ok": True}

    code, payload = runner.run_archive(
        date(2026, 7, 30),
        lambda _trade_date: {
            "ok": True,
            "data": {
                "summary": {"state": "active", "items": []},
                "previous_report": None,
            },
        },
        archive,
    )

    self.assertEqual(code, 0)
    self.assertEqual(payload["state"], "active")
    self.assertFalse(archive_called)
~~~

- [ ] **Step 2: Run the focused tests and confirm RED.**

~~~bash
$VENV -m unittest \
  tests.test_hermes_daily_report_runner.HermesDailyReportRunnerTests.test_narrative_is_deterministic_and_chinese \
  tests.test_hermes_daily_report_runner.HermesDailyReportRunnerTests.test_archive_active_returns_zero_without_calling_archive \
  -v
~~~

Expected: failure because archive helpers do not exist.

- [ ] **Step 3: Implement bounded deterministic rendering and archive control flow.**

~~~python
def _short(value: object, limit: int = 500) -> str:
    text = str(value or "不可用").replace("\r", " ").replace("\n", " ").strip()
    return text if len(text) <= limit else f"{text[:limit]}..."


def render_archive_narrative(summary: dict, previous: dict | None) -> str:
    lines = [
        "本报告基于已持久化的日度研究批次生成，仅用于研究和模拟交易。",
        f"批次状态：{summary['state']}。",
    ]
    for item in summary["items"]:
        error = item.get("error") or {}
        signal = item.get("processed_signal") or error.get("code")
        decision = item.get("final_trade_decision") or error.get("code")
        lines.append(
            f"{item['symbol']}：状态 {item['status']}；"
            f"信号：{_short(signal)}；决策：{_short(decision)}。"
        )
    if previous is None:
        lines.append("无可比较的上一份归档报告。")
    else:
        lines.append(f"上一份归档交易日：{previous['trade_date']}。")
    lines.append("风险提示：信号与决策可能失效，不构成交易建议。")
    return "\n".join(lines)


def run_archive(
    trade_date: date,
    lookup=get_daily_report_batch_impl,
    archive=archive_daily_report_impl,
) -> tuple[int, dict]:
    lookup_result = lookup(trade_date.isoformat())
    if not lookup_result.get("ok"):
        return 1, {"ok": False, "mode": "archive", "error": lookup_result["error"]}
    data = lookup_result["data"]
    if data["summary"]["state"] == "active":
        return 0, {
            "ok": True,
            "mode": "archive",
            "state": "active",
            "trade_date": trade_date.isoformat(),
        }
    result = archive(
        trade_date.isoformat(),
        render_archive_narrative(data["summary"], data["previous_report"]),
    )
    if not result.get("ok"):
        return 1, {"ok": False, "mode": "archive", "error": result["error"]}
    return 0, {
        "ok": True,
        "mode": "archive",
        "trade_date": trade_date.isoformat(),
        **result["data"],
    }
~~~

Retain ordered items from the existing summary. Truncate every item-derived
value to 500 characters, which keeps the full three-symbol narrative below
the archive's 20,000-character limit.

- [ ] **Step 4: Write and run the terminal archive test.**

~~~python
def test_archive_passes_one_deterministic_narrative_to_existing_archive(self):
    seen = []
    lookup = lambda _trade_date: {
        "ok": True,
        "data": {
            "summary": {
                "state": "degraded",
                "items": [{
                    "symbol": "ETH",
                    "status": "failed",
                    "processed_signal": None,
                    "final_trade_decision": None,
                    "error": {"code": "ANALYSIS_FAILED"},
                }],
            },
            "previous_report": None,
        },
    }
    archive = lambda trade_date, narrative: seen.append((trade_date, narrative)) or {
        "ok": True,
        "data": {
            "filename": "2026-07-30.md",
            "sha256": "0" * 64,
            "state": "degraded",
        },
    }

    code, payload = runner.run_archive(date(2026, 7, 30), lookup, archive)

    self.assertEqual(code, 0)
    self.assertEqual(seen[0][0], "2026-07-30")
    self.assertIn("ANALYSIS_FAILED", seen[0][1])
    self.assertEqual(payload["filename"], "2026-07-30.md")
~~~

Run:

~~~bash
$VENV -m unittest tests.test_hermes_daily_report_runner -v
~~~

Expected: all archive tests pass.

- [ ] **Step 5: Add the CLI parser and test safe invalid dates.**

~~~python
def test_main_rejects_noncanonical_trade_date_as_safe_json(self):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = runner.main(["submit", "--trade-date", "20260730"])

    self.assertEqual(code, 1)
    self.assertEqual(
        json.loads(stdout.getvalue())["error"]["code"],
        "INVALID_REPORT_REQUEST",
    )
~~~

Implement \`main(argv: list[str] | None = None) -> int\` with \`submit\` and
\`archive\` modes plus optional \`--trade-date YYYY-MM-DD\`. A canonical input
must satisfy \`date.fromisoformat(value).isoformat() == value\`. Every path
prints exactly one \`json.dumps(payload, ensure_ascii=False, sort_keys=True)\`
line; parse errors return a safe \`INVALID_REPORT_REQUEST\` JSON envelope and
exit 1 without tracebacks.

- [ ] **Step 6: Run runner tests, compile, and commit archive behavior.**

~~~bash
$VENV -m unittest tests.test_hermes_daily_report_runner -v
$VENV -m py_compile tradingagents/integrations/hermes_daily_report_runner.py
git add tradingagents/integrations/hermes_daily_report_runner.py tests/test_hermes_daily_report_runner.py
git commit -m "feat: add deterministic no-agent report archive runner"
~~~

### Task 3: Add Hermes No-Agent Wrappers

**Files:**
- Create: \`deploy/hermes/scripts/tradingagents-daily-report-submit.sh\`
- Create: \`deploy/hermes/scripts/tradingagents-daily-report-archive.sh\`
- Modify: \`tests/test_hermes_review_verifier.py\`

- [ ] **Step 1: Write a failing static wrapper test.**

~~~python
def test_daily_report_no_agent_wrappers_are_fixed_and_secret_free(self):
    submit = SUBMIT_SCRIPT.read_text(encoding="ascii")
    archive = ARCHIVE_SCRIPT.read_text(encoding="ascii")

    self.assertIn("hermes_daily_report_bootstrap submit", submit)
    self.assertIn("hermes_daily_report_bootstrap archive", archive)
    self.assertIn(".venv-hermes-mcp/bin/python", submit)
    self.assertNotIn("hermes ", submit)
    self.assertNotIn("API_KEY", submit + archive)
~~~

- [ ] **Step 2: Run the static test and confirm RED.**

~~~bash
$VENV -m unittest tests.test_hermes_review_verifier.HermesReviewVerifierTests.test_daily_report_no_agent_wrappers_are_fixed_and_secret_free -v
~~~

Expected: failure because the wrappers do not exist.

- [ ] **Step 3: Create the executable wrappers.**

~~~bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=/home/ubuntu/workspace/TradingAgents-crypto
exec "$PROJECT_DIR/.venv-hermes-mcp/bin/python" -m tradingagents.integrations.hermes_daily_report_bootstrap submit "$@"
~~~

Create the archive wrapper with the same body except for the final \`archive\`
mode. Keep both files ASCII and set Git mode to \`100755\`.

- [ ] **Step 4: Run static tests and commit the wrappers.**

~~~bash
chmod 755 deploy/hermes/scripts/tradingagents-daily-report-submit.sh deploy/hermes/scripts/tradingagents-daily-report-archive.sh
$VENV -m unittest tests.test_hermes_review_verifier.HermesReviewVerifierTests.test_daily_report_no_agent_wrappers_are_fixed_and_secret_free -v
git add deploy/hermes/scripts tests/test_hermes_review_verifier.py
git commit -m "feat: add no-agent Hermes report Cron scripts"
~~~

### Task 4: Replace The Cloud Runbook Section

**Files:**
- Modify: \`docs/hermes_integration.md\`
- Modify: \`tests/test_hermes_review_verifier.py\`

- [ ] **Step 1: Write a failing static runbook test.**

~~~python
def test_daily_report_runbook_uses_paused_no_agent_local_jobs(self):
    runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

    self.assertIn("--no-agent --script", runbook)
    self.assertIn("tradingagents-daily-report-submit.sh", runbook)
    self.assertIn("tradingagents-daily-report-archive.sh", runbook)
    self.assertIn("hermes cron remove", runbook)
    self.assertNotIn("--skill tradingagents-daily-report", runbook)
~~~

- [ ] **Step 2: Run the static test and confirm RED.**

~~~bash
$VENV -m unittest tests.test_hermes_review_verifier.HermesReviewVerifierTests.test_daily_report_runbook_uses_paused_no_agent_local_jobs -v
~~~

Expected: failure because the agent-driven Cron commands remain documented.

- [ ] **Step 3: Replace only the daily-report Cron deployment commands.**

Document the exact replacement flow:

~~~bash
cd /home/ubuntu/workspace/TradingAgents-crypto
install -d -m 700 /home/ubuntu/.hermes/scripts
install -m 700 deploy/hermes/scripts/tradingagents-daily-report-submit.sh /home/ubuntu/.hermes/scripts/tradingagents-daily-report-submit.sh
install -m 700 deploy/hermes/scripts/tradingagents-daily-report-archive.sh /home/ubuntu/.hermes/scripts/tradingagents-daily-report-archive.sh

hermes cron create --name tradingagents-daily-report-submit --deliver local --no-agent --script /home/ubuntu/.hermes/scripts/tradingagents-daily-report-submit.sh --workdir /home/ubuntu/workspace/TradingAgents-crypto '0 8 * * *'
hermes cron create --name tradingagents-daily-report-archive --deliver local --no-agent --script /home/ubuntu/.hermes/scripts/tradingagents-daily-report-archive.sh --workdir /home/ubuntu/workspace/TradingAgents-crypto '0 12 * * *'
hermes cron pause '<new-submit-job-id>'
hermes cron pause '<new-archive-job-id>'
hermes cron remove '<old-agent-submit-job-id>'
hermes cron remove '<old-agent-archive-job-id>'
~~~

Immediately pause both created jobs. Document a root-only
\`/etc/tradingagents/hermes-gateway.env\` and its \`hermes-gateway.service\`
drop-in so no-agent scripts inherit the required values without reading a
project \`.env\`. Document manual submit, active archive, terminal archive,
JSON output, batch/report paths, mode-600 report checks, and durable-run
polling before each job is paused. State that no-agent jobs do not attach a
skill; retain the interactive skill for manual operator use.

- [ ] **Step 4: Run static tests and full regression.**

~~~bash
$VENV -m unittest tests.test_hermes_review_verifier -v
$VENV -m unittest discover -s tests -v
$VENV -m py_compile tradingagents/integrations/hermes_daily_report_runner.py
git diff --check
~~~

Expected: all tests pass, runner compiles, and the diff is clean.

- [ ] **Step 5: Commit documentation and prepare review.**

~~~bash
git add docs/hermes_integration.md tests/test_hermes_review_verifier.py
git commit -m "docs: run daily reports through no-agent Cron"
git status --short
git log --oneline origin/main..HEAD
~~~

Expected: a clean worktree with focused implementation commits ready for a new
PR. Do not replace the cloud jobs until the PR has been reviewed and merged.
