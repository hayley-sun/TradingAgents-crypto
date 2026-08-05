"""Ordered promotion of reflected report lessons through Hermes memory."""

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Literal

from tradingagents.integrations.hermes_report_learning import ReportLearningStore
from tradingagents.integrations.hermes_report_memory_verifier import (
    verify_report_memory_consistency,
)
from tradingagents.integrations.schemas import ReportLearningRecord, ReportLearningRevision, utc_now


REPORT_MEMORY_MARKER = "[TradingAgents paper report: {session_id}]"
MEMORY_ERROR_CODES = frozenset(
    {
        "REPORT_MEMORY_MARKER_MISSING",
        "REPORT_MEMORY_MARKER_AMBIGUOUS",
        "REPORT_MEMORY_CONTENT_MISMATCH",
        "REPORT_MEMORY_INDEX_MISSING",
        "REPORT_MEMORY_INDEX_MISMATCH",
        "REPORT_MEMORY_RESULT_AMBIGUOUS",
        "REPORT_MEMORY_VERIFICATION_FAILED",
        "MEMORY_MARKER_COUNT_INVALID",
        "MEMORY_MARKER_MISSING",
        "MEMORY_MARKER_DUPLICATE",
        "MEMORY_MARKER_DUPLICATE",
        "MEMORY_CONTENT_MISMATCH",
        "MEMORY_INDEX_MISSING",
        "MEMORY_INDEX_MISMATCH",
        "MEMORY_RESULT_AMBIGUOUS",
        "MEMORY_VERIFICATION_FAILED",
        "MEMORY_PATH_UNREADABLE",
    }
)


@dataclass(frozen=True)
class ReportMemoryWork:
    session_id: str
    symbol: str
    trade_date: date
    revision: int
    maturity_days: int
    action: Literal["add", "replace"]


@dataclass(frozen=True)
class ReportMemoryOperation(ReportMemoryWork):
    content: str
    old_text: str | None


def _work(record: ReportLearningRecord, revision: ReportLearningRevision) -> ReportMemoryWork:
    return ReportMemoryWork(
        session_id=record.session_id,
        symbol=record.symbol,
        trade_date=record.trade_date,
        revision=revision.revision,
        maturity_days=record.outcomes[revision.revision - 1].horizon_days,
        action="add" if revision.revision == 1 else "replace",
    )


def _validate_revision(revision: int) -> None:
    if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= 3:
        raise ValueError("invalid report memory revision")


def _earliest(record: ReportLearningRecord) -> ReportLearningRevision | None:
    number = record.confirmed_revision + 1
    if number > record.reflected_revision:
        return None
    revision = record.revisions[number - 1]
    if revision.reflection_state != "ready":
        return None
    if revision.memory_state not in {"add_pending", "replace_pending", "memory_call_started"}:
        return None
    return revision


