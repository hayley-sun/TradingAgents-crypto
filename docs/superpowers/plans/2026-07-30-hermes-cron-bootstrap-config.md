# Hermes Cron Bootstrap Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the no-agent daily-report bootstrap safely load its allowlisted runtime values from the existing Hermes config.

**Architecture:** The bootstrap uses `yaml.safe_load` on one fixed Hermes config location, copies only six named TradingAgents values, and does so before runner import. Existing submit, archive, worker, and report behavior stays unchanged. The runbook removes the ineffective systemd secret duplicate and validates the repaired Cron path using a temporary historical-date job.

**Tech Stack:** Python 3.10+, PyYAML, `unittest`, Bash, Hermes Agent Cron, systemd.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `tradingagents/integrations/hermes_daily_report_bootstrap.py` | Load fixed TradingAgents config values before runner import. |
| `tests/test_hermes_daily_report_runner.py` | Unit coverage for isolation, load failure, and import ordering. |
| `tests/test_hermes_review_verifier.py` | Static no-agent deployment safeguards. |
| `docs/hermes_integration.md` | Cloud migration and temporary no-agent validation runbook. |

### Task 1: Add Bootstrap Tests And Loader

**Files:**
- Modify: `tests/test_hermes_daily_report_runner.py`
- Modify: `tradingagents/integrations/hermes_daily_report_bootstrap.py`

- [ ] **Step 1: Write failing whitelist and invalid-config tests.**

```python
loaded = bootstrap.load_tradingagents_cron_environment(config_path, environment)
self.assertTrue(loaded)
self.assertEqual(environment["DEEPSEEK_API_KEY"], "test-deepseek")
self.assertNotIn("UNRELATED_VALUE", environment)
```

```python
loaded = bootstrap.load_tradingagents_cron_environment(invalid_config, environment)
self.assertFalse(loaded)
self.assertEqual(environment, {"EXISTING": "value"})
```

- [ ] **Step 2: Verify RED.**

```bash
$VENV -m unittest tests.test_hermes_daily_report_runner -v
```

Expected: loader attribute is absent.

- [ ] **Step 3: Implement the fixed whitelist loader.**

```python
CRON_ENVIRONMENT_KEYS = (
    "TRADINGAGENTS_RESULTS_DIR",
    "DEEPSEEK_API_KEY",
    "FINNHUB_API_KEY",
    "COINGECKO_DEMO_API_KEY",
    "COINGECKO_PRO_API_KEY",
    "CRYPTOCOMPARE_API_KEY",
)
```

Use `yaml.safe_load` only. Resolve `HERMES_HOME/config.yaml`, falling back to
`Path.home() / ".hermes/config.yaml"`. Read only
`mcp_servers.tradingagents_crypto.env`, select non-empty string values for the
tuple above, and update the target environment only after all parsing succeeds.
Return `False` without output or mutation on any read or parse failure.

- [ ] **Step 4: Add an import-order test and verify GREEN.**

```python
with patch.object(bootstrap, "_load_default_cron_environment", side_effect=load), \
     patch.object(bootstrap, "import_module", side_effect=import_runner):
    self.assertEqual(bootstrap.main(["submit"]), 0)
self.assertEqual(events, ["load", "import"])
```

```bash
$VENV -m unittest tests.test_hermes_daily_report_runner -v
```

### Task 2: Replace The Ineffective Runbook Guidance

**Files:**
- Modify: `docs/hermes_integration.md`
- Modify: `tests/test_hermes_review_verifier.py`

- [ ] **Step 1: Add failing static assertions.**

```python
self.assertIn("allowlisted values from", runbook)
self.assertNotIn("EnvironmentFile=/etc/tradingagents/hermes-gateway.env", runbook)
self.assertNotIn("hermes-gateway.env", runbook)
```

- [ ] **Step 2: Verify RED.**

```bash
$VENV -m unittest tests.test_hermes_review_verifier -v
```

- [ ] **Step 3: Update the runbook.**

Explain Hermes credential stripping and the reviewed bootstrap read from the
existing `600` config. Delete EnvironmentFile creation and validation steps.
Document migration removal of the drop-in and duplicate file, then a temporary
historical-date no-agent job that exercises submit, active archive, and
terminal archive without resuming production jobs.

- [ ] **Step 4: Verify static GREEN.**

```bash
$VENV -m unittest tests.test_hermes_review_verifier -v
```

### Task 3: Full Verification, Review, And Cloud Deployment

**Files:**
- Modify: `tradingagents/integrations/hermes_daily_report_bootstrap.py`
- Modify: `tests/test_hermes_daily_report_runner.py`
- Modify: `tests/test_hermes_review_verifier.py`
- Modify: `docs/hermes_integration.md`
- Create: `docs/superpowers/specs/2026-07-30-hermes-cron-bootstrap-config-design.md`
- Create: `docs/superpowers/plans/2026-07-30-hermes-cron-bootstrap-config.md`

- [ ] **Step 1: Run full local checks.**

```bash
$VENV -m unittest discover -v
$VENV -m compileall -q tradingagents/integrations
git diff --check
```

- [ ] **Step 2: Commit and create a PR.**

```bash
git add tradingagents/integrations/hermes_daily_report_bootstrap.py tests/test_hermes_daily_report_runner.py tests/test_hermes_review_verifier.py docs/hermes_integration.md docs/superpowers/specs/2026-07-30-hermes-cron-bootstrap-config-design.md docs/superpowers/plans/2026-07-30-hermes-cron-bootstrap-config.md
git commit -m "fix: load Hermes config for no-agent Cron"
git push -u origin fix/hermes-cron-bootstrap-config
```

- [ ] **Step 3: After merge, deploy and accept on cloud.**

Verify a clean cloud worktree, detach at merged `origin/main`, run full tests,
and reinstall wrappers. Remove the existing `tradingagents-env.conf` and
`/etc/tradingagents/hermes-gateway.env`, daemon-reload, and restart Gateway.
Keep production jobs paused. Create temporary historical-date submit/archive
no-agent jobs, validate three sessions, active archive behavior, terminal
archive, mode `600`, and disclaimer, then remove temporary jobs and scripts.
Resume production jobs only after every acceptance check passes.
