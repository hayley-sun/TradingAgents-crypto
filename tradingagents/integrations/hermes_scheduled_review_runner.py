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
    build_evidence_packet,
    record_review_fact,
)
from tradingagents.integrations.hermes_report_memory import (
    MEMORY_ERROR_CODES,
    begin_report_memory,
    confirm_report_memory,
    list_pending_report_memory,
    quarantine_report_memory,
)
from tradingagents.integrations.hermes_report_memory_verifier import (
    verify_report_memory_consistency,
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
from tradingagents.integrations.schemas import is_valid_review_id, is_valid_session_id


DEFAULT_MEMORY_LIMIT = MAX_MEMORY_ITEMS
MAX_REPORT_ITEMS = 18


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


def run_report_reflection_pending(
    limit: int,
    lister: Callable[[int], list[Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """List bounded report-reflection work as metadata without source content."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_REPORT_ITEMS:
        return 1, _error("INVALID_SCHEDULED_REVIEW_REQUEST", "report-reflection-pending")
    if lister is None:
        store = ReportLearningStore.from_environment()
        lister = lambda selected_limit: store.records()
    records = list(lister(limit))
    candidates = sorted(
        (
            (record, revision)
            for record in records
            for revision in record.revisions
            if revision.reflection_state == "pending"
        ),
        key=lambda item: (
            item[1].created_at,
            item[0].trade_date,
            item[0].symbol,
            item[0].session_id,
            item[1].revision,
        ),
    )
    next_revisions = {
        record.session_id: record.reflected_revision + 1 for record in records
    }
    items: list[dict[str, Any]] = []
    remaining = candidates
    while remaining and len(items) < limit:
        deferred = []
        progressed = False
        for record, revision in remaining:
            if len(items) >= limit:
                deferred.append((record, revision))
                continue
            if revision.revision != next_revisions[record.session_id]:
                deferred.append((record, revision))
                continue
            maturity_days = record.outcomes[revision.revision - 1].horizon_days
            items.append(
                {
                    "session_id": record.session_id,
                    "symbol": record.symbol,
                    "trade_date": record.trade_date.isoformat(),
                    "revision": revision.revision,
                    "maturity_days": maturity_days,
                }
            )
            next_revisions[record.session_id] += 1
            progressed = True
        if not progressed:
            break
        remaining = deferred
    return 0, {
        "ok": True,
        "mode": "report-reflection-pending",
        "count": len(items),
        "items": items,
    }


def run_report_reflection_evidence(
    session_id: str,
    revision: int,
    loader: Callable[[], Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Return exactly one bounded evidence packet for a completed report revision."""
    if (
        not is_valid_session_id(session_id)
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or not 1 <= revision <= 3
    ):
        return 1, _error("INVALID_SCHEDULED_REVIEW_REQUEST", "report-reflection-evidence")
    if loader is not None:
        loaded = loader()
        session, record = loaded
    else:
        session_store = SessionStore.from_environment()
        report_store = ReportLearningStore.from_environment()
        session = session_store.load(session_id)
        if session is None:
            raise LookupError("session not found")
        record = report_store.load(session_id)
        if record is None:
            raise LookupError("report learning record not found")
    packet = build_evidence_packet(record, session, revision)
    return 0, {
        "ok": True,
        "mode": "report-reflection-evidence",
        "packet": packet.model_dump(mode="json"),
    }


def run_report_memory_pending(
    limit: int,
    lister: Callable[[int], list[Any]] | None = None,
) -> tuple[int, dict[str, Any]]:
    """List ordered report-memory metadata without exposing memory content."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_REPORT_ITEMS:
        return 1, _error("INVALID_SCHEDULED_REVIEW_REQUEST", "report-memory-pending")
    if lister is None:
        store = ReportLearningStore.from_environment()
        lister = lambda selected_limit: list_pending_report_memory(store, selected_limit)
    work = list(lister(limit))[:limit]
    return 0, {
        "ok": True,
        "mode": "report-memory-pending",
        "count": len(work),
        "items": [
            {
                "session_id": item.session_id,
                "symbol": item.symbol,
                "trade_date": item.trade_date.isoformat(),
                "revision": item.revision,
                "maturity_days": item.maturity_days,
                "action": item.action,
                "memory_state": item.memory_state,
            }
            for item in work
        ],
    }


def run_begin_report_memory(
    session_id: str,
    revision: int,
    starter: Callable[[str, int], Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Return the sole runner payload that includes exact memory operation text."""
    if not is_valid_session_id(session_id) or isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 3:
        return 1, _error("INVALID_SCHEDULED_REVIEW_REQUEST", "begin-report-memory")
    if starter is None:
        store = ReportLearningStore.from_environment()
        starter = lambda selected_session, selected_revision: begin_report_memory(store, selected_session, selected_revision)
    operation = starter(session_id, revision)
    return 0, {
        "ok": True,
        "mode": "begin-report-memory",
        "session_id": operation.session_id,
        "symbol": operation.symbol,
        "trade_date": operation.trade_date.isoformat(),
        "revision": operation.revision,
        "maturity_days": operation.maturity_days,
        "action": operation.action,
        **(
            {
                "content": operation.content,
                "old_text": operation.old_text,
            }
            if operation.memory_state != "verification_pending"
            else {}
        ),
        "memory_state": operation.memory_state,
    }


def run_confirm_report_memory(
    session_id: str,
    revision: int,
    memory_path: Path,
    confirmer: Callable[[str, int], Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Verify Hermes memory read-only, then expose project state only."""
    if not is_valid_session_id(session_id) or isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 3:
        return 1, _error("INVALID_SCHEDULED_REVIEW_REQUEST", "confirm-report-memory")
    if confirmer is None:
        store = ReportLearningStore.from_environment()
        results_root = _results_root()
        confirmer = lambda selected_session, selected_revision: confirm_report_memory(
            store,
            selected_session,
            selected_revision,
            verifier=lambda *_args: verify_report_memory_consistency(
                selected_session, selected_revision, results_root, memory_path
            ),
        )
    record = confirmer(session_id, revision)
    snapshot = record.revisions[revision - 1]
    return 0, {
        "ok": True,
        "mode": "confirm-report-memory",
        "session_id": session_id,
        "revision": revision,
        "confirmed_revision": record.confirmed_revision,
        "memory_state": snapshot.memory_state,
    }


def run_quarantine_report_memory(
    session_id: str,
    revision: int,
    error_code: str,
    quarantiner: Callable[[str, int, str], Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Quarantine a report-memory operation using an allowlisted code."""
    if not is_valid_session_id(session_id) or isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 3:
        return 1, _error("INVALID_SCHEDULED_REVIEW_REQUEST", "quarantine-report-memory")
    if not isinstance(error_code, str) or error_code not in MEMORY_ERROR_CODES:
        return 1, _error("INVALID_SCHEDULED_REVIEW_REQUEST", "quarantine-report-memory")
    if quarantiner is None:
        store = ReportLearningStore.from_environment()
        quarantiner = lambda selected_session, selected_revision, selected_code: quarantine_report_memory(
            store, selected_session, selected_revision, selected_code
        )
    record = quarantiner(session_id, revision, error_code)
    return 0, {
        "ok": True,
        "mode": "quarantine-report-memory",
        "session_id": session_id,
        "revision": revision,
        "memory_state": record.revisions[revision - 1].memory_state,
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
    report_pending_parser = subparsers.add_parser("report-reflection-pending", add_help=False)
    report_pending_parser.add_argument("--limit", type=int, default=MAX_REPORT_ITEMS)
    report_evidence_parser = subparsers.add_parser("report-reflection-evidence", add_help=False)
    report_evidence_parser.add_argument("--session-id", required=True)
    report_evidence_parser.add_argument("--revision", type=int, required=True)
    report_memory_pending_parser = subparsers.add_parser("report-memory-pending", add_help=False)
    report_memory_pending_parser.add_argument("--limit", type=int, default=MAX_REPORT_ITEMS)
    begin_report_memory_parser = subparsers.add_parser("begin-report-memory", add_help=False)
    begin_report_memory_parser.add_argument("--session-id", required=True)
    begin_report_memory_parser.add_argument("--revision", type=int, required=True)
    confirm_report_memory_parser = subparsers.add_parser("confirm-report-memory", add_help=False)
    confirm_report_memory_parser.add_argument("--session-id", required=True)
    confirm_report_memory_parser.add_argument("--revision", type=int, required=True)
    confirm_report_memory_parser.add_argument(
        "--hermes-memory-path",
        type=Path,
        default=Path.home() / ".hermes" / "memories" / "MEMORY.md",
    )
    quarantine_report_memory_parser = subparsers.add_parser("quarantine-report-memory", add_help=False)
    quarantine_report_memory_parser.add_argument("--session-id", required=True)
    quarantine_report_memory_parser.add_argument("--revision", type=int, required=True)
    quarantine_report_memory_parser.add_argument("--error-code", required=True)
    try:
        parsed = parser.parse_args(arguments)
        if parsed.mode == "process-due":
            code, payload = run_process_due(_parse_date(parsed.current_utc_date))
        elif parsed.mode == "memory-pending":
            if not 1 <= parsed.limit <= MAX_MEMORY_ITEMS:
                raise ValueError("invalid memory limit")
            code, payload = run_memory_pending(parsed.limit)
        elif parsed.mode == "confirm-memory":
            if not is_valid_review_id(parsed.review_id):
                raise ValueError("invalid review id")
            code, payload = run_confirm_memory(
                parsed.review_id, parsed.hermes_memory_path.expanduser().resolve()
            )
        elif parsed.mode == "report-reflection-pending":
            if not 1 <= parsed.limit <= MAX_REPORT_ITEMS:
                raise ValueError("invalid report reflection limit")
            code, payload = run_report_reflection_pending(parsed.limit)
        elif parsed.mode == "report-reflection-evidence":
            if not is_valid_session_id(parsed.session_id) or not 1 <= parsed.revision <= 3:
                raise ValueError("invalid report reflection evidence request")
            code, payload = run_report_reflection_evidence(parsed.session_id, parsed.revision)
        elif parsed.mode == "report-memory-pending":
            if not 1 <= parsed.limit <= MAX_REPORT_ITEMS:
                raise ValueError("invalid report memory limit")
            code, payload = run_report_memory_pending(parsed.limit)
        elif parsed.mode == "begin-report-memory":
            if not is_valid_session_id(parsed.session_id) or not 1 <= parsed.revision <= 3:
                raise ValueError("invalid report memory request")
            code, payload = run_begin_report_memory(parsed.session_id, parsed.revision)
        elif parsed.mode == "confirm-report-memory":
            if not is_valid_session_id(parsed.session_id) or not 1 <= parsed.revision <= 3:
                raise ValueError("invalid report memory request")
            code, payload = run_confirm_report_memory(
                parsed.session_id, parsed.revision, parsed.hermes_memory_path.expanduser().resolve()
            )
        elif parsed.mode == "quarantine-report-memory":
            if not is_valid_session_id(parsed.session_id) or not 1 <= parsed.revision <= 3:
                raise ValueError("invalid report memory request")
            code, payload = run_quarantine_report_memory(parsed.session_id, parsed.revision, parsed.error_code)
        else:
            raise ValueError("invalid scheduled review mode")
    except (TypeError, ValueError):
        code, payload = 1, _error("INVALID_SCHEDULED_REVIEW_REQUEST", mode)
    except Exception:
        code, payload = 1, _error("SCHEDULED_REVIEW_RUNNER_FAILED", mode)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
