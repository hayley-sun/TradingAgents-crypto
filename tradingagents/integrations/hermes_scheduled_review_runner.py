"""Safe deterministic commands for scheduled Hermes paper reviews."""

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from tradingagents.integrations.hermes_learning import ReviewStore
from tradingagents.integrations.hermes_mcp import (
    PROJECT_ROOT,
    SessionStore,
    review_paper_decision_impl,
)
from tradingagents.integrations.hermes_report_learning import (
    ReportLearningStore,
    record_review_fact,
)
from tradingagents.integrations.hermes_review_verifier import verify_review_consistency
from tradingagents.integrations.hermes_scheduled_reviews import (
    MAX_MEMORY_ITEMS,
    ScheduledReviewProcessReport,
    ScheduledReviewStore,
    confirm_scheduled_memory,
    inspect_pending_memory,
    process_due_reviews,
)
from tradingagents.integrations.schemas import is_valid_review_id


DEFAULT_MEMORY_LIMIT = MAX_MEMORY_ITEMS


def _results_root() -> Path:
    configured = os.getenv("TRADINGAGENTS_RESULTS_DIR")
    return (Path(configured) if configured else PROJECT_ROOT / "results").expanduser().resolve()


def run_process_due(
    current_utc_date: date,
    processor: Callable[[date], ScheduledReviewProcessReport] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Process due project reviews without invoking a Hermes Agent."""
    if processor is None:
        store = ScheduledReviewStore.from_environment()
        session_store = SessionStore.from_environment()
        review_store = ReviewStore.from_environment()
        report_store = ReportLearningStore.from_environment()

        def active_processor(as_of_date: date) -> ScheduledReviewProcessReport:
            return process_due_reviews(
                store,
                as_of_date,
                lambda session_id, review_date, workflow_version: review_paper_decision_impl(
                    {
                        "session_id": session_id,
                        "review_date": review_date.isoformat(),
                    },
                    store=session_store,
                    review_store=review_store,
                    current_date=as_of_date,
                    write_legacy_learning=(workflow_version == 1),
                ),
                fact_recorder=lambda review: record_review_fact(
                    report_store,
                    session_store.load(review.session_id),
                    review,
                ),
            )

        processor = active_processor
    report = processor(current_utc_date)
    return 0, {
        "ok": True,
        "mode": "process-due",
        "current_utc_date": current_utc_date.isoformat(),
        "due_count": report.due_count,
        "reviewed_count": report.reviewed_count,
        "retryable_count": report.retryable_count,
        "skipped_count": report.skipped_count,
        "attention_required_count": report.attention_required_count,
        "report_fact_count": getattr(report, "report_fact_count", 0),
    }


def run_memory_pending(
    limit: int,
    lister: Callable[[int], list[Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """List exact canonical lessons for the Hermes memory tool."""
    if lister is None:
        store = ScheduledReviewStore.from_environment()
        review_store = ReviewStore.from_environment()
        lister = lambda selected_limit: inspect_pending_memory(
            store, review_store.load, selected_limit
        )
    listing = lister(limit)
    if isinstance(listing, list):
        work = listing
        unavailable_count = 0
        unavailable_review_ids = []
    else:
        work = list(listing.items)
        unavailable_count = listing.unavailable_count
        unavailable_review_ids = list(
            listing.unavailable_review_ids[:MAX_MEMORY_ITEMS]
        )
    return 0, {
        "ok": True,
        "mode": "memory-pending",
        "count": len(work),
        "unavailable_count": unavailable_count,
        "unavailable_review_ids": unavailable_review_ids,
        "items": [
            {
                "trade_date": item.trade_date.isoformat(),
                "review_date": item.review_date.isoformat(),
                "symbol": item.symbol,
                "horizon_days": item.horizon_days,
                "review_id": item.review_id,
                "hermes_memory_entry": item.hermes_memory_entry,
            }
            for item in work
        ],
    }


def run_confirm_memory(
    review_id: str,
    memory_path: Path,
    confirmer: Callable[[str, Path], Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Verify three-store consistency and confirm project schedule state."""
    if confirmer is None:
        store = ScheduledReviewStore.from_environment()
        results_root = _results_root()

        def active_confirmer(selected_review_id: str, selected_memory_path: Path):
            return confirm_scheduled_memory(
                store,
                selected_review_id,
                lambda candidate: verify_review_consistency(
                    candidate, results_root, selected_memory_path
                ),
            )

        confirmer = active_confirmer
    item = confirmer(review_id, memory_path)
    return 0, {
        "ok": True,
        "mode": "confirm-memory",
        "review_id": review_id,
        "state": item.state,
    }


def _error(code: str, mode: str) -> dict[str, Any]:
    return {
        "ok": False,
        "mode": mode,
        "error": {
            "code": code,
            "message": "The scheduled paper-review command could not complete.",
            "suggested_action": "Inspect the safe Cron result and retry or investigate the item.",
        },
    }


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parse_date(value: str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("invalid current UTC date")
    return parsed


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    mode = arguments[0] if arguments else "unknown"
    parser = _SafeArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    process_parser = subparsers.add_parser("process-due", add_help=False)
    process_parser.add_argument("--current-utc-date")
    pending_parser = subparsers.add_parser("memory-pending", add_help=False)
    pending_parser.add_argument("--limit", type=int, default=DEFAULT_MEMORY_LIMIT)
    confirm_parser = subparsers.add_parser("confirm-memory", add_help=False)
    confirm_parser.add_argument("--review-id", required=True)
    confirm_parser.add_argument(
        "--hermes-memory-path",
        type=Path,
        default=Path.home() / ".hermes" / "memories" / "MEMORY.md",
    )
    try:
        parsed = parser.parse_args(arguments)
        if parsed.mode == "process-due":
            code, payload = run_process_due(_parse_date(parsed.current_utc_date))
        elif parsed.mode == "memory-pending":
            if not 1 <= parsed.limit <= MAX_MEMORY_ITEMS:
                raise ValueError("invalid memory limit")
            code, payload = run_memory_pending(parsed.limit)
        else:
            if not is_valid_review_id(parsed.review_id):
                raise ValueError("invalid review id")
            code, payload = run_confirm_memory(
                parsed.review_id, parsed.hermes_memory_path.expanduser().resolve()
            )
    except (TypeError, ValueError):
        code, payload = 1, _error("INVALID_SCHEDULED_REVIEW_REQUEST", mode)
    except Exception:
        code, payload = 1, _error("SCHEDULED_REVIEW_RUNNER_FAILED", mode)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
