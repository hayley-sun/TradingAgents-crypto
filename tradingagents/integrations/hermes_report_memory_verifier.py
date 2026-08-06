"""Read-only verification for ordered Hermes report-memory promotions."""

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from tradingagents.integrations.hermes_learning import LearningStore
from tradingagents.integrations.hermes_report_learning import (
    REPORT_MEMORY_MARKER,
    ReportLearningStore,
)


ENTRY_DELIMITER = "\n§\n"


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
