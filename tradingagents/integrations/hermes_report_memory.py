"""Ordered promotion of reflected report lessons through Hermes memory."""

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Literal

from tradingagents.integrations.hermes_report_learning import ReportLearningStore
from tradingagents.integrations.hermes_report_memory_verifier import (
    verify_report_memory_consistency,
    verify_report_memory_absence,
)
from tradingagents.integrations.hermes_report_retention import ReportMemoryRetirementStore
from tradingagents.integrations.schemas import (
    ReportLearningRecord,
    ReportLearningRevision,
    ReportMemoryRetirement,
    is_valid_session_id,
    utc_now,
)


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
RETIREMENT_MEMORY_ERROR_CODES = frozenset(
    {
        "MEMORY_MARKER_MISSING",
        "MEMORY_MARKER_DUPLICATE",
        "MEMORY_PATH_UNREADABLE",
        "MEMORY_RESULT_AMBIGUOUS",
        "MEMORY_REMOVE_FAILED",
        "MEMORY_VERIFICATION_FAILED",
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
    memory_state: str = field(default="add_pending", kw_only=True, compare=False)


@dataclass(frozen=True)
class ReportMemoryOperation(ReportMemoryWork):
    content: str
    old_text: str | None


@dataclass(frozen=True)
class ReportMemoryRetirementWork:
    session_id: str
    symbol: str
    trade_date: date
    revision: Literal[3] = 3
    maturity_days: int = 15
    state: str = "pending"


@dataclass(frozen=True)
class ReportMemoryRetirementOperation(ReportMemoryRetirementWork):
    action: Literal["remove"] = "remove"
    old_text: str = ""


def _work(record: ReportLearningRecord, revision: ReportLearningRevision) -> ReportMemoryWork:
    return ReportMemoryWork(
        session_id=record.session_id,
        symbol=record.symbol,
        trade_date=record.trade_date,
        revision=revision.revision,
        maturity_days=record.outcomes[revision.revision - 1].horizon_days,
        action="add" if revision.revision == 1 else "replace",
        memory_state=revision.memory_state,
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
    if revision.memory_state not in {
        "add_pending",
        "replace_pending",
        "memory_call_started",
        "verification_pending",
    }:
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
        if snapshot.memory_state in {"memory_call_started", "verification_pending"}:
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


def _retirement_work(item: ReportMemoryRetirement) -> ReportMemoryRetirementWork:
    return ReportMemoryRetirementWork(
        session_id=item.session_id,
        symbol=item.symbol,
        trade_date=item.trade_date,
        state=item.state,
    )


def _retirement_operation(item: ReportMemoryRetirement) -> ReportMemoryRetirementOperation:
    work = _retirement_work(item)
    return ReportMemoryRetirementOperation(
        **work.__dict__,
        old_text=item.marker,
    )


def list_pending_report_memory_retirements(
    retirement_store: ReportMemoryRetirementStore,
    report_store: ReportLearningStore,
    limit: int = 18,
) -> list[ReportMemoryRetirementWork]:
    """Reconcile journals and list bounded, completed-report retirements."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 18:
        raise ValueError("invalid report memory retirement limit")
    records = report_store.records()
    eligible = {
        record.session_id: record
        for record in records
        if record.confirmed_revision == 3
        and len(record.revisions) >= 3
        and record.revisions[2].memory_state == "confirmed"
    }
    symbols = sorted({record.symbol for record in records})
    for symbol in symbols:
        retirement_store.sync_symbol(symbol, records)
    pending: list[ReportMemoryRetirement] = []
    for journal in retirement_store.journals():
        for item in journal.items:
            if item.state not in {
                "pending",
                "memory_call_started",
                "verification_pending",
            }:
                continue
            if item.session_id not in eligible:
                continue
            pending.append(item)
    pending.sort(
        key=lambda item: (
            item.created_at,
            item.trade_date,
            item.symbol,
            item.session_id,
        )
    )
    return [_retirement_work(item) for item in pending[:limit]]


def begin_report_memory_retirement(
    store: ReportMemoryRetirementStore,
    symbol: str,
    session_id: str,
) -> ReportMemoryRetirementOperation:
    """Claim one retirement and return its persisted marker-only operation."""
    if not is_valid_session_id(session_id):
        raise ValueError("invalid report memory retirement session id")

    def transition(journal):
        if journal is None:
            raise ValueError("report memory retirement journal unavailable")
        matches = [item for item in journal.items if item.session_id == session_id]
        if len(matches) != 1:
            raise ValueError("report memory retirement item unavailable")
        item = matches[0]
        if item.state in {"memory_call_started", "verification_pending"}:
            return journal
        if item.state != "pending":
            raise ValueError("report memory retirement item is not pending")
        started = item.model_copy(update={"state": "memory_call_started", "updated_at": utc_now()})
        return journal.model_copy(
            update={
                "items": [
                    started if current.session_id == session_id else current
                    for current in journal.items
                ]
            }
        )

    journal = store.update(symbol, transition)
    item = next(item for item in journal.items if item.session_id == session_id)
    return _retirement_operation(item)


def confirm_report_memory_retirement(
    store: ReportMemoryRetirementStore,
    symbol: str,
    session_id: str,
    verifier: Callable[..., object] | None = None,
) -> ReportMemoryRetirement:
    """Verify marker absence and finalize one retirement without project mutations."""
    if not is_valid_session_id(session_id):
        raise ValueError("invalid report memory retirement session id")

    def mark_verification_pending(journal):
        if journal is None:
            raise ValueError("report memory retirement journal unavailable")
        matches = [item for item in journal.items if item.session_id == session_id]
        if len(matches) != 1:
            raise ValueError("report memory retirement item unavailable")
        item = matches[0]
        if item.state in {"verification_pending", "retired"}:
            return journal
        if item.state != "memory_call_started":
            raise ValueError("report memory retirement operation was not started")
        pending = item.model_copy(
            update={"state": "verification_pending", "updated_at": utc_now()}
        )
        return journal.model_copy(
            update={
                "items": [
                    pending if current.session_id == session_id else current
                    for current in journal.items
                ]
            }
        )

    journal = store.update(symbol, mark_verification_pending)
    item = next(item for item in journal.items if item.session_id == session_id)
    if item.state == "retired":
        return item

    verification_ok = False
    error_code = "MEMORY_VERIFICATION_FAILED"
    try:
        if verifier is None:
            memory_path = Path.home() / ".hermes" / "memories" / "MEMORY.md"
            result = verify_report_memory_absence(session_id, item.marker, memory_path)
        else:
            result = verifier(session_id, item.marker)
        if isinstance(result, bool):
            verification_ok = result
        else:
            verification_ok = (
                getattr(result, "ok", False) is True
                and getattr(result, "marker_occurrences", None) == 0
            )
            error_code = getattr(result, "error_code", error_code) or error_code
            if not verification_ok and getattr(result, "marker_occurrences", 0) > 1:
                error_code = "MEMORY_MARKER_DUPLICATE"
    except Exception:
        verification_ok = False

    def finalize(journal):
        if journal is None:
            raise ValueError("report memory retirement journal unavailable")
        current = next(
            (entry for entry in journal.items if entry.session_id == session_id), None
        )
        if current is None:
            raise ValueError("report memory retirement item unavailable")
        if current.state == "retired":
            return journal
        if current.state != "verification_pending":
            raise ValueError("report memory retirement confirmation is stale")
        if verification_ok:
            replacement = current.model_copy(
                update={
                    "state": "retired",
                    "retired_at": utc_now(),
                    "updated_at": utc_now(),
                    "last_error_code": None,
                }
            )
        else:
            replacement = current.model_copy(
                update={
                    "state": "attention_required",
                    "last_error_code": (
                        error_code
                        if error_code in RETIREMENT_MEMORY_ERROR_CODES
                        else "MEMORY_VERIFICATION_FAILED"
                    ),
                    "updated_at": utc_now(),
                }
            )
        return journal.model_copy(
            update={
                "items": [
                    replacement if entry.session_id == session_id else entry
                    for entry in journal.items
                ]
            }
        )

    final = store.update(symbol, finalize)
    result = next(item for item in final.items if item.session_id == session_id)
    return result


def quarantine_report_memory_retirement(
    store: ReportMemoryRetirementStore,
    symbol: str,
    session_id: str,
    error_code: str,
) -> ReportMemoryRetirement:
    """Persist an allowlisted retirement failure without changing project artifacts."""
    if not is_valid_session_id(session_id):
        raise ValueError("invalid report memory retirement session id")
    if error_code not in RETIREMENT_MEMORY_ERROR_CODES:
        raise ValueError("invalid report memory retirement error code")

    def transition(journal):
        if journal is None:
            raise ValueError("report memory retirement journal unavailable")
        item = next(
            (entry for entry in journal.items if entry.session_id == session_id), None
        )
        if item is None:
            raise ValueError("report memory retirement item unavailable")
        if item.state == "attention_required" and item.last_error_code == error_code:
            return journal
        if item.state == "retired":
            raise ValueError("retired report memory cannot be quarantined")
        replacement = item.model_copy(
            update={
                "state": "attention_required",
                "last_error_code": error_code,
                "updated_at": utc_now(),
            }
        )
        return journal.model_copy(
            update={
                "items": [
                    replacement if entry.session_id == session_id else entry
                    for entry in journal.items
                ]
            }
        )

    journal = store.update(symbol, transition)
    return next(item for item in journal.items if item.session_id == session_id)
