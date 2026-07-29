# Hermes Review Operations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox `- [ ]` syntax for tracking.

**Goal:** Automate explicit Hermes paper-review memory promotion, maintain persisted asynchronous sessions, and provide a fail-closed multi-provider historical USD reference chain.

**Architecture:** A provider resolver obtains both review dates from one provider before a review is saved. Independent verifier and maintenance CLIs operate on existing result files without an LLM or an order path. A versioned Hermes skill calls MCP plus Hermes's own memory tool, and a systemd timer runs maintenance without secrets.

**Tech Stack:** Python 3.12, Pydantic v2, requests, unittest, FastMCP 1.28.1, Hermes Agent skills, systemd.

---

## File Structure

| File | Responsibility |
| --- | --- |
| tradingagents/dataflows/crypto_price_references.py | CoinGecko, CryptoCompare, Coinbase same-provider historical USD resolver. |
| tradingagents/integrations/schemas.py | Persisted PriceReference.source values. |
| tradingagents/integrations/hermes_learning.py | Persists resolver-returned references. |
| tradingagents/integrations/hermes_mcp.py | Resolver injection, non-secret key health, shared dead-worker reconciliation. |
| tradingagents/integrations/hermes_review_verifier.py | Read-only review/index/memory consistency CLI. |
| tradingagents/integrations/hermes_maintenance.py | Dead-worker repair and log-retention CLI. |
| deploy/hermes/skills/tradingagents-paper-review/SKILL.md | Explicit review-to-memory Hermes workflow. |
| deploy/systemd/tradingagents-hermes-maintenance.* | Secret-free systemd service and timer. |
| docs/hermes_integration.md | Cloud install and operator runbook. |

### Task 1: Add Same-Provider Historical USD Resolver

**Files:**
- Create: tradingagents/dataflows/crypto_price_references.py
- Create: tests/test_crypto_price_references.py
- Modify: tradingagents/integrations/schemas.py:140-150
- Test: tests/test_hermes_schemas.py

- [ ] **Step 1: Write failing resolver and schema tests.**

~~~python
def test_chain_uses_fallback_for_both_dates_after_primary_failure(self):
    values = resolve_historical_usd_references(
        "BTC", [ENTRY, REVIEW], providers=[failing, fallback]
    )
    self.assertEqual([item.source for item in values], ["cryptocompare", "cryptocompare"])

def test_chain_rejects_provider_missing_one_date(self):
    with self.assertRaises(HistoricalPriceUnavailable):
        resolve_historical_usd_references("BTC", [ENTRY, REVIEW], providers=[partial])

def test_price_reference_accepts_declared_fallback_source(self):
    self.assertEqual(
        PriceReference(date=ENTRY, usd_price=1, source="coinbase").source,
        "coinbase",
    )
~~~

- [ ] **Step 2: Run the tests and confirm RED.**

Run:

~~~bash
VENV=/Users/xiashan/Site/0-ext/TradingAgents-crypto/.worktrees/hermes-mcp-async-jobs/.venv-hermes-mcp/bin/python
$VENV -m unittest tests.test_crypto_price_references tests.test_hermes_schemas -v
~~~

Expected: missing resolver module and unsupported source validation failures.

- [ ] **Step 3: Implement providers and one-provider batch selection.**

~~~python
def resolve_historical_usd_references(symbol, dates, providers=None):
    for provider in providers or configured_providers():
        try:
            values = provider.references(symbol, dates)
        except HistoricalPriceUnavailable:
            continue
        if len(values) == len(dates) and {value.date for value in values} == set(dates):
            if len({value.source for value in values}) == 1:
                return [next(value for value in values if value.date == day) for day in dates]
    raise HistoricalPriceUnavailable("historical USD references are unavailable")
~~~

Add coingecko, cryptocompare, and coinbase to the source literal. Use the
existing CoinGecko helper; require a cleaned CRYPTOCOMPARE_API_KEY for
CryptoCompare's exact UTC daily USD candle; use public symbol-USD Coinbase
daily candles. Reject non-positive, non-finite, or wrong-date values without
logging headers, keys, or provider responses.