def list_pending_report_memory(
    store: ReportLearningStore, limit: int = 18
) -> list[ReportMemoryWork]:
    """List at most one, earliest unconfirmed promotion per report."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 18:
        raise ValueError("invalid report memory limit")
    items: list[ReportMemoryWork] = []
    records_with_work = []
    for record in store.records():
        revision = _earliest(record)
        if revision is not None:
            records_with_work.append((record, revision))
    for record, revision in sorted(
        records_with_work,
        key=lambda pair: (
            pair[1].created_at,
            pair[0].trade_date,
            pair[0].symbol,
            pair[0].session_id,
        ),
    )[:limit]:
        items.append(_work(record, revision))
    return items


def begin_report_memory(
    store: ReportLearningStore, session_id: str, revision: int
) -> ReportMemoryOperation:
    """Claim one ordered promotion, returning the exact Hermes operation payload."""
    _validate_revision(revision)
    with store.locked():
        record = store.load(session_id)
        if record is None:
            raise ValueError("report memory record unavailable")
        if revision > len(record.revisions) or revision != record.confirmed_revision + 1:
            raise ValueError("report memory revision is not next")
        snapshot = record.revisions[revision - 1]
        if snapshot.reflection_state != "ready":
            raise ValueError("report memory revision is not ready")
        if snapshot.memory_state == "memory_call_started":
            return _operation(record, snapshot)
        expected = "add_pending" if revision == 1 else "replace_pending"
        if snapshot.memory_state != expected:
            raise ValueError("report memory revision is not pending")
        started = snapshot.model_copy(
            update={"memory_state": "memory_call_started", "updated_at": utc_now()}
        )
        updated = record.model_copy(
            update={"revisions": [*record.revisions[: revision - 1], started, *record.revisions[revision:]]}
        )
        store._save_unlocked(updated)
        record = updated
        snapshot = started
    return _operation(record, snapshot)


def _operation(record: ReportLearningRecord, revision: ReportLearningRevision) -> ReportMemoryOperation:
    work = _work(record, revision)
    return ReportMemoryOperation(
        **work.__dict__,
        content=revision.hermes_memory_entry or "",
        old_text=None if work.action == "add" else REPORT_MEMORY_MARKER.format(session_id=record.session_id),
    )


def confirm_report_memory(
    store: ReportLearningStore,
    session_id: str,
    revision: int,
    verifier: Callable[..., object] | None = None,
) -> ReportLearningRecord:
    """Verify an applied Hermes operation and advance the durable confirmation cursor."""
    _validate_revision(revision)
    with store.locked():
        record = store.load(session_id)
        if record is None or revision > len(record.revisions) or revision != record.confirmed_revision + 1:
            raise ValueError("report memory confirmation is stale")
        snapshot = record.revisions[revision - 1]
        if snapshot.memory_state == "verification_pending":
            pending = record
        elif snapshot.memory_state == "memory_call_started":
            pending_snapshot = snapshot.model_copy(
                update={"memory_state": "verification_pending", "updated_at": utc_now()}
            )
            pending = record.model_copy(
                update={
                    "revisions": [
                        *record.revisions[: revision - 1],
                        pending_snapshot,
                        *record.revisions[revision:],
                    ]
                }
            )
            store._save_unlocked(pending)
        else:
            raise ValueError("report memory operation was not started")

    verification_ok = False
    verification_error_code = "MEMORY_VERIFICATION_FAILED"
    try:
        if verifier is None:
            results_root = _results_root()
            memory_path = Path.home() / ".hermes" / "memories" / "MEMORY.md"
            result = verify_report_memory_consistency(session_id, revision, results_root, memory_path)
        else:
            result = verifier(session_id, revision)
        if isinstance(result, bool):
            verification_ok = result
        else:
            verification_ok = getattr(result, "ok", False) is True
        if not verification_ok:
            if getattr(result, "marker_occurrences", 1) == 0:
                verification_error_code = "MEMORY_MARKER_MISSING"
            elif getattr(result, "marker_occurrences", 1) != 1:
                verification_error_code = "MEMORY_MARKER_DUPLICATE"
            elif getattr(result, "exact_content_occurrences", 1) != 1:
                verification_error_code = "MEMORY_CONTENT_MISMATCH"
            elif not getattr(result, "index_matches_latest_reflection", True):
                verification_error_code = "MEMORY_INDEX_MISMATCH"
            verification_error_code = getattr(result, "error_code", verification_error_code)
    except Exception:
        verification_ok = False

    with store.locked():
        current = store.load(session_id)
        if current is None:
            raise ValueError("report memory record unavailable")
        snapshot = current.revisions[revision - 1]
        if current.confirmed_revision >= revision and snapshot.memory_state == "confirmed":
            return current
        if snapshot.memory_state != "verification_pending":
            raise ValueError("report memory confirmation is stale")
        if not verification_ok:
            failed = snapshot.model_copy(
                update={
                    "memory_state": "attention_required",
                    "last_error_code": verification_error_code,
                    "updated_at": utc_now(),
                }
            )
            failed_record = current.model_copy(
                update={"revisions": [*current.revisions[: revision - 1], failed, *current.revisions[revision:]]}
            )
            store._save_unlocked(failed_record)
            raise ValueError("report memory verification failed")
        confirmed = snapshot.model_copy(
            update={"memory_state": "confirmed", "verified_at": utc_now(), "updated_at": utc_now()}
        )
        updated = current.model_copy(
            update={
                "confirmed_revision": revision,
                "revisions": [*current.revisions[: revision - 1], confirmed, *current.revisions[revision:]],
            }
        )
        store._save_unlocked(updated)
        return updated


def quarantine_report_memory(
    store: ReportLearningStore, session_id: str, revision: int, error_code: str
) -> ReportLearningRecord:
    """Put one failed promotion into attention-required state, blocking later revisions."""
    _validate_revision(revision)
    if error_code not in MEMORY_ERROR_CODES:
        raise ValueError("invalid report memory error code")
    with store.locked():
        record = store.load(session_id)
        if record is None or revision > len(record.revisions):
            raise ValueError("report memory record unavailable")
        snapshot = record.revisions[revision - 1]
        if revision <= record.confirmed_revision or snapshot.memory_state == "confirmed":
            raise ValueError("confirmed report memory cannot be quarantined")
        if snapshot.memory_state == "attention_required" and snapshot.last_error_code == error_code:
            return record
        if snapshot.memory_state not in {
            "add_pending",
            "replace_pending",
            "memory_call_started",
            "verification_pending",
            "attention_required",
        }:
            raise ValueError("report memory revision is not active")
        quarantined = snapshot.model_copy(
            update={"memory_state": "attention_required", "last_error_code": error_code, "updated_at": utc_now()}
        )
        updated = record.model_copy(
            update={"revisions": [*record.revisions[: revision - 1], quarantined, *record.revisions[revision:]]}
        )
        store._save_unlocked(updated)
        return updated


def _results_root() -> Path:
    configured = os.getenv("TRADINGAGENTS_RESULTS_DIR")
    return (Path(configured) if configured else Path(__file__).resolve().parents[2] / "results").expanduser().resolve()
