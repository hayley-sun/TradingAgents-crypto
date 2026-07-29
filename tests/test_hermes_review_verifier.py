import io
import json
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


if __name__ == "__main__":
    unittest.main()