- [ ] **Step 4: Run focused tests and commit.**

~~~bash
$VENV -m unittest tests.test_crypto_price_references tests.test_dataflow_requests tests.test_hermes_schemas -v
git add tradingagents/dataflows/crypto_price_references.py tradingagents/integrations/schemas.py tests/test_crypto_price_references.py tests/test_hermes_schemas.py
git commit -m "feat: add historical USD price fallback chain"
~~~

Expected: all tests pass without live HTTP.

### Task 2: Persist Price Sources in Paper Reviews

**Files:**
- Modify: tradingagents/integrations/hermes_learning.py:206-300
- Modify: tradingagents/integrations/hermes_mcp.py:24-42,151-196,644-759
- Modify: tests/test_hermes_learning.py
- Modify: tests/test_hermes_mcp.py

- [ ] **Step 1: Write failing integration tests.**

~~~python
def test_review_persists_one_fallback_source_for_both_prices(self):
    review = review_completed_session(
        session, REVIEW, paired_fallback, review_store, learning_store
    )
    self.assertEqual(
        (review.entry_price.source, review.review_price.source),
        ("cryptocompare", "cryptocompare"),
    )

def test_health_reports_cryptocompare_key_as_boolean_only(self):
    result = health_check_impl()
    self.assertIs(result["data"]["cryptocompare_key_available"], True)
    self.assertNotIn("secret-value", json.dumps(result))
~~~

- [ ] **Step 2: Run focused tests and confirm RED.**

~~~bash
$VENV -m unittest tests.test_hermes_learning tests.test_hermes_mcp -v
~~~

Expected: the old float lookup contract cannot persist fallback sources.

- [ ] **Step 3: Change the review contract to paired references.**

review_completed_session receives a (symbol, trade_date, review_date) resolver
and persists the two returned PriceReference objects directly. It still loads an
existing review before making a provider call. Derive return from usd_price;
make the memory sentence source-neutral. Wire the MCP default to
resolve_historical_usd_references, retain PRICE_DATA_UNAVAILABLE when all
providers fail, and add only cryptocompare_key_available to health output.

- [ ] **Step 4: Run tests and commit.**

~~~bash
$VENV -m unittest tests.test_hermes_learning tests.test_hermes_mcp tests.test_hermes_schemas -v
git add tradingagents/integrations/hermes_learning.py tradingagents/integrations/hermes_mcp.py tests/test_hermes_learning.py tests/test_hermes_mcp.py
git commit -m "feat: persist Hermes review price sources"
~~~

Expected: repeated reviews repair only the project learning index and make no provider call.

### Task 3: Add Read-Only Review Consistency Verification

**Files:**
- Create: tradingagents/integrations/hermes_review_verifier.py
- Create: tests/test_hermes_review_verifier.py

- [ ] **Step 1: Write failing temporary-file tests.**

~~~python
def test_verifier_requires_review_index_and_one_memory_entry(self):
    result = verify_review_consistency(review_id, results_root, memory_path)
    self.assertEqual(result.hermes_memory_occurrences, 1)

def test_verifier_rejects_duplicate_memory_entry(self):
    with self.assertRaises(ReviewVerificationError):
        verify_review_consistency(review_id, results_root, duplicate_memory_path)
~~~

- [ ] **Step 2: Confirm RED.**

~~~bash
$VENV -m unittest tests.test_hermes_review_verifier -v
~~~

Expected: verifier import fails.

- [ ] **Step 3: Implement the API and CLI.**

~~~python
def verify_review_consistency(review_id, results_root, memory_path):
    review = ReviewStore(results_root / "hermes" / "reviews").load(review_id)
    index = LearningStore(results_root / "hermes" / "memories").load(review.symbol) if review else None
    occurrences = memory_path.read_text(encoding="utf-8").count(review.hermes_memory_entry) if review else 0
    if review is None or index is None or review_id not in {entry.review_id for entry in index.entries} or occurrences != 1:
        raise ReviewVerificationError("review consistency check failed")
    return ReviewVerification(review_id, True, True, occurrences)
