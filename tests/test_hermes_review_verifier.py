import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingagents.integrations.hermes_learning import LearningStore, ReviewStore
from tradingagents.integrations.schemas import (
    PaperDecisionReview,
    PriceReference,
    utc_now,
)
from tradingagents.integrations.hermes_review_verifier import (
    ReviewVerificationError,
    main,
    verify_review_consistency,
)


REVIEW_ID = "review_0123456789abcdef"
MEMORY_ENTRY = "Paper-trading research lesson for BTC: review_0123456789abcdef."
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_PATH = PROJECT_ROOT / "deploy" / "hermes" / "skills" / "tradingagents-paper-review" / "SKILL.md"
DAILY_REPORT_SKILL_PATH = (
    PROJECT_ROOT
    / "deploy"
    / "hermes"
    / "skills"
    / "tradingagents-daily-report"
    / "SKILL.md"
)
SERVICE_PATH = PROJECT_ROOT / "deploy" / "systemd" / "tradingagents-hermes-maintenance.service"
TIMER_PATH = PROJECT_ROOT / "deploy" / "systemd" / "tradingagents-hermes-maintenance.timer"
RUNBOOK_PATH = PROJECT_ROOT / "docs" / "hermes_integration.md"
DAILY_REPORT_SUBMIT_SCRIPT = (
    PROJECT_ROOT
    / "deploy"
    / "hermes"
    / "scripts"
    / "tradingagents-daily-report-submit.sh"
)
DAILY_REPORT_ARCHIVE_SCRIPT = (
    PROJECT_ROOT
    / "deploy"
    / "hermes"
    / "scripts"
    / "tradingagents-daily-report-archive.sh"
)
SCHEDULED_REVIEW_PROCESS_SCRIPT = (
    PROJECT_ROOT
    / "deploy"
    / "hermes"
    / "scripts"
    / "tradingagents-scheduled-review-process.sh"
)
SCHEDULED_REVIEW_SKILL_PATH = (
    PROJECT_ROOT
    / "deploy"
    / "hermes"
    / "skills"
    / "tradingagents-scheduled-paper-reviews"
    / "SKILL.md"
)


def scheduled_acceptance_guard_script() -> str:
    text = RUNBOOK_PATH.read_text(encoding="utf-8")
    function_start = text.index("validate_acceptance_trade_date() {")
    heredoc_start = text.index("<<'PY'\n", function_start) + len("<<'PY'\n")
    heredoc_end = text.index("\nPY\n}", heredoc_start)
    return text[heredoc_start:heredoc_end]


def saved_review(results_root: Path) -> PaperDecisionReview:
    review = PaperDecisionReview(
        review_id=REVIEW_ID,
        session_id="hermes_0123456789abcdef",
        symbol="BTC",
        trade_date="2026-07-28",
        review_date="2026-07-29",
        action="BUY",
        entry_price=PriceReference(
            date="2026-07-28", usd_price=100.0, source="coinbase"
        ),
        review_price=PriceReference(
            date="2026-07-29", usd_price=110.0, source="coinbase"
        ),
        raw_return_pct=10.0,
        verdict="correct",
        created_at=utc_now(),
        hermes_memory_entry=MEMORY_ENTRY,
    )
    ReviewStore(results_root / "hermes" / "reviews").save(review)
    LearningStore(results_root / "hermes" / "memories").upsert(review)
    return review


