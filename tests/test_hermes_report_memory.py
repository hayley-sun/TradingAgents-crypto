import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingagents.integrations.hermes_learning import LearningStore, ReviewStore
from tradingagents.integrations.hermes_mcp import (
    SessionStore,
    archive_daily_report_impl,
    review_paper_decision_impl,
)
from tradingagents.integrations.hermes_report_learning import (
    REPORT_MEMORY_MARKER,
    ReportLearningStore,
    record_review_fact,
    submit_report_reflection,
)
from tradingagents.integrations.hermes_report_memory import (
    MEMORY_ERROR_CODES,
    begin_report_memory,
    confirm_report_memory,
    list_pending_report_memory,
    quarantine_report_memory,
)
from tradingagents.integrations.hermes_reports import ReportBatchStore
from tradingagents.integrations.hermes_scheduled_reviews import (
    ScheduledReviewStore,
    process_due_reviews,
)
from tradingagents.integrations import hermes_scheduled_review_runner as runner
from tradingagents.integrations.hermes_report_memory_verifier import (
    ENTRY_DELIMITER,
    verify_report_memory_consistency,
)
from tradingagents.integrations.schemas import (
    AnalysisResult,
    AnalysisSession,
    DailyReportRequest,
    PriceReference,
    utc_now,
)
from tests.test_hermes_report_learning import (
    completed_session,
    paper_review,
    valid_reflection_payload,
    report_learning_record,
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


class FakeHermesMemory:
    """In-memory model of Hermes exact add and marker-based replace semantics."""

    def __init__(self):
        self.entries: list[str] = []
        self.actions: list[str] = []

    @property
    def text(self) -> str:
        return ENTRY_DELIMITER.join(self.entries)

    def apply(self, action: str, content: str, old_text: str | None) -> str:
        self.actions.append(action)
        if action == "add":
            if old_text is not None:
                raise AssertionError("add must not have old text")
            if content in self.entries:
                return "Entry already exists"
            self.entries.append(content)
            return "Entry added"
        if action != "replace" or old_text is None:
            raise AssertionError("replace requires old text")
        matches = [
            position
            for position, entry in enumerate(self.entries)
            if old_text in entry
        ]
        if len(matches) != 1:
            raise AssertionError("replace marker must identify exactly one entry")
        self.entries[matches[0]] = content
        return "Entry replaced"


class VersionTwoLifecycleHarness:
    def __init__(self):
        self._temporary_directory = TemporaryDirectory()
        self.root = Path(self._temporary_directory.name) / "results" / "hermes"
        self.trade_date = date(2026, 7, 1)
        self.session_id = SESSION_ID
        self.batch_store = ReportBatchStore(self.root / "report_batches")
        self.session_store = SessionStore(self.root / "sessions")
        self.schedule_store = ScheduledReviewStore(self.root / "review_schedules")
        self.review_store = ReviewStore(self.root / "reviews")
        self.report_store = ReportLearningStore(self.root / "report_memories")
        self.index_store = LearningStore(self.root / "memories")
        self.memory = FakeHermesMemory()
        self.revision_progression: list[int] = []
        self.archive_result: dict = {}
        self.canonical_review_results: list[dict] = []
        self._review_snapshots: dict[Path, bytes] = {}
        self.review_immutability_checks = 0

        self.request = DailyReportRequest(
            trade_date=self.trade_date,
            symbols=["BTC"],
            analysts=["market", "news", "fundamentals"],
            research_depth=1,
            llm_provider="deepseek",
            quick_model="deepseek-v4-flash",
            deep_model="deepseek-v4-pro",
        )
        self.session = AnalysisSession(
            session_id=self.session_id,
            status="completed",
            created_at=utc_now(),
            completed_at=utc_now(),
            request=self.request.for_symbol("BTC"),
            result=AnalysisResult(
                reports={
                    "market": "BTC held archived support after confirmation.",
                    "sentiment": "Sentiment was constructive but mixed.",
                    "news": "News was context rather than an entry trigger.",
                    "fundamentals": "Fundamentals did not contradict the thesis.",
                },
                investment_plan="Buy only after confirmation.",
                trader_investment_plan="Use a paper position with an invalidation level.",
                final_trade_decision="FINAL TRANSACTION PROPOSAL: **BUY**",
                processed_signal="BUY",
            ),
        )
        self.session_store.save(self.session)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._temporary_directory.cleanup()

    @property
    def memory_actions(self) -> list[str]:
        return self.memory.actions

    @property
    def memory_text(self) -> str:
        return self.memory.text

    def archive_new_report(self) -> None:
        self.batch_store.create_or_load(
            self.request, lambda _request: self.session_id
        )
        self.archive_result = archive_daily_report_impl(
            self.trade_date.isoformat(),
            "Lifecycle integration report.",
            batch_store=self.batch_store,
            session_store=self.session_store,
            schedule_store=self.schedule_store,
        )
        if self.archive_result.get("ok") is not True:
            raise AssertionError("v2 report archive failed")
        archived = self.batch_store.load(self.trade_date)
        if archived is None or archived.archive is None:
            raise AssertionError("v2 report was not archived")
        plan = self.schedule_store.load(self.trade_date)
        if plan is None:
            raise AssertionError("v2 schedule was not enrolled")
        if plan.workflow_version != 2:
            raise AssertionError("v2 schedule was not enrolled")

    def process_horizon(
        self, horizon: int, *, return_pct: float, verdict: str
    ) -> None:
        plan = self.schedule_store.load(self.trade_date)
        if plan is None:
            raise AssertionError("scheduled review plan is unavailable")
        item = next(item for item in plan.items if item.horizon_days == horizon)

        def reviewer(session_id: str, review_date: date, workflow_version: int):
            if (
                session_id != self.session_id
                or review_date != item.review_date
                or workflow_version != 2
            ):
                raise AssertionError("scheduled review identity changed")
            result = review_paper_decision_impl(
                {
                    "session_id": session_id,
                    "review_date": review_date.isoformat(),
                },
                store=self.session_store,
                review_store=self.review_store,
                learning_store=self.index_store,
                price_reference_resolver=lambda _symbol, trade_date, observed_date: (
                    PriceReference(
                        date=trade_date, usd_price=100.0, source="coinbase"
                    ),
                    PriceReference(
                        date=observed_date,
                        usd_price=100.0 * (1.0 + return_pct / 100.0),
                        source="coinbase",
                    ),
                ),
                current_date=review_date,
                write_legacy_learning=False,
            )
            if result.get("ok") is not True:
                raise AssertionError("canonical paper review failed")
            review = result["data"]["review"]
            if (review["raw_return_pct"], review["verdict"]) != (
                return_pct,
                verdict,
            ):
                raise AssertionError("canonical paper review outcome changed")
            self.canonical_review_results.append(result)
            return result

        result = process_due_reviews(
            self.schedule_store,
            item.review_date + timedelta(days=1),
            reviewer,
            fact_recorder=lambda review: record_review_fact(
                self.report_store, self.session_store.load(self.session_id), review
            ),
        )
        if (result.reviewed_count, result.report_fact_count) != (1, 1):
            raise AssertionError("v2 scheduled review did not record one fact")
        for path, expected_bytes in self._review_snapshots.items():
            if path.read_bytes() != expected_bytes:
                raise AssertionError("persisted review was mutated")
            self.review_immutability_checks += 1
        for path in self.review_store_files():
            if path not in self._review_snapshots:
                self._review_snapshots[path] = path.read_bytes()

    def reflect_and_promote(self, *, expected_action: str) -> None:
        before = self.record()
        revision = before.desired_revision
        reflected = submit_report_reflection(
            self.report_store,
            self.index_store,
            self.session_store.load(self.session_id),
            revision,
            valid_reflection_payload(
                horizons=tuple(outcome.horizon_days for outcome in before.outcomes)
            ),
        )
        operation = begin_report_memory(self.report_store, self.session_id, revision)
        if operation.action != expected_action:
            raise AssertionError("unexpected report memory action")
        expected_old_text = (
            None
            if revision == 1
            else REPORT_MEMORY_MARKER.format(session_id=self.session_id)
        )
        if operation.old_text != expected_old_text:
            raise AssertionError("unexpected report memory replace marker")
        self.memory.apply(operation.action, operation.content, operation.old_text)
        confirmed = confirm_report_memory(
            self.report_store,
            self.session_id,
            revision,
            verifier=self._verify_memory,
        )
        if reflected.reflected_revision != revision:
            raise AssertionError("reflection revision did not advance")
        self.revision_progression.append(confirmed.confirmed_revision)

    def _verify_memory(self, session_id: str, revision: int) -> bool:
        record = self.report_store.load(session_id)
        index = self.index_store.load("BTC")
        if record is None or index is None:
            return False
        content = record.revisions[revision - 1].hermes_memory_entry
        marker = REPORT_MEMORY_MARKER.format(session_id=session_id)
        indexed = [
            entry
            for entry in index.report_entries
            if entry.session_id == session_id
            and entry.reflected_revision == revision
        ]
        return (
            content is not None
            and self.memory.entries.count(content) == 1
            and self.memory.text.count(marker) == 1
            and len(indexed) == 1
        )

    def review_store_files(self) -> list[Path]:
        return sorted(self.review_store.root.glob("review_*.json"))

    def report_store_files(self) -> list[Path]:
        return sorted(self.report_store.root.glob("hermes_*.json"))

    def persisted_review_outcomes(self) -> list[tuple[int, float, str]]:
        reviews = [
            self.review_store.load(path.stem) for path in self.review_store_files()
        ]
        return sorted(
            (review.horizon_days, review.raw_return_pct, review.verdict)
            for review in reviews
            if review is not None
        )

    def report_outcomes(self) -> list[tuple[int, float, str]]:
        return [
            (outcome.horizon_days, outcome.raw_return_pct, outcome.verdict)
            for outcome in self.record().outcomes
        ]

    def index(self):
        index = self.index_store.load("BTC")
        if index is None:
            raise AssertionError("symbol learning index is unavailable")
        return index

    def record(self):
        record = self.report_store.load(self.session_id)
        if record is None:
            raise AssertionError("report learning record is unavailable")
        return record

    def latest_memory_entry(self) -> str:
        record = self.record()
        content = record.revisions[record.confirmed_revision - 1].hermes_memory_entry
        if content is None:
            raise AssertionError("confirmed report memory content is unavailable")
        return content

    def memory_marker_count(self) -> int:
        marker = REPORT_MEMORY_MARKER.format(session_id=self.session_id)
        return self.memory.text.count(marker)

    def memory_entry_count_for_report(self) -> int:
        marker = REPORT_MEMORY_MARKER.format(session_id=self.session_id)
        return sum(marker in entry for entry in self.memory.entries)


class HermesReportMemoryTests(unittest.TestCase):
    def test_v2_report_lifecycle_keeps_one_project_and_memory_entry(self):
        with VersionTwoLifecycleHarness() as harness:
            harness.archive_new_report()

            for horizon, return_pct, verdict, expected_action in (
                (1, 1.2, "correct", "add"),
                (7, -4.2, "incorrect", "replace"),
                (15, 3.1, "correct", "replace"),
            ):
                harness.process_horizon(
                    horizon, return_pct=return_pct, verdict=verdict
                )
                harness.reflect_and_promote(expected_action=expected_action)

            self.assertEqual(len(harness.review_store_files()), 3)
            self.assertEqual(len(harness.report_store_files()), 1)
            self.assertEqual(len(harness.index().report_entries), 1)
            self.assertTrue(harness.archive_result["ok"])
            self.assertEqual(len(harness.canonical_review_results), 3)
            self.assertEqual(
                harness.persisted_review_outcomes(),
                [(1, 1.2, "correct"), (7, -4.2, "incorrect"), (15, 3.1, "correct")],
            )
            self.assertEqual(
                harness.report_outcomes(),
                [(1, 1.2, "correct"), (7, -4.2, "incorrect"), (15, 3.1, "correct")],
            )
            self.assertEqual(harness.review_immutability_checks, 3)
            self.assertEqual(harness.revision_progression, [1, 2, 3])
            self.assertEqual(harness.memory_actions, ["add", "replace", "replace"])
            self.assertEqual(harness.record().desired_revision, 3)
            self.assertEqual(harness.record().confirmed_revision, 3)
            self.assertEqual(harness.memory_marker_count(), 1)
            self.assertEqual(harness.memory_entry_count_for_report(), 1)
            self.assertEqual(harness.memory_text, harness.latest_memory_entry())

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

    def test_pending_memory_orders_by_earliest_revision_creation_time(self):
        first = report_learning_record(session_number=101)
        second = report_learning_record(session_number=202)
        first_revision = first.revisions[0].model_copy(
            update={"created_at": datetime(2026, 7, 3, tzinfo=timezone.utc)}
        )
        second_revision = second.revisions[0].model_copy(
            update={"created_at": datetime(2026, 7, 1, tzinfo=timezone.utc)}
        )
        first = first.model_copy(update={"revisions": [first_revision, *first.revisions[1:]]})
        second = second.model_copy(update={"revisions": [second_revision, *second.revisions[1:]]})

        class Store:
            def records(self):
                return [first, second]

        work = list_pending_report_memory(Store(), limit=18)
        self.assertEqual([item.session_id for item in work], [second.session_id, first.session_id])

    def test_runner_rejects_unallowlisted_quarantine_code_before_store_call(self):
        called = []

        def sentinel(*args):
            called.append(args)
            raise AssertionError("store must not be called")

        code, payload = runner.run_quarantine_report_memory(
            SESSION_ID, 1, "NOT_ALLOWLISTED", sentinel
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST")
        self.assertEqual(called, [])

    def test_verifier_rejects_tampered_index_symbol(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1)
            record = store.load(SESSION_ID)
            results = Path(directory)
            index_store = LearningStore(results / "hermes" / "memories")
            index_store.upsert_report(record)
            index_path = index_store.path_for(record.symbol)
            payload = json.loads(index_path.read_text(encoding="ascii"))
            payload["symbol"] = "ETH"
            index_path.write_text(json.dumps(payload), encoding="ascii")
            memory_path = results / "MEMORY.md"
            memory_path.write_text(record.revisions[0].hermes_memory_entry, encoding="utf-8")
            result = verify_report_memory_consistency(SESSION_ID, 1, results, memory_path)
        self.assertFalse(result.index_matches_latest_reflection)
        self.assertFalse(result.ok)

    def test_verifier_requires_exact_marker_segment_and_allows_unrelated_entries(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1)
            record = store.load(SESSION_ID)
            results = Path(directory)
            LearningStore(results / "hermes" / "memories").upsert_report(record)
            entry = record.revisions[0].hermes_memory_entry
            other_marker = REPORT_MEMORY_MARKER.format(session_id="hermes_abcdef0123456789")
            memory_path = results / "MEMORY.md"
            memory_path.write_text(
                ENTRY_DELIMITER.join(
                    [f"{other_marker}\nUnrelated report entry.", entry]
                ),
                encoding="utf-8",
            )
            self.assertTrue(verify_report_memory_consistency(SESSION_ID, 1, results, memory_path).ok)
            target_marker = REPORT_MEMORY_MARKER.format(session_id=SESSION_ID)
            for tampered in (entry + " forged suffix", entry.replace(target_marker, target_marker + " forged prefix")):
                memory_path.write_text(tampered, encoding="utf-8")
                self.assertFalse(verify_report_memory_consistency(SESSION_ID, 1, results, memory_path).ok)

    def test_verifier_uses_complete_hermes_entries_at_any_position(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1)
            record = store.load(SESSION_ID)
            results = Path(directory)
            LearningStore(results / "hermes" / "memories").upsert_report(record)
            entry = record.revisions[0].hermes_memory_entry
            memory_path = results / "MEMORY.md"
            ordinary_entries = ["Ordinary preference.", "Another unrelated memory."]

            for entries in (
                [entry, *ordinary_entries],
                [ordinary_entries[0], entry, ordinary_entries[1]],
                [*ordinary_entries, entry],
            ):
                with self.subTest(position=entries.index(entry)):
                    memory_path.write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")
                    result = verify_report_memory_consistency(
                        SESSION_ID, 1, results, memory_path
                    )
                    self.assertTrue(result.ok)
                    self.assertEqual(result.exact_content_occurrences, 1)

            for entries in (
                [entry, entry],
                [entry + " forged suffix", ordinary_entries[0]],
            ):
                with self.subTest(entries=entries):
                    memory_path.write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")
                    self.assertFalse(
                        verify_report_memory_consistency(
                            SESSION_ID, 1, results, memory_path
                        ).ok
                    )

    def test_verification_pending_is_listable_and_begin_is_idempotent_after_crash(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1)
            first = begin_report_memory(store, SESSION_ID, 1)

            def mark_verification_pending(current):
                snapshot = current.revisions[0].model_copy(update={"memory_state": "verification_pending"})
                return current.model_copy(update={"revisions": [snapshot]})

            store.update(SESSION_ID, mark_verification_pending)
            code, listing = runner.run_report_memory_pending(
                18, lambda limit: list_pending_report_memory(store, limit)
            )
            restarted = begin_report_memory(store, SESSION_ID, 1)
            confirmed = confirm_report_memory(store, SESSION_ID, 1, verifier=lambda *_args: True)
        self.assertEqual(code, 0)
        self.assertEqual(listing["count"], 1)
        self.assertEqual(first, restarted)
        self.assertEqual(confirmed.confirmed_revision, 1)

    def test_runner_exposes_verification_pending_without_memory_mutation_payload(self):
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1)
            first = begin_report_memory(store, SESSION_ID, 1)

            def mark_verification_pending(current):
                snapshot = current.revisions[0].model_copy(update={"memory_state": "verification_pending"})
                return current.model_copy(update={"revisions": [snapshot]})

            store.update(SESSION_ID, mark_verification_pending)
            code, listing = runner.run_report_memory_pending(
                18, lambda limit: list_pending_report_memory(store, limit)
            )
            begin_code, begin_payload = runner.run_begin_report_memory(
                SESSION_ID, 1, lambda session_id, revision: begin_report_memory(store, session_id, revision)
            )
        self.assertEqual(first.memory_state, "memory_call_started")
        self.assertEqual(code, 0)
        self.assertEqual(listing["items"][0]["memory_state"], "verification_pending")
        self.assertEqual(begin_code, 0)
        self.assertEqual(begin_payload["memory_state"], "verification_pending")
        self.assertNotIn("content", begin_payload)
        self.assertNotIn("old_text", begin_payload)


if __name__ == "__main__":
    unittest.main()