~~~

main() validates --review-id, resolves optional --results-dir and
--hermes-memory-path, prints only safe booleans plus ID as JSON, and exits one
for missing, corrupt, or duplicate state.

- [ ] **Step 4: Run verifier tests, compile, and commit.**

~~~bash
$VENV -m unittest tests.test_hermes_review_verifier -v
$VENV -m py_compile tradingagents/integrations/hermes_review_verifier.py
git add tradingagents/integrations/hermes_review_verifier.py tests/test_hermes_review_verifier.py
git commit -m "feat: verify Hermes review memory consistency"
~~~

Expected: tests use only temporary results roots and memory files.

### Task 4: Maintain Dead Workers and Worker Logs

**Files:**
- Create: tradingagents/integrations/hermes_maintenance.py
- Create: tests/test_hermes_maintenance.py
- Modify: tradingagents/integrations/hermes_mcp.py:200-287

- [ ] **Step 1: Write failing liveness and retention tests.**

~~~python
def test_maintenance_marks_dead_pid_but_keeps_live_and_untracked_sessions(self):
    report = run_maintenance(store, logs_root, worker_is_alive=lambda pid: pid == LIVE_PID)
    self.assertEqual(store.load(DEAD_ID).error.code, "WORKER_EXITED")
    self.assertEqual(store.load(LIVE_ID).status, "running")
    self.assertIn(UNTRACKED_ID, report.untracked_session_ids)

def test_maintenance_prunes_only_expired_worker_logs(self):
    run_maintenance(store, logs_root, now=NOW, log_retention_days=14)
    self.assertFalse(expired_log.exists())
    self.assertTrue(recent_log.exists())
~~~

- [ ] **Step 2: Confirm RED.**

~~~bash
$VENV -m unittest tests.test_hermes_maintenance -v
~~~

Expected: maintenance import fails.

- [ ] **Step 3: Extract shared reconciliation and implement CLI.**