class HermesReviewVerifierTests(unittest.TestCase):
    def test_verifier_accepts_legacy_entry_without_session_id(self):
        with TemporaryDirectory() as directory:
            results_root = Path(directory) / "results"
            saved_review(results_root)
            learning_path = (
                results_root / "hermes" / "memories" / "BTC.json"
            )
            payload = json.loads(learning_path.read_text(encoding="ascii"))
            payload["entries"][0]["session_id"] = None
            learning_path.write_text(json.dumps(payload), encoding="ascii")
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text(f"{MEMORY_ENTRY}\n", encoding="utf-8")

            result = verify_review_consistency(REVIEW_ID, results_root, memory_path)

        self.assertIs(result.learning_index_contains_review, True)

    def test_verifier_rejects_legacy_entry_with_wrong_non_null_session_id(self):
        with TemporaryDirectory() as directory:
            results_root = Path(directory) / "results"
            saved_review(results_root)
            learning_path = (
                results_root / "hermes" / "memories" / "BTC.json"
            )
            payload = json.loads(learning_path.read_text(encoding="ascii"))
            payload["entries"][0]["session_id"] = "hermes_ffffffffffffffff"
            learning_path.write_text(json.dumps(payload), encoding="ascii")
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text(f"{MEMORY_ENTRY}\n", encoding="utf-8")

            with self.assertRaises(ReviewVerificationError):
                verify_review_consistency(REVIEW_ID, results_root, memory_path)

    def test_verifier_finds_legacy_review_after_v2_index_upgrade(self):
        from tests.test_hermes_report_learning import report_learning_record

        with TemporaryDirectory() as directory:
            results_root = Path(directory) / "results"
            saved_review(results_root)
            LearningStore(results_root / "hermes" / "memories").upsert_report(
                report_learning_record()
            )
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text(f"{MEMORY_ENTRY}\n", encoding="utf-8")

            result = verify_review_consistency(REVIEW_ID, results_root, memory_path)

        self.assertIs(result.learning_index_contains_review, True)

    def test_verifier_rejects_wrong_legacy_lesson_after_v2_index_upgrade(self):
        from tests.test_hermes_report_learning import report_learning_record

        with TemporaryDirectory() as directory:
            results_root = Path(directory) / "results"
            review = saved_review(results_root)
            learning_store = LearningStore(results_root / "hermes" / "memories")
            learning_store.upsert_report(report_learning_record())
            learning_store.upsert(
                review.model_copy(update={"hermes_memory_entry": "Wrong v2 lesson."})
            )
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text(f"{MEMORY_ENTRY}\n", encoding="utf-8")

            with self.assertRaises(ReviewVerificationError):
                verify_review_consistency(REVIEW_ID, results_root, memory_path)

    def test_verifier_requires_review_index_and_one_memory_entry(self):
        with TemporaryDirectory() as directory:
            results_root = Path(directory) / "results"
            saved_review(results_root)
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text(f"# Memory\n\n{MEMORY_ENTRY}\n", encoding="utf-8")

            result = verify_review_consistency(REVIEW_ID, results_root, memory_path)

        self.assertEqual(result.review_id, REVIEW_ID)
        self.assertIs(result.review_exists, True)
        self.assertIs(result.learning_index_contains_review, True)
        self.assertEqual(result.hermes_memory_occurrences, 1)

    def test_verifier_rejects_duplicate_memory_entry(self):
        with TemporaryDirectory() as directory:
            results_root = Path(directory) / "results"
            saved_review(results_root)
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text(
                f"{MEMORY_ENTRY}\n{MEMORY_ENTRY}\n", encoding="utf-8"
            )

            with self.assertRaises(ReviewVerificationError):
                verify_review_consistency(REVIEW_ID, results_root, memory_path)

    def test_verifier_rejects_learning_entry_with_wrong_lesson(self):
        with TemporaryDirectory() as directory:
            results_root = Path(directory) / "results"
            review = saved_review(results_root)
            LearningStore(results_root / "hermes" / "memories").upsert(
                review.model_copy(
                    update={"hermes_memory_entry": "Different indexed lesson."}
                )
            )
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text(f"{MEMORY_ENTRY}\n", encoding="utf-8")

            with self.assertRaises(ReviewVerificationError):
                verify_review_consistency(REVIEW_ID, results_root, memory_path)

    def test_cli_outputs_only_safe_status_fields_and_failure_exit_code(self):
        with TemporaryDirectory() as directory:
            results_root = Path(directory) / "results"
            saved_review(results_root)
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text(f"{MEMORY_ENTRY}\n", encoding="utf-8")
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                success_exit_code = main(
                    [
                        "--review-id",
                        REVIEW_ID,
                        "--results-dir",
                        str(results_root),
                        "--hermes-memory-path",
                        str(memory_path),
                    ]
                )

            memory_path.write_text("missing entry\n", encoding="utf-8")
            failure_stdout = io.StringIO()
            with redirect_stdout(failure_stdout):
                failure_exit_code = main(
                    [
                        "--review-id",
                        REVIEW_ID,
                        "--results-dir",
                        str(results_root),
                        "--hermes-memory-path",
                        str(memory_path),
                    ]
                )

        success_payload = json.loads(stdout.getvalue())
        failure_payload = json.loads(failure_stdout.getvalue())
        self.assertEqual(success_exit_code, 0)
        self.assertEqual(
            set(success_payload),
            {
                "ok",
                "review_id",
                "review_exists",
                "learning_index_contains_review",
                "hermes_memory_occurrences",
            },
        )
        self.assertEqual(success_payload["review_id"], REVIEW_ID)
        self.assertEqual(failure_exit_code, 1)
        self.assertEqual(failure_payload, {"ok": False, "review_id": REVIEW_ID})
        self.assertNotIn(directory, failure_stdout.getvalue())
        self.assertNotIn(MEMORY_ENTRY, failure_stdout.getvalue())

    def test_skill_uses_hermes_memory_deduplication_and_verifier(self):
        text = SKILL_PATH.read_text(encoding="utf-8")

        self.assertIn("explicitly invokes", text)
        self.assertIn("mcp__tradingagents_crypto__review_paper_decision", text)
        self.assertIn("memory tool", text)
        self.assertIn("action=add", text)
        self.assertIn("already exists", text)
        self.assertNotIn("search the current long-term memory", text)
        self.assertIn("hermes_review_verifier", text)
        self.assertIn("never a real order", text)

    def test_timer_runs_secret_free_maintenance_service(self):
        service = SERVICE_PATH.read_text(encoding="ascii")
        timer = TIMER_PATH.read_text(encoding="ascii")

        self.assertIn("hermes_maintenance", service)
        self.assertIn("User=ubuntu", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("PrivateTmp=true", service)
        self.assertIn("UMask=0077", service)
        self.assertNotIn("EnvironmentFile", service)
        self.assertIn("OnBootSec=5min", timer)
        self.assertIn("OnUnitActiveSec=15min", timer)
        self.assertIn("Persistent=true", timer)

    def test_runbook_documents_skill_timer_and_price_fallback_configuration(self):
        text = RUNBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn("CRYPTOCOMPARE_API_KEY", text)
        self.assertIn("CoinGecko -> CryptoCompare -> Coinbase", text)
        self.assertIn("tradingagents-paper-review", text)
        self.assertIn("memory(action=add", text)
        self.assertIn("tradingagents-hermes-maintenance.timer", text)
        self.assertIn("hermes_review_verifier", text)

    def test_daily_report_skill_and_runbook_keep_reports_local(self):
        skill = DAILY_REPORT_SKILL_PATH.read_text(encoding="ascii")
        normalized_skill = " ".join(skill.split())
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn("start_daily_report_batch", skill)
        self.assertIn("get_daily_report_batch", skill)
        self.assertIn("archive_daily_report", skill)
        self.assertIn("never a real order", normalized_skill)
        self.assertNotIn("review_paper_decision", skill)
        self.assertNotIn("memory(action=", skill)
        self.assertIn("gateway install --system --run-as-user ubuntu", runbook)
        self.assertIn("tradingagents-daily-report", runbook)
        self.assertIn("--deliver local", runbook)
        self.assertIn("hermes cron create", runbook)
        self.assertIn("hermes cron pause", runbook)
        self.assertIn("hermes cron resume", runbook)
        self.assertIn("results/hermes/report_batches", runbook)
        self.assertIn("results/hermes/reports", runbook)

    def test_daily_report_no_agent_wrappers_are_fixed_and_secret_free(self):
        submit = DAILY_REPORT_SUBMIT_SCRIPT.read_text(encoding="ascii")
        archive = DAILY_REPORT_ARCHIVE_SCRIPT.read_text(encoding="ascii")

        self.assertIn("hermes_daily_report_bootstrap submit", submit)
        self.assertIn("hermes_daily_report_bootstrap archive", archive)
        self.assertIn(".venv-hermes-mcp/bin/python", submit)
        self.assertNotIn("hermes ", submit)
        self.assertNotIn("API_KEY", submit + archive)

    def test_daily_report_runbook_uses_paused_no_agent_local_jobs(self):
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn("--no-agent --script", runbook)
        self.assertIn("--script tradingagents-daily-report-submit.sh", runbook)
        self.assertIn("--script tradingagents-daily-report-archive.sh", runbook)
        self.assertNotIn(
            "--script /home/ubuntu/.hermes/scripts/tradingagents-daily-report-submit.sh",
            runbook,
        )
        self.assertNotIn(
            "--script /home/ubuntu/.hermes/scripts/tradingagents-daily-report-archive.sh",
            runbook,
        )
        self.assertIn("tradingagents-daily-report-submit.sh", runbook)
        self.assertIn("tradingagents-daily-report-archive.sh", runbook)
        self.assertIn("hermes cron remove", runbook)
        self.assertNotIn("--skill tradingagents-daily-report", runbook)

    def test_daily_report_runbook_loads_bootstrap_config_and_waits_for_runs(self):
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn("从 `mcp_servers.tradingagents_crypto.env` 加载白名单值", runbook)
        self.assertIn("mcp_servers.tradingagents_crypto.env", runbook)
        self.assertIn("临时历史日期无 agent job", runbook)
        self.assertNotIn(
            "EnvironmentFile=/etc/tradingagents/hermes-gateway.env",
            runbook,
        )
        self.assertIn("run_once_and_pause", runbook)
        self.assertIn('hermes cron runs "$job_id" --limit 1', runbook)
        self.assertLess(
            runbook.index('hermes cron pause "$submit_job_id"'),
            runbook.index('hermes cron remove "$old_submit_job_id"'),
        )
        self.assertLess(
            runbook.index('hermes cron pause "$archive_job_id"'),
            runbook.index('hermes cron remove "$old_archive_job_id"'),
        )
        self.assertLess(
            runbook.index("hermes cron create --name tradingagents-daily-report-submit"),
            runbook.index('hermes cron remove "$old_submit_job_id"'),
        )

    def test_scheduled_review_jobs_keep_memory_writes_agent_owned(self):
        process_script = SCHEDULED_REVIEW_PROCESS_SCRIPT.read_text(encoding="ascii")
        skill = SCHEDULED_REVIEW_SKILL_PATH.read_text(encoding="ascii")
        normalized_skill = " ".join(skill.split())
        runbook = RUNBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn("hermes_scheduled_review_bootstrap process-due", process_script)
        self.assertIn(".venv-hermes-mcp/bin/python", process_script)
        self.assertNotIn("hermes ", process_script)
        self.assertNotIn("API_KEY", process_script)
        self.assertNotIn("MEMORY.md", process_script)

        self.assertIn("memory-pending --limit 18", skill)
        self.assertIn("memory tool", normalized_skill)
        self.assertIn("action=add", skill)
        self.assertIn("Entry added", skill)
        self.assertIn("Entry already exists", skill)
        self.assertIn("confirm-memory", skill)
        self.assertIn("unavailable_count", skill)
        self.assertIn("unavailable_review_ids", skill)
        self.assertIn("Continue with the valid items", skill)
        self.assertIn("Never edit", skill)
        self.assertIn("MEMORY.md", skill)
        self.assertIn("never a real order", normalized_skill)

        self.assertIn("tradingagents-scheduled-review-process", runbook)
        self.assertIn("tradingagents-scheduled-review-memory", runbook)
        self.assertIn("tradingagents-scheduled-paper-reviews", runbook)
        self.assertIn("'15 8 * * *'", runbook)
        self.assertIn("'30 8 * * *'", runbook)
        self.assertIn("--no-agent --script tradingagents-scheduled-review-process.sh", runbook)
        self.assertIn("--skill tradingagents-scheduled-paper-reviews", runbook)
        self.assertIn("不会自动回填旧报告", runbook)
        self.assertIn("不得通过脚本直接修改", runbook)
        self.assertIn("持久保留全部复盘索引项", runbook)
        self.assertIn("最近 5 条", runbook)
        self.assertIn("MEMORY.md", runbook)
        self.assertIn("不会直接修改或写入 `MEMORY.md`", runbook)
        self.assertIn("count-only read-only verification", runbook)
        self.assertIn("marker-occurrence read-only verification", runbook)
        self.assertIn("不会暴露 raw memory text", runbook)
        self.assertNotIn("脚本不会读取、编辑或写入 `MEMORY.md`", runbook)

    def test_scheduled_skill_promotes_legacy_then_report_memory(self):
        skill = SCHEDULED_REVIEW_SKILL_PATH.read_text(encoding="ascii")

        self.assertIn("memory-pending --limit 18", skill)
        self.assertIn("report-reflection-pending --limit 18", skill)
        self.assertIn("report-reflection-evidence", skill)
        self.assertIn("submit_report_reflection", skill)
        self.assertIn("begin-report-memory", skill)
        self.assertIn("action=add", skill)
        self.assertIn("action=replace", skill)
        self.assertIn("confirm-report-memory", skill)
        self.assertIn("quarantine-report-memory", skill)
        self.assertNotIn("memory(action=read", skill)
        self.assertNotIn("edit MEMORY.md", skill)

    def test_scheduled_skill_defers_rejected_reflection_retries(self):
        skill = SCHEDULED_REVIEW_SKILL_PATH.read_text(encoding="ascii")
        reflection = skill[
            skill.index("## 2. Reflect bounded report evidence (v2)") :
            skill.index("## 3. Promote one report memory entry at a time")
        ]

        self.assertIn("one evidence fetch and one submit", reflection)
        self.assertIn("Do not fetch, regenerate, or submit", reflection)
        self.assertIn("same `session_id` and `revision`", reflection)
        self.assertIn("current Agent run", reflection)
        self.assertIn("may have contributed", reflection)
        self.assertIn("is consistent with", reflection)
        self.assertIn("could indicate", reflection)
        self.assertIn("certainty", reflection)
        self.assertIn("real-order", reflection)
        self.assertIn("credential", reflection)
        self.assertIn("prompt-injection", reflection)
        self.assertIn("unsupported external-source", reflection)
        self.assertIn("marker", reflection)
        self.assertIn("delimiter", reflection)

    def test_scheduled_skill_requires_exact_packet_field_evidence_references(self):
        skill = SCHEDULED_REVIEW_SKILL_PATH.read_text(encoding="ascii")
        reflection = skill[
            skill.index("## 2. Reflect bounded report evidence (v2)") :
            skill.index("## 3. Promote one report memory entry at a time")
        ]
        normalized_reflection = " ".join(reflection.split())

        self.assertIn("`causal_hypotheses[].evidence`", reflection)
        self.assertIn("must exist on every hypothesis", normalized_reflection)
        self.assertIn("non-empty list of strings", normalized_reflection)
        self.assertIn("missing, `null`, not a list, or empty", normalized_reflection)
        self.assertIn(
            "exact copy of one `packet.fields[].name`", normalized_reflection
        )
        self.assertIn("case-sensitive", reflection)
        self.assertIn("`excerpt`", reflection)
        self.assertIn("`sha256`", reflection)
        self.assertIn("natural-language description", reflection)
        self.assertIn("alias", reflection)
        self.assertIn("Before submitting", normalized_reflection)
        self.assertIn(
            "reject the generated reflection locally", normalized_reflection
        )
        self.assertIn("`REFLECTION_EVIDENCE_INVALID`", reflection)
        self.assertIn("do not call", normalized_reflection)
        self.assertIn("`submit_report_reflection`", reflection)

    def test_runbook_documents_v2_cutover_and_single_entry_acceptance(self):
        text = RUNBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn("scheduled_review_version: 2", text)
        self.assertIn("report_memories/<session_id>.json", text)
        self.assertIn("T+1 add", text)
        self.assertIn("T+7/T+15 replace", text)
        self.assertIn("旧 v1", text)
        self.assertIn("只有一个 Hermes memory 条目", text)

    def test_runbook_documents_reflection_retry_gate_recovery(self):
        text = RUNBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn("REPORT_REFLECTION_RETRY_DEFERRED", text)
        self.assertIn("同一 UTC 日期最多消耗一次", text)
        self.assertIn("三个不同 UTC 日期", text)
        self.assertIn("保持 `attention_required` artifact 不变", text)
        self.assertIn("新的未使用历史日期", text)
        self.assertIn(
            "不得直接修改 `report_memories/<session_id>.json`", text
        )
        self.assertIn("不得重新运行同一 item", text)

    def test_scheduled_skill_separates_legacy_retry_from_report_quarantine(self):
        skill = SCHEDULED_REVIEW_SKILL_PATH.read_text(encoding="ascii")
        safe_summary = skill[skill.index("## 5. Safety and reporting") :]

        self.assertIn("leave the item in `memory_pending`", skill)
        self.assertNotIn("quarantined by the project bootstrap", skill)
        self.assertIn("REPORT_MEMORY_RESULT_AMBIGUOUS", skill)
        self.assertIn("already persists `attention_required`", skill)
        self.assertIn("Do not call `quarantine-report-memory` after", skill)
        self.assertNotIn("horizon", safe_summary)

    def test_runbook_retires_old_jobs_only_after_v2_acceptance(self):
        text = RUNBOOK_PATH.read_text(encoding="utf-8")
        acceptance = text.index("全部旧 v1 与新 v2 验收通过后")

        self.assertGreater(
            text.index('hermes cron remove "$old_process_job_id"'), acceptance
        )
        self.assertGreater(
            text.index('hermes cron remove "$old_memory_job_id"'), acceptance
        )
        self.assertIn("MEMORY_ERROR_CODES", text)
        self.assertIn("已持久化 `attention_required`", text)
        self.assertIn("不得再次调用 `quarantine-report-memory`", text)
        self.assertIn("恢复旧 v1 job", text)
        self.assertNotIn("SCHEDULED_REVIEW_RUNNER_FAILED", text)

    def test_runbook_pauses_old_jobs_before_install_and_each_replacement_immediately(self):
        text = RUNBOOK_PATH.read_text(encoding="utf-8")
        scheduled = text[text.index("## T+1/T+7/T+15") :]
        self.assertIn('hermes cron pause "$old_process_job_id"', scheduled)
        self.assertIn('hermes cron pause "$old_memory_job_id"', scheduled)
        old_list = scheduled.index("hermes cron list --all")
        old_process_pause = scheduled.index('hermes cron pause "$old_process_job_id"')
        old_memory_pause = scheduled.index('hermes cron pause "$old_memory_job_id"')
        install_wrapper = scheduled.index(
            "install -m 700 deploy/hermes/scripts/tradingagents-scheduled-review-process.sh"
        )
        create_process = scheduled.index(
            "hermes cron create --name tradingagents-scheduled-review-process"
        )
        pause_process = scheduled.index(
            'hermes cron pause "$scheduled_review_process_job_id"'
        )
        create_memory = scheduled.index(
            "hermes cron create --name tradingagents-scheduled-review-memory"
        )
        pause_memory = scheduled.index(
            'hermes cron pause "$scheduled_review_memory_job_id"'
        )

        self.assertLess(old_list, old_process_pause)
        self.assertLess(old_process_pause, old_memory_pause)
        self.assertLess(old_memory_pause, install_wrapper)
        self.assertLess(create_process, pause_process)
        self.assertLess(pause_process, create_memory)
        self.assertLess(create_memory, pause_memory)

    def test_runbook_executes_all_v2_horizons_before_retiring_old_jobs(self):
        text = RUNBOOK_PATH.read_text(encoding="utf-8")
        scheduled = text[text.index("## T+1/T+7/T+15") :]
        self.assertIn(
            'run_scheduled_job_once_and_pause "$scheduled_review_process_job_id"',
            scheduled,
        )
        self.assertIn("#### T+1 add acceptance", scheduled)
        self.assertIn("#### T+7 replace acceptance", scheduled)
        self.assertIn("#### T+15 replace acceptance", scheduled)
        processor = scheduled.index(
            'run_scheduled_job_once_and_pause "$scheduled_review_process_job_id"'
        )
        t1 = scheduled.index("#### T+1 add acceptance")
        t7 = scheduled.index("#### T+7 replace acceptance")
        t15 = scheduled.index("#### T+15 replace acceptance")
        retire = scheduled.index('hermes cron remove "$old_process_job_id"')

        self.assertLess(processor, t1)
        self.assertLess(t1, t7)
        self.assertLess(t7, t15)
        self.assertLess(t15, retire)
        self.assertEqual(scheduled.count("process-due --current-utc-date <T+"), 3)
        self.assertEqual(
            scheduled.count(
                'run_scheduled_job_once_and_pause "$scheduled_review_memory_job_id" --accept-hooks'
            ),
            3,
        )
        for start, end in ((t1, t7), (t7, t15), (t15, retire)):
            stage = scheduled[start:end]
            self.assertIn("confirm-report-memory", stage)
            self.assertIn("marker_occurrences: 1", stage)
            self.assertIn("index_matches_latest_reflection: true", stage)

    def test_runbook_smokes_processor_before_enrolling_acceptance_report(self):
        text = RUNBOOK_PATH.read_text(encoding="utf-8")
        scheduled = text[text.index("## T+1/T+7/T+15") :]
        smoke = scheduled.index(
            'run_scheduled_job_once_and_pause "$scheduled_review_process_job_id"'
        )
        self.assertIn("#### Create and archive v2 acceptance report", scheduled)
        enrollment = scheduled.index("#### Create and archive v2 acceptance report")
        submit = scheduled.index(
            'hermes_daily_report_bootstrap submit --trade-date "$ACCEPTANCE_TRADE_DATE"'
        )
        archive = scheduled.index(
            'hermes_daily_report_bootstrap archive --trade-date "$ACCEPTANCE_TRADE_DATE"'
        )
        t1 = scheduled.index("#### T+1 add acceptance")

        self.assertLess(smoke, enrollment)
        self.assertLess(enrollment, submit)
        self.assertLess(submit, archive)
        self.assertLess(archive, t1)
        self.assertIn('scheduled_review_version") == 2', scheduled[archive:t1])
        self.assertIn('workflow_version") == 2', scheduled[archive:t1])
        self.assertIn('len(schedule["items"]) == 9', scheduled[archive:t1])

    def test_runbook_guards_acceptance_date_before_submit(self):
        text = RUNBOOK_PATH.read_text(encoding="utf-8")
        scheduled = text[text.index("#### Create and archive v2 acceptance report") :]
        self.assertIn("validate_acceptance_trade_date()", scheduled)
        definition = scheduled.index("validate_acceptance_trade_date()")
        invocation = scheduled.index(
            'validate_acceptance_trade_date "$ACCEPTANCE_TRADE_DATE"'
        )
        submit = scheduled.index(
            'hermes_daily_report_bootstrap submit --trade-date "$ACCEPTANCE_TRADE_DATE"'
        )
        self.assertLess(definition, invocation)
        self.assertLess(invocation, submit)

        cases = (
            ("noncanonical", "20260804", None, 1, "ISO"),
            ("today-minus-15", "2026-08-05", None, 1, "fully elapsed T+15"),
            ("today-minus-16", "2026-08-04", None, 0, "acceptance date ready"),
            ("batch-exists", "2026-08-04", "report_batches", 1, "already has a report batch"),
            ("schedule-exists", "2026-08-04", "review_schedules", 1, "already has a review schedule"),
        )
        script = scheduled_acceptance_guard_script()
        for name, trade_date, occupied_dir, expected_code, expected_message in cases:
            with self.subTest(name=name), TemporaryDirectory() as directory:
                results_root = Path(directory) / "results"
                if occupied_dir is not None:
                    occupied = results_root / "hermes" / occupied_dir
                    occupied.mkdir(parents=True)
                    (occupied / f"{trade_date}.json").write_text("{}", encoding="ascii")
                environment = os.environ.copy()
                environment.update(
                    {
                        "TRADINGAGENTS_ACCEPTANCE_GUARD_TESTING": "1",
                        "TRADINGAGENTS_ACCEPTANCE_TODAY_UTC": "2026-08-20",
                        "TRADINGAGENTS_ACCEPTANCE_RESULTS_DIR": str(results_root),
                    }
                )
                result = subprocess.run(
                    [sys.executable, "-", trade_date],
                    input=script,
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                )

            self.assertEqual(result.returncode, expected_code)
            self.assertIn(expected_message, result.stdout + result.stderr)

    def test_scheduled_skill_requires_nested_mcp_success_and_state_aware_restart(self):
        skill = SCHEDULED_REVIEW_SKILL_PATH.read_text(encoding="ascii")
        memory_started = skill[
            skill.index("- For `add_pending`") : skill.index("- For `verification_pending`")
        ]
        verification_pending = skill[
            skill.index("- For `verification_pending`") : skill.index("For an add")
        ]

        self.assertIn("response `ok` is exactly `true`", skill)
        self.assertIn('`data.reflection_state` is exactly `"ready"`', skill)
        self.assertIn(
            '`data.memory_state` is either `"add_pending"` or `"replace_pending"`',
            skill,
        )
        self.assertIn("Missing or unknown response nesting is failure", skill)
        self.assertIn("idempotent", memory_started)
        self.assertIn("exactly once", memory_started)
        self.assertIn("does not return `content` or `old_text`", verification_pending)
        self.assertIn("do not call `begin-report-memory` again", verification_pending)
        self.assertNotIn("memory(action=", verification_pending)
        self.assertLess(
            skill.index("memory-pending --limit 18"),
            skill.index("report-reflection-pending --limit 18"),
        )
        self.assertLess(
            skill.index("report-reflection-pending --limit 18"),
            skill.index("report-memory-pending --limit 18"),
        )

    def test_scheduled_skill_uses_agent_owned_completed_report_retirement(self):
        skill = SCHEDULED_REVIEW_SKILL_PATH.read_text(encoding="ascii")
        self.assertIn("## 4. Retire bounded completed report memory", skill)
        retirement = skill[skill.index("## 4. Retire bounded completed report memory") :]
        verification_pending = retirement[
            retirement.index("For `verification_pending`") : retirement.index(
                "## 5. Safety and reporting"
            )
        ]
        normalized_verification_pending = " ".join(verification_pending.split())

        self.assertIn("report-memory-retirement-pending --limit 18", retirement)
        self.assertIn("begin-report-memory-retirement", retirement)
        self.assertIn(
            "memory(action=remove,target=memory,old_text=<returned marker>)",
            retirement,
        )
        self.assertIn("Entry removed", retirement)
        self.assertIn("confirm-report-memory-retirement", retirement)
        self.assertIn("quarantine-report-memory-retirement", retirement)
        self.assertIn("MEMORY_RESULT_AMBIGUOUS", retirement)
        self.assertIn("MEMORY_REMOVE_FAILED", retirement)
        self.assertIn("MEMORY_VERIFICATION_FAILED", retirement)
        self.assertIn("retries only confirmation", verification_pending)
        self.assertIn(
            "Do not call `begin-report-memory-retirement`",
            normalized_verification_pending,
        )
        self.assertIn(
            "do not call the Hermes memory remove tool again",
            normalized_verification_pending,
        )
        self.assertNotIn("memory(action=read", skill)
        self.assertLess(
            skill.index("report-memory-pending --limit 18"),
            skill.index("report-memory-retirement-pending --limit 18"),
        )

    def test_runbook_documents_bounded_report_memory_capacity_and_retention(self):
        text = RUNBOOK_PATH.read_text(encoding="utf-8")

        self.assertIn("memory:\n  memory_char_limit: 40000", text)
        self.assertIn("report-memory-capacity", text)
        self.assertIn("--memory-char-limit 40000", text)
        self.assertIn("current_chars <= 9000", text)
        self.assertIn("reserved_report_chars == 30897", text)
        self.assertIn("safe/no-memory-text", text)
        self.assertIn("T+15", text)
        self.assertIn("never retired", text)
        self.assertIn("latest 5 final completed reports per symbol", text)
        self.assertIn("report records, immutable review, and learning index remain permanent", text)
        self.assertIn("six completed reports for one symbol", text)
        self.assertIn("only oldest final marker", text)
        self.assertIn("attention_required", text)
        self.assertNotIn("memory(action=read", text)


if __name__ == "__main__":
    unittest.main()
