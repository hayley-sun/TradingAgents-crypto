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


if __name__ == "__main__":
    unittest.main()