Add a shared reconcile_session_worker(session, store, worker_is_alive) helper
used by get_analysis_result_impl and maintenance. It saves
_worker_exited_session only when a queued/running session has a recorded dead
PID. Maintenance iterates valid session JSON names, reports untracked active
sessions without changing them, deletes only logs/*.log older than 14 days, and
implements --dry-run without writes or unlink calls. Its JSON stdout contains
counts and opaque IDs only.

- [ ] **Step 4: Run regression tests and commit.**

~~~bash
$VENV -m unittest tests.test_hermes_maintenance tests.test_hermes_mcp -v
$VENV -m py_compile tradingagents/integrations/hermes_maintenance.py
git add tradingagents/integrations/hermes_maintenance.py tradingagents/integrations/hermes_mcp.py tests/test_hermes_maintenance.py tests/test_hermes_mcp.py
git commit -m "feat: maintain Hermes worker sessions and logs"
~~~

Expected: MCP lookup retains its existing dead-worker behavior.

### Task 5: Ship Skill, Timer Templates, and Runbook

**Files:**
- Create: deploy/hermes/skills/tradingagents-paper-review/SKILL.md
- Create: deploy/systemd/tradingagents-hermes-maintenance.service
- Create: deploy/systemd/tradingagents-hermes-maintenance.timer
- Modify: docs/hermes_integration.md:105-185
- Modify: tests/test_hermes_review_verifier.py

- [ ] **Step 1: Write failing static asset tests.**

~~~python
def test_skill_requires_mcp_memory_tool_and_verifier(self):
    text = SKILL_PATH.read_text(encoding="utf-8")
    self.assertIn("mcp__tradingagents_crypto__review_paper_decision", text)
    self.assertIn("memory tool", text)
    self.assertIn("hermes_review_verifier", text)

def test_timer_uses_maintenance_without_environment_file(self):
    service = SERVICE_PATH.read_text(encoding="ascii")
    self.assertIn("hermes_maintenance", service)
    self.assertNotIn("EnvironmentFile", service)
~~~

- [ ] **Step 2: Confirm RED.**

~~~bash
$VENV -m unittest tests.test_hermes_review_verifier -v
~~~

Expected: source-controlled deployment files are missing.

- [ ] **Step 3: Add deployment assets and update the runbook.**

The skill is explicit-only; calls review MCP; uses the Hermes memory tool rather
than terminal writes; runs the verifier; prohibits secrets and real orders. The
service runs as ubuntu with project WorkingDirectory, the Hermes venv,
NoNewPrivileges=true, PrivateTmp=true, and UMask=0077. The timer uses
OnBootSec=5min, OnUnitActiveSec=15min, and Persistent=true. Document skill
installation, fresh-session discovery, verifier, journal inspection, 14-day
worker-log retention, private CryptoCompare config, and the
CoinGecko/CryptoCompare/Coinbase fail-closed chain.

- [ ] **Step 4: Run full verification and commit.**

~~~bash
$VENV -m unittest discover -s tests -v
$VENV -m py_compile tradingagents/integrations/hermes_mcp.py tradingagents/integrations/hermes_learning.py tradingagents/integrations/hermes_review_verifier.py tradingagents/integrations/hermes_maintenance.py tradingagents/dataflows/crypto_price_references.py
git diff --check
git add deploy docs/hermes_integration.md tests/test_hermes_review_verifier.py
git commit -m "docs: automate Hermes review operations"
~~~

Expected: all tests pass and no whitespace errors.

### Task 6: Review, Merge, and Cloud Acceptance

**Files:**
- Modify: none before review

- [ ] **Step 1: Run the final local gate.**

~~~bash
$VENV -m unittest discover -s tests -v
git diff --check origin/main...HEAD
git status --short
~~~

Expected: all tests pass and no uncommitted files.

- [ ] **Step 2: Push and create a draft pull request.**

~~~bash
git push -u origin feature/hermes-review-operations
gh pr create --draft --base main --head feature/hermes-review-operations --title "[codex] Automate Hermes review operations"
~~~

- [ ] **Step 3: Merge after review without deleting the branch.**

~~~bash
PR_NUMBER="$(gh pr view --json number --jq '.number')"
gh pr ready "$PR_NUMBER"
gh pr merge "$PR_NUMBER" --merge
~~~

- [ ] **Step 4: Deploy the exact merge commit on ubuntu@124.222.79.66.**

~~~bash
PR_NUMBER="$(gh pr view --json number --jq '.number')"
MERGE_COMMIT="$(gh pr view "$PR_NUMBER" --json mergeCommit --jq '.mergeCommit.oid')"
cd /home/ubuntu/workspace/TradingAgents-crypto
git fetch origin main
git checkout --detach "$MERGE_COMMIT"
.venv-hermes-mcp/bin/python -m unittest discover -s tests -v
sudo install -D -m 644 deploy/systemd/tradingagents-hermes-maintenance.service /etc/systemd/system/tradingagents-hermes-maintenance.service
sudo install -D -m 644 deploy/systemd/tradingagents-hermes-maintenance.timer /etc/systemd/system/tradingagents-hermes-maintenance.timer
install -D -m 644 deploy/hermes/skills/tradingagents-paper-review/SKILL.md /home/ubuntu/.hermes/skills/blockchain/tradingagents-paper-review/SKILL.md
sudo systemctl daemon-reload
sudo systemctl enable --now tradingagents-hermes-maintenance.timer
~~~

- [ ] **Step 5: Verify cloud operation without exposing secrets.**

~~~bash
.venv-hermes-mcp/bin/python -m tradingagents.integrations.hermes_maintenance --dry-run
systemctl is-active tradingagents-hermes-maintenance.timer
hermes memory status
hermes skills list | rg tradingagents-paper-review
~~~

Use /tradingagents-paper-review in a fresh Hermes session for an existing
completed paper review and require the verifier to report review file, learning
index, and Hermes memory exactly once.
