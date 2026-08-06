"""Read-only verification for ordered Hermes report-memory promotions."""

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from tradingagents.integrations.hermes_learning import LearningStore
from tradingagents.integrations.hermes_report_learning import (
    HERMES_ENTRY_DELIMITER_CHARS,
    HERMES_MEMORY_CHAR_LIMIT,
    HERMES_REPORT_ENTRY_RESERVATION,
    HERMES_REPORT_MEMORY_MAX_CHARS,
    REPORT_MEMORY_MARKER,
    ReportLearningStore,
)


ENTRY_DELIMITER = "\n§\n"
_HERMES_EXISTING_MEMORY_MAX_CHARS = 9000
_CAPACITY_ERROR_CODES = frozenset(
    {
        "MEMORY_PATH_UNREADABLE",
        "MEMORY_LIMIT_INVALID",
        "MEMORY_LIMIT_TOO_SMALL",
        "MEMORY_CAPACITY_EXCEEDED",
    }
)


@dataclass(frozen=True)
class ReportMemoryCapacityVerification:
    """Metadata-only capacity preflight for a supplied Hermes memory path."""

    current_chars: int
    configured_limit: int
    reserved_report_chars: int
    available_chars: int
    ok: bool
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.error_code is not None and self.error_code not in _CAPACITY_ERROR_CODES:
            raise ValueError("invalid capacity error code")

    def model_dump(self) -> dict[str, int | bool | str | None]:
        return {
            "current_chars": self.current_chars,
            "configured_limit": self.configured_limit,
            "reserved_report_chars": self.reserved_report_chars,
            "available_chars": self.available_chars,
            "ok": self.ok,
            "error_code": self.error_code,
        }

    def model_dump_json(self) -> str:
        return json.dumps(self.model_dump(), ensure_ascii=True, sort_keys=True)


def _capacity_result(
    *,
    current_chars: int,
    configured_limit: int,
    ok: bool,
    error_code: str | None = None,
) -> ReportMemoryCapacityVerification:
    reserved = (
        HERMES_REPORT_ENTRY_RESERVATION * HERMES_REPORT_MEMORY_MAX_CHARS
        + (HERMES_REPORT_ENTRY_RESERVATION - 1) * HERMES_ENTRY_DELIMITER_CHARS
    )
    available = max(configured_limit - current_chars, 0) if ok else 0
    return ReportMemoryCapacityVerification(
        current_chars=current_chars,
        configured_limit=configured_limit,
        reserved_report_chars=reserved,
        available_chars=available,
        ok=ok,
        error_code=error_code,
    )


def verify_report_memory_capacity(
    memory_path: Path,
    memory_char_limit: int = HERMES_MEMORY_CHAR_LIMIT,
) -> ReportMemoryCapacityVerification:
    """Return count-only capacity metadata without mutating or exposing memory."""
    if (
        isinstance(memory_char_limit, bool)
        or not isinstance(memory_char_limit, int)
        or memory_char_limit <= 0
    ):
        return _capacity_result(
            current_chars=0,
            configured_limit=0,
            ok=False,
            error_code="MEMORY_LIMIT_INVALID",
        )
    if memory_char_limit < HERMES_MEMORY_CHAR_LIMIT:
        return _capacity_result(
            current_chars=0,
            configured_limit=memory_char_limit,
            ok=False,
            error_code="MEMORY_LIMIT_TOO_SMALL",
        )
    if not isinstance(memory_path, (Path, str)):
        return _capacity_result(
            current_chars=0,
            configured_limit=memory_char_limit,
            ok=False,
            error_code="MEMORY_PATH_UNREADABLE",
        )
    try:
        current_chars = len(Path(memory_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, TypeError, ValueError):
        return _capacity_result(
            current_chars=0,
            configured_limit=memory_char_limit,
            ok=False,
            error_code="MEMORY_PATH_UNREADABLE",
        )
    if current_chars > _HERMES_EXISTING_MEMORY_MAX_CHARS:
        return _capacity_result(
            current_chars=current_chars,
            configured_limit=memory_char_limit,
            ok=False,
            error_code="MEMORY_CAPACITY_EXCEEDED",
        )
    return _capacity_result(
        current_chars=current_chars,
        configured_limit=memory_char_limit,
        ok=True,
    )


@dataclass(frozen=True)
class ReportMemoryVerification:
    session_id: str
    revision: int
    report_exists: bool
    target_ready: bool
    marker_occurrences: int
    exact_content_occurrences: int
    index_exists: bool
    index_matches_latest_reflection: bool

    @property
    def ok(self) -> bool:
        return (
            self.report_exists
            and self.target_ready
            and self.marker_occurrences == 1
            and self.exact_content_occurrences == 1
            and self.index_matches_latest_reflection
        )


def verify_report_memory_consistency(
    session_id: str,
    revision: int,
    results_root: Path,
    memory_path: Path,
) -> ReportMemoryVerification:
    """Check report, v2 symbol index, and memory text without writing anything."""
    report_exists = False
    target_ready = False
    marker_occurrences = 0
    exact_content_occurrences = 0
    index_exists = False
    index_matches_latest_reflection = False
    try:
        report_store = ReportLearningStore(Path(results_root) / "hermes" / "report_memories")
        record = report_store.load(session_id)
        if record is None:
            return ReportMemoryVerification(
                session_id, revision, False, False, 0, 0, False, False
            )
        report_exists = True
        if isinstance(revision, bool) or not isinstance(revision, int) or not 1 <= revision <= len(record.revisions):
            return ReportMemoryVerification(
                session_id, revision, True, False, 0, 0, False, False
            )
        snapshot = record.revisions[revision - 1]
        target_ready = (
            snapshot.reflection_state == "ready"
            and snapshot.hermes_memory_entry is not None
            and snapshot.lesson is not None
        )
        memory_text = Path(memory_path).read_text(encoding="utf-8")
        marker = REPORT_MEMORY_MARKER.format(session_id=session_id)
        marker_occurrences = memory_text.count(marker)
        if snapshot.hermes_memory_entry is not None:
            desired = snapshot.hermes_memory_entry.strip("\r\n")
            exact_content_occurrences = sum(
                entry.strip("\r\n") == desired
                for entry in memory_text.split(ENTRY_DELIMITER)
            )

        index = LearningStore(Path(results_root) / "hermes" / "memories").load(record.symbol)
        index_exists = index is not None and index.schema_version == 2
        if index_exists:
            matches = [entry for entry in index.report_entries if entry.session_id == session_id]
            if (
                index.symbol == record.symbol
                and len(matches) == 1
                and 1 <= record.reflected_revision <= len(record.revisions)
            ):
                latest = record.revisions[record.reflected_revision - 1]
                index_matches_latest_reflection = (
                    matches[0].reflected_revision == record.reflected_revision
                    and matches[0].trade_date == record.trade_date
                    and matches[0].maturity_days
                    == record.outcomes[record.reflected_revision - 1].horizon_days
                    and latest.lesson is not None
                    and matches[0].lesson == latest.lesson
                )
    except (OSError, ValueError, TypeError, json.JSONDecodeError, ValidationError):
        pass
    return ReportMemoryVerification(
        session_id,
        revision,
        report_exists,
        target_ready,
        marker_occurrences,
        exact_content_occurrences,
        index_exists,
        index_matches_latest_reflection,
    )
