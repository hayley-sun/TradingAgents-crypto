"""Read-only consistency checks for Hermes paper-decision reviews."""

import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from tradingagents.integrations.hermes_learning import LearningStore, ReviewStore
from tradingagents.integrations.schemas import is_valid_review_id


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ReviewVerificationError(RuntimeError):
    """Raised when a review, learning index, and Hermes memory disagree."""


@dataclass(frozen=True)
class ReviewVerification:
    """Safe, read-only state for one paper-decision review."""

    review_id: str
    review_exists: bool
    learning_index_contains_review: bool
    hermes_memory_occurrences: int


def verify_review_consistency(
    review_id: str, results_root: Path, hermes_memory_path: Path
) -> ReviewVerification:
    """Require a saved review, indexed lesson, and exactly one memory entry."""
    if not is_valid_review_id(review_id):
        raise ReviewVerificationError("review consistency check failed")

    root = Path(results_root).expanduser().resolve()
    memory_path = Path(hermes_memory_path).expanduser().resolve()
    try:
        review = ReviewStore(root / "hermes" / "reviews").load(review_id)
        if review is None:
            raise ReviewVerificationError("review consistency check failed")
        learning_index = LearningStore(root / "hermes" / "memories").load(
            review.symbol
        )
        memory_text = memory_path.read_text(encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
        raise ReviewVerificationError("review consistency check failed") from error

    matching_entries = (
        [entry for entry in learning_index.entries if entry.review_id == review_id]
        if learning_index is not None
        else []
    )
    learning_index_contains_review = len(matching_entries) == 1 and (
        matching_entries[0].review_date == review.review_date
        and matching_entries[0].lesson == review.hermes_memory_entry
    )
    hermes_memory_occurrences = memory_text.count(review.hermes_memory_entry)
    verification = ReviewVerification(
        review_id=review_id,
        review_exists=True,
        learning_index_contains_review=learning_index_contains_review,
        hermes_memory_occurrences=hermes_memory_occurrences,
    )
    if not learning_index_contains_review or hermes_memory_occurrences != 1:
        raise ReviewVerificationError("review consistency check failed")
    return verification


def _default_results_dir() -> Path:
    configured = os.getenv("TRADINGAGENTS_RESULTS_DIR")
    return Path(configured) if configured else PROJECT_ROOT / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a Hermes paper-decision review without changing any state."
    )
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--results-dir", type=Path, default=_default_results_dir())
    parser.add_argument(
        "--hermes-memory-path",
        type=Path,
        default=Path.home() / ".hermes" / "memories" / "MEMORY.md",
    )
    arguments = parser.parse_args(argv)

    try:
        result = verify_review_consistency(
            arguments.review_id,
            arguments.results_dir,
            arguments.hermes_memory_path,
        )
    except ReviewVerificationError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "review_id": (
                        arguments.review_id
                        if is_valid_review_id(arguments.review_id)
                        else None
                    ),
                }
            )
        )
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "review_id": result.review_id,
                "review_exists": result.review_exists,
                "learning_index_contains_review": result.learning_index_contains_review,
                "hermes_memory_occurrences": result.hermes_memory_occurrences,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
