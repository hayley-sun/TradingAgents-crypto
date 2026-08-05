import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingagents.integrations.hermes_learning import LearningStore
from tradingagents.integrations.hermes_report_learning import ReportLearningStore
from tradingagents.integrations.hermes_report_memory import (
    begin_report_memory,
    confirm_report_memory,
    list_pending_report_memory,
    quarantine_report_memory,
)
from tradingagents.integrations.hermes_report_memory_verifier import (
    verify_report_memory_consistency,
)
from tests.test_hermes_report_learning import (
    completed_session,
    paper_review,
    valid_reflection_payload,
)


SESSION_ID = "hermes_0123456789abcdef"


def report_store_with_ready_revisions(directory: str, *horizons: int):
    report_store = ReportLearningStore(Path(directory) / "hermes" / "report_memories")
    index_store = LearningStore(Path(directory) / "hermes" / "memories")
    session = completed_session()
    from tradingagents.integrations.hermes_report_learning import record_review_fact, submit_report_reflection

    for horizon in horizons:
        record_review_fact(report_store, session, paper_review(horizon))
    for revision in range(1, len(horizons) + 1):
        submit_report_reflection(
            report_store,
            index_store,
            session,
            revision,
            valid_reflection_payload(horizons=tuple(horizons[:revision])),
        )
    return report_store


class HermesReportMemoryTests(unittest.TestCase):
    def test_memory_queue_exposes_only_earliest_unconfirmed_revision(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1, 7)
            work = list_pending_report_memory(store, limit=18)
        self.assertEqual([(item.revision, item.action) for item in work], [(1, "add")])

    def test_confirmed_t1_unlocks_t7_replace(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1, 7)
            started = begin_report_memory(store, SESSION_ID, 1)
            self.assertEqual(started.action, "add")
            confirm_report_memory(store, SESSION_ID, 1, verifier=lambda *_args: True)
            replacement = begin_report_memory(store, SESSION_ID, 2)
        self.assertEqual(replacement.action, "replace")
        self.assertEqual(replacement.old_text, f"[TradingAgents paper report: {SESSION_ID}]")

    def test_report_verifier_requires_one_marker_and_exact_revision_content(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1)
            record = store.load(SESSION_ID)
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text(record.revisions[0].hermes_memory_entry, encoding="utf-8")
            results = Path(directory)
            LearningStore(results / "hermes" / "memories").upsert_report(record)
            result = verify_report_memory_consistency(SESSION_ID, 1, results, memory_path)
        self.assertEqual(result.marker_occurrences, 1)
        self.assertTrue(result.index_matches_latest_reflection)

    def test_verifier_rejects_zero_or_two_markers_and_wrong_content(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1)
            record = store.load(SESSION_ID)
            results = Path(directory)
            LearningStore(results / "hermes" / "memories").upsert_report(record)
            memory_path = results / "MEMORY.md"
            entry = record.revisions[0].hermes_memory_entry
            for content in ("", entry + "\n" + entry, entry.replace("BUY", "SELL")):
                memory_path.write_text(content, encoding="utf-8")
                result = verify_report_memory_consistency(SESSION_ID, 1, results, memory_path)
                self.assertFalse(result.ok)

    def test_missing_index_record_is_reported_without_writing(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1)
            record = store.load(SESSION_ID)
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text(record.revisions[0].hermes_memory_entry, encoding="utf-8")
            result = verify_report_memory_consistency(SESSION_ID, 1, Path(directory) / "missing-index", memory_path)
        self.assertFalse(result.index_matches_latest_reflection)

    def test_quarantine_blocks_later_revision(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1, 7)
            begin_report_memory(store, SESSION_ID, 1)
            quarantined = quarantine_report_memory(store, SESSION_ID, 1, "MEMORY_CONTENT_MISMATCH")
            work = list_pending_report_memory(store, limit=18)
        self.assertEqual(quarantined.revisions[0].memory_state, "attention_required")
        self.assertEqual(work, [])

    def test_begin_is_idempotent_after_memory_call_started(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1)
            first = begin_report_memory(store, SESSION_ID, 1)
            restarted = begin_report_memory(store, SESSION_ID, 1)
        self.assertEqual(first, restarted)

    def test_stale_confirmation_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1, 7)
            with self.assertRaises(ValueError):
                confirm_report_memory(store, SESSION_ID, 2, verifier=lambda *_args: True)

    def test_ambiguous_memory_verifier_result_requires_attention(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1)
            begin_report_memory(store, SESSION_ID, 1)
            with self.assertRaises(ValueError):
                confirm_report_memory(store, SESSION_ID, 1, verifier=lambda *_args: object())
            self.assertEqual(store.load(SESSION_ID).revisions[0].memory_state, "attention_required")


if __name__ == "__main__":
    unittest.main()
