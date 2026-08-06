import json
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingagents.integrations import hermes_report_retention
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
    begin_report_memory_retirement,
    confirm_report_memory_retirement,
    list_pending_report_memory_retirements,
    list_pending_report_memory,
    quarantine_report_memory,
    quarantine_report_memory_retirement,
)
from tradingagents.integrations.hermes_report_retention import (
    ReportMemoryRetirementError,
    ReportMemoryRetirementStore,
)
from tradingagents.integrations.hermes_reports import ReportBatchStore
from tradingagents.integrations.hermes_scheduled_reviews import (
    ScheduledReviewStore,
    process_due_reviews,
)
from tradingagents.integrations import hermes_scheduled_review_runner as runner
from tradingagents.integrations.hermes_report_memory_verifier import (
    ENTRY_DELIMITER,
    verify_report_memory_capacity,
    verify_report_memory_consistency,
    verify_report_memory_absence,
)
from tradingagents.integrations.schemas import (
    AnalysisResult,
    AnalysisSession,
    DailyReportRequest,
    PriceReference,
    ReportLearningRecord,
    ReportMemoryRetirementJournal,
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


def confirmed_report_record(
    session_number: int,
    trade_date: date,
    *,
    symbol: str = "BTC",
    horizons: tuple[int, ...] = (1, 7, 15),
):
    """Build an immutable report record with all included revisions verified."""
    record = report_learning_record(
        session_number=session_number,
        trade_date=trade_date,
        horizons=horizons,
    )
    now = utc_now()
    revisions = [
        revision.model_copy(
            update={"memory_state": "confirmed", "verified_at": now}
        )
        for revision in record.revisions
    ]
    return ReportLearningRecord.model_validate(
        {
            **record.model_dump(),
            "symbol": symbol,
            "confirmed_revision": len(revisions),
            "revisions": revisions,
        }
    )


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
        if action == "remove":
            if content is not None or old_text is None:
                raise AssertionError("remove requires old text and no content")
            matches = [
                position
                for position, entry in enumerate(self.entries)
                if old_text in entry
            ]
            if len(matches) != 1:
                raise AssertionError("remove marker must identify exactly one entry")
            del self.entries[matches[0]]
            return "Entry removed"
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
    def test_report_memory_capacity_returns_count_only_metadata(self):
        with TemporaryDirectory() as directory:
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text("operator memory\n", encoding="utf-8")

            result = verify_report_memory_capacity(memory_path)

        self.assertTrue(result.ok)
        self.assertEqual(result.current_chars, len("operator memory\n"))
        self.assertEqual(result.configured_limit, 40000)
        self.assertEqual(result.reserved_report_chars, 30897)
        self.assertEqual(result.available_chars, 40000 - len("operator memory\n"))
        self.assertEqual(
            set(result.model_dump()),
            {
                "current_chars",
                "configured_limit",
                "reserved_report_chars",
                "available_chars",
                "ok",
                "error_code",
            },
        )
        serialized = result.model_dump_json()
        self.assertNotIn("operator memory", serialized)
        self.assertNotIn(str(memory_path), serialized)

    def test_report_memory_capacity_fails_closed_at_boundaries(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            memory_path = root / "MEMORY.md"
            memory_path.write_text("x" * 9000, encoding="utf-8")
            exactly_limit = verify_report_memory_capacity(memory_path)
            memory_path.write_text("x" * 9001, encoding="utf-8")
            over_limit = verify_report_memory_capacity(memory_path)
            too_small = verify_report_memory_capacity(memory_path, 39999)

        self.assertTrue(exactly_limit.ok)
        self.assertFalse(over_limit.ok)
        self.assertFalse(too_small.ok)
        self.assertEqual(exactly_limit.current_chars, 9000)
        self.assertEqual(over_limit.current_chars, 9001)

    def test_report_memory_capacity_missing_or_unreadable_is_safe_failure(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-MEMORY.md"
            missing_result = verify_report_memory_capacity(missing)
            unreadable = Path(directory) / "directory-memory"
            unreadable.mkdir()
            unreadable_result = verify_report_memory_capacity(unreadable)

        for result in (missing_result, unreadable_result):
            self.assertFalse(result.ok)
            self.assertEqual(result.current_chars, 0)
            self.assertEqual(result.available_chars, 0)
            serialized = result.model_dump_json()
            self.assertNotIn("private", serialized)
            self.assertNotIn(str(result), serialized)

    def test_report_memory_capacity_rejects_malformed_limits_without_reading(self):
        with TemporaryDirectory() as directory:
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text("operator memory", encoding="utf-8")

            results = [
                verify_report_memory_capacity(memory_path, True),
                verify_report_memory_capacity(memory_path, "40000"),
                verify_report_memory_capacity(memory_path, -1),
            ]

        for result in results:
            self.assertFalse(result.ok)
            self.assertEqual(result.current_chars, 0)
            self.assertEqual(result.available_chars, 0)

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


class HermesReportMemoryRetentionTests(unittest.TestCase):
    def test_active_report_stays_pinned_while_completed_retirements_run(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            report_store = ReportLearningStore(root / "report_memories")
            index_store = LearningStore(root / "memories")
            retirement_store = ReportMemoryRetirementStore(
                root / "report_memory_retirements"
            )
            completed = [
                confirmed_report_record(number, date(2026, 7, number))
                for number in range(1, 7)
            ]
            active = report_learning_record(
                session_number=900,
                trade_date=date(2026, 7, 20),
                horizons=(1, 7, 15),
            )
            active_marker = REPORT_MEMORY_MARKER.format(
                session_id=active.session_id
            )
            active = active.model_copy(
                update={
                    "revisions": [
                        revision.model_copy(
                            update={
                                "hermes_memory_entry": (
                                    f"{active_marker}\n"
                                    f"Active revision {revision.revision}"
                                )
                            }
                        )
                        for revision in active.revisions
                    ]
                }
            )
            for record in [*completed, active]:
                report_store.save(record)
                index_store.upsert_report(record)

            memory = FakeHermesMemory()
            memory.entries = [
                f"{REPORT_MEMORY_MARKER.format(session_id=record.session_id)}\n"
                f"Completed BTC report {record.trade_date.isoformat()}"
                for record in completed
            ]

            completed_report_bytes = {
                report_store.path_for(record.session_id): report_store.path_for(
                    record.session_id
                ).read_bytes()
                for record in completed
            }
            btc_index_bytes = index_store.path_for("BTC").read_bytes()

            first = begin_report_memory(report_store, active.session_id, 1)
            self.assertEqual(first.action, "add")
            self.assertEqual(
                memory.apply(first.action, first.content, first.old_text),
                "Entry added",
            )
            confirm_report_memory(
                report_store, active.session_id, 1, verifier=lambda *_args: True
            )

            second = begin_report_memory(report_store, active.session_id, 2)
            self.assertEqual(second.action, "replace")
            self.assertEqual(second.old_text, active_marker)
            self.assertEqual(
                memory.apply(second.action, second.content, second.old_text),
                "Entry replaced",
            )
            confirm_report_memory(
                report_store, active.session_id, 2, verifier=lambda *_args: True
            )

            memory_path = root / "MEMORY.md"
            memory_path.write_text(memory.text, encoding="utf-8")
            pending = list_pending_report_memory_retirements(
                retirement_store, report_store
            )
            oldest = next(item for item in pending if item.symbol == "BTC")
            self.assertEqual(oldest.session_id, completed[0].session_id)
            self.assertNotIn(active.session_id, {item.session_id for item in pending})
            operation = begin_report_memory_retirement(
                retirement_store, oldest.symbol, oldest.session_id
            )
            self.assertEqual(
                memory.apply("remove", None, operation.old_text), "Entry removed"
            )
            memory_path.write_text(memory.text, encoding="utf-8")
            self.assertEqual(
                confirm_report_memory_retirement(
                    retirement_store,
                    oldest.symbol,
                    oldest.session_id,
                    lambda session_id, marker: verify_report_memory_absence(
                        session_id, marker, memory_path
                    ),
                ).state,
                "retired",
            )
            self.assertIn(active_marker, memory.text)

            third = begin_report_memory(report_store, active.session_id, 3)
            self.assertEqual(third.action, "replace")
            self.assertEqual(third.old_text, active_marker)
            self.assertEqual(
                memory.apply(third.action, third.content, third.old_text),
                "Entry replaced",
            )
            confirm_report_memory(
                report_store, active.session_id, 3, verifier=lambda *_args: True
            )
            memory_path.write_text(memory.text, encoding="utf-8")
            pending_after_t15 = list_pending_report_memory_retirements(
                retirement_store, report_store
            )
            self.assertNotIn(
                active.session_id, {item.session_id for item in pending_after_t15}
            )
            self.assertIn(active_marker, memory.text)
            self.assertEqual(
                report_store.load(active.session_id).confirmed_revision, 3
            )
            self.assertEqual(
                {
                    report_store.path_for(record.session_id): report_store.path_for(
                        record.session_id
                    ).read_bytes()
                    for record in completed
                },
                completed_report_bytes,
            )
            self.assertEqual(index_store.path_for("BTC").read_bytes(), btc_index_bytes)

    def test_active_report_survives_retention_reconciliation_and_replacements(self):
        with VersionTwoLifecycleHarness() as harness:
            harness.archive_new_report()
            retirement_store = ReportMemoryRetirementStore(
                harness.root / "report_memory_retirements"
            )

            for horizon, return_pct, verdict, expected_action in (
                (1, 1.2, "correct", "add"),
                (7, -4.2, "incorrect", "replace"),
                (15, 3.1, "correct", "replace"),
            ):
                harness.process_horizon(
                    horizon, return_pct=return_pct, verdict=verdict
                )
                harness.reflect_and_promote(expected_action=expected_action)
                self.assertEqual(
                    list_pending_report_memory_retirements(
                        retirement_store, harness.report_store
                    ),
                    [],
                )

            self.assertEqual(harness.memory_actions, ["add", "replace", "replace"])
            self.assertEqual(harness.memory_marker_count(), 1)

    def test_six_completed_reports_retire_oldest_only_and_preserve_project_artifacts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            report_store = ReportLearningStore(root / "report_memories")
            review_store = ReviewStore(root / "reviews")
            index_store = LearningStore(root / "memories")
            retirement_store = ReportMemoryRetirementStore(
                root / "report_memory_retirements"
            )
            records_by_symbol = {
                "BTC": [
                    confirmed_report_record(number, date(2026, 7, number))
                    for number in range(1, 7)
                ],
                "ETH": [
                    confirmed_report_record(
                        number + 100, date(2026, 7, number), symbol="ETH"
                    )
                    for number in range(1, 7)
                ],
            }
            records = [record for group in records_by_symbol.values() for record in group]
            for record in records:
                report_store.save(record)
                index_store.upsert_report(record)
                for outcome in record.outcomes:
                    review_store.save(
                        paper_review(outcome.horizon_days, verdict=outcome.verdict).model_copy(
                            update={
                                "review_id": outcome.review_id,
                                "session_id": record.session_id,
                                "symbol": record.symbol,
                                "trade_date": record.trade_date,
                                "review_date": outcome.review_date,
                                "created_at": record.created_at,
                            }
                        )
                    )

            memory = FakeHermesMemory()
            memory.entries = [
                f"{REPORT_MEMORY_MARKER.format(session_id=record.session_id)}\n"
                f"Completed {record.symbol} report {record.trade_date.isoformat()}"
                for record in records
            ]
            memory_path = root / "MEMORY.md"
            memory_path.write_text(memory.text, encoding="utf-8")
            project_artifacts = {
                "reports": {
                    path: path.read_bytes()
                    for path in report_store.root.glob("hermes_*.json")
                },
                "reviews": {
                    path: path.read_bytes()
                    for path in review_store.root.glob("review_*.json")
                },
                "indexes": {
                    symbol: index_store.path_for(symbol).read_bytes()
                    for symbol in records_by_symbol
                },
            }

            pending = list_pending_report_memory_retirements(
                retirement_store, report_store
            )
            self.assertEqual(
                {item.symbol: item.session_id for item in pending},
                {
                    "BTC": records_by_symbol["BTC"][0].session_id,
                    "ETH": records_by_symbol["ETH"][0].session_id,
                },
            )
            btc_item = next(item for item in pending if item.symbol == "BTC")
            operation = begin_report_memory_retirement(
                retirement_store, btc_item.symbol, btc_item.session_id
            )
            self.assertEqual(
                memory.apply("remove", None, operation.old_text), "Entry removed"
            )
            memory_path.write_text(memory.text, encoding="utf-8")
            retired = confirm_report_memory_retirement(
                retirement_store,
                btc_item.symbol,
                btc_item.session_id,
                lambda session_id, marker: verify_report_memory_absence(
                    session_id, marker, memory_path
                ),
            )

            self.assertEqual(retired.state, "retired")
            btc_markers = {
                REPORT_MEMORY_MARKER.format(session_id=record.session_id)
                for record in records_by_symbol["BTC"][1:]
            }
            self.assertEqual(
                {
                    entry.split("\n", 1)[0]
                    for entry in memory.entries
                    if entry.startswith("[TradingAgents paper report:")
                    and "BTC" in entry
                },
                btc_markers,
            )
            self.assertEqual(
                sum(
                    REPORT_MEMORY_MARKER.format(session_id=record.session_id) in entry
                    for entry in memory.entries
                    for record in records_by_symbol["BTC"]
                ),
                5,
            )
            self.assertEqual(
                sum(
                    REPORT_MEMORY_MARKER.format(session_id=record.session_id) in entry
                    for entry in memory.entries
                    for record in records_by_symbol["ETH"]
                ),
                6,
            )
            remaining = list_pending_report_memory_retirements(
                retirement_store, report_store
            )
            self.assertEqual(
                [(item.symbol, item.session_id) for item in remaining],
                [("ETH", records_by_symbol["ETH"][0].session_id)],
            )
            self.assertEqual(
                {
                    "reports": {
                        path: path.read_bytes()
                        for path in report_store.root.glob("hermes_*.json")
                    },
                    "reviews": {
                        path: path.read_bytes()
                        for path in review_store.root.glob("review_*.json")
                    },
                    "indexes": {
                        symbol: index_store.path_for(symbol).read_bytes()
                        for symbol in records_by_symbol
                    },
                },
                project_artifacts,
            )

    def test_real_hermes_delimiter_keeps_ordinary_section_sign_inside_entry(self):
        self.assertEqual(ENTRY_DELIMITER, "\n§\n")
        with TemporaryDirectory() as directory:
            store = report_store_with_ready_revisions(directory, 1)
            record = store.load(SESSION_ID)
            self.assertIsNotNone(record)
            marker = REPORT_MEMORY_MARKER.format(session_id=SESSION_ID)
            entry = f"{marker}\nEvidence contains an ordinary § section sign."
            snapshot = record.revisions[0].model_copy(
                update={"hermes_memory_entry": entry}
            )
            record = record.model_copy(update={"revisions": [snapshot]})
            store.save(record)
            results = Path(directory)
            LearningStore(results / "hermes" / "memories").upsert_report(record)
            memory_path = results / "MEMORY.md"
            memory_path.write_text(
                ENTRY_DELIMITER.join([entry, "Unrelated § preference."]),
                encoding="utf-8",
            )

            result = verify_report_memory_consistency(
                SESSION_ID, 1, results, memory_path
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.marker_occurrences, 1)
        self.assertEqual(result.exact_content_occurrences, 1)

    def test_retirement_lifecycle_removes_one_marker_and_confirms_absence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            report_store = ReportLearningStore(root / "report_memories")
            retirement_store = ReportMemoryRetirementStore(
                root / "report_memory_retirements"
            )
            completed = [
                confirmed_report_record(number, date(2026, 7, number))
                for number in range(1, 8)
            ]
            for record in completed:
                report_store.save(record)

            pending = list_pending_report_memory_retirements(
                retirement_store, report_store
            )
            self.assertEqual(len(pending), 2)
            item = pending[0]
            operation = begin_report_memory_retirement(
                retirement_store, item.symbol, item.session_id
            )
            self.assertEqual(operation.action, "remove")
            self.assertEqual(
                operation.old_text,
                f"[TradingAgents paper report: {item.session_id}]",
            )

            memory_path = root / "MEMORY.md"
            memory_path.write_text(
                f"other{ENTRY_DELIMITER}[TradingAgents paper report: {item.session_id}]\nlesson",
                encoding="utf-8",
            )
            memory_path.write_text("other", encoding="utf-8")
            result = confirm_report_memory_retirement(
                retirement_store,
                item.symbol,
                item.session_id,
                lambda _session_id, marker: verify_report_memory_absence(
                    _session_id, marker, memory_path
                ),
            )
            self.assertEqual(result.state, "retired")

    def test_retirement_verification_pending_retry_does_not_mutate_memory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            report_store = ReportLearningStore(root / "report_memories")
            retirement_store = ReportMemoryRetirementStore(
                root / "report_memory_retirements"
            )
            records = [
                confirmed_report_record(number, date(2026, 7, number))
                for number in range(1, 8)
            ]
            for record in records:
                report_store.save(record)
            item = list_pending_report_memory_retirements(
                retirement_store, report_store
            )[0]
            begin_report_memory_retirement(retirement_store, "BTC", item.session_id)
            calls = []

            def failing_verifier(*args):
                calls.append(args)
                return False

            first = confirm_report_memory_retirement(
                retirement_store, "BTC", item.session_id, failing_verifier
            )
            self.assertEqual(first.state, "attention_required")
            with self.assertRaises(ValueError):
                confirm_report_memory_retirement(
                    retirement_store, "BTC", item.session_id, failing_verifier
                )
            self.assertEqual(len(calls), 1)

    def test_retirement_rejects_non_integer_zero_like_marker_counts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            report_store = ReportLearningStore(root / "report_memories")
            retirement_store = ReportMemoryRetirementStore(
                root / "report_memory_retirements"
            )
            records = [
                confirmed_report_record(number, date(2026, 7, number))
                for number in range(1, 8)
            ]
            for record in records:
                report_store.save(record)

            class UnsafeZeroLikeResult:
                ok = True
                marker_occurrences = False

            item = list_pending_report_memory_retirements(
                retirement_store, report_store
            )[0]
            begin_report_memory_retirement(retirement_store, "BTC", item.session_id)
            result = confirm_report_memory_retirement(
                retirement_store,
                "BTC",
                item.session_id,
                lambda *_: UnsafeZeroLikeResult(),
            )

            self.assertEqual(result.state, "attention_required")
            self.assertIsNone(result.retired_at)

    def test_sync_selects_only_oldest_completed_reports_beyond_five(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            report_store = ReportLearningStore(root / "report_memories")
            retirement_store = ReportMemoryRetirementStore(
                root / "report_memory_retirements"
            )
            completed = [
                confirmed_report_record(
                    number, date(2026, 7, number)
                )
                for number in range(1, 8)
            ]
            active_t7 = confirmed_report_record(
                8, date(2026, 7, 8), horizons=(1, 7)
            )
            for record in [*completed, active_t7]:
                report_store.save(record)
            report_bytes = {
                path: path.read_bytes()
                for path in report_store.root.glob("hermes_*.json")
            }

            items = retirement_store.sync_symbol("btc", report_store.records())
            repeated = retirement_store.sync_symbol("BTC", report_store.records())

            self.assertEqual(
                [item.session_id for item in items],
                [
                    "hermes_00000000000000000000000000000001",
                    "hermes_00000000000000000000000000000002",
                ],
            )
            self.assertTrue(all(item.state == "pending" for item in items))
            expected_markers = [
                f"[TradingAgents paper report: {item.session_id}]" for item in items
            ]
            self.assertEqual([item.marker for item in items], expected_markers)
            loaded = retirement_store.load("BTC")
            self.assertIsNotNone(loaded)
            self.assertEqual([item.marker for item in loaded.items], expected_markers)

            original_marker_template = hermes_report_retention.REPORT_MEMORY_MARKER
            hermes_report_retention.REPORT_MEMORY_MARKER = "[changed marker: {session_id}]"
            try:
                after_template_change = retirement_store.sync_symbol(
                    "BTC", report_store.records()
                )
            finally:
                hermes_report_retention.REPORT_MEMORY_MARKER = original_marker_template

            self.assertEqual(
                [item.marker for item in after_template_change], expected_markers
            )
            self.assertEqual(repeated, items)
            self.assertEqual(
                {
                    path: path.read_bytes()
                    for path in report_store.root.glob("hermes_*.json")
                },
                report_bytes,
            )

    def test_symbol_scoped_locks_are_normalized_and_not_shared(self):
        with TemporaryDirectory() as directory:
            root = (
                Path(directory)
                / "results"
                / "hermes"
                / "report_memory_retirements"
            )
            retirement_store = ReportMemoryRetirementStore(root)

            btc_lock = retirement_store.lock_path_for(" btc ")
            eth_lock = retirement_store.lock_path_for("ETH")

            self.assertEqual(
                btc_lock, retirement_store.path_for("BTC").with_suffix(".json.lock")
            )
            self.assertNotEqual(btc_lock, eth_lock)
            with retirement_store.locked("BTC"):
                self.assertTrue(btc_lock.exists())
                self.assertFalse((root / ".report-memory-retirements.lock").exists())

    def test_sync_is_per_symbol_and_preserves_retirement_history(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            retirement_store = ReportMemoryRetirementStore(
                root / "report_memory_retirements"
            )
            btc_records = [
                confirmed_report_record(number, date(2026, 7, number))
                for number in range(1, 8)
            ]
            eth_records = [
                confirmed_report_record(
                    number + 100, date(2026, 7, number), symbol="ETH"
                )
                for number in range(1, 8)
            ]

            btc_items = retirement_store.sync_symbol("BTC", btc_records)
            eth_items = retirement_store.sync_symbol("ETH", eth_records)
            now = utc_now()
            def preserve_history(journal):
                return ReportMemoryRetirementJournal(
                    symbol="BTC",
                    items=[
                        journal.items[0].model_copy(
                            update={"state": "retired", "retired_at": now}
                        ),
                        journal.items[1].model_copy(
                            update={
                                "state": "attention_required",
                                "last_error_code": "MEMORY_MARKER_DUPLICATE",
                            }
                        ),
                    ],
                )

            retirement_store.update("BTC", preserve_history)

            reconciled_btc = retirement_store.sync_symbol("BTC", btc_records)
            reconciled_eth = retirement_store.sync_symbol("ETH", eth_records)

            self.assertEqual(
                [(item.session_id, item.state) for item in reconciled_btc],
                [
                    (
                        "hermes_00000000000000000000000000000001",
                        "retired",
                    ),
                    (
                        "hermes_00000000000000000000000000000002",
                        "attention_required",
                    ),
                ],
            )
            self.assertEqual(
                [item.session_id for item in reconciled_eth],
                [
                    "hermes_00000000000000000000000000000065",
                    "hermes_00000000000000000000000000000066",
                ],
            )

    def test_sync_preserves_existing_journal_bytes_when_storage_fails(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            retirement_store = ReportMemoryRetirementStore(
                root / "report_memory_retirements"
            )
            initial_records = [
                confirmed_report_record(number, date(2026, 7, number))
                for number in range(1, 8)
            ]
            retirement_store.sync_symbol("BTC", initial_records)
            journal_path = retirement_store.path_for("BTC")
            original_bytes = journal_path.read_bytes()
            original_write = hermes_report_retention._atomic_json_write

            def fail_write(_destination, _value):
                raise OSError("simulated disk failure")

            hermes_report_retention._atomic_json_write = fail_write
            try:
                with self.assertRaises(ReportMemoryRetirementError):
                    retirement_store.sync_symbol(
                        "BTC",
                        [
                            *initial_records,
                            confirmed_report_record(8, date(2026, 7, 8)),
                        ],
                    )
            finally:
                hermes_report_retention._atomic_json_write = original_write

            self.assertEqual(journal_path.read_bytes(), original_bytes)

    def test_stale_save_cannot_drop_newly_synchronized_retirement(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            retirement_store = ReportMemoryRetirementStore(
                root / "report_memory_retirements"
            )
            initial_records = [
                confirmed_report_record(number, date(2026, 7, number))
                for number in range(1, 8)
            ]
            retirement_store.sync_symbol("BTC", initial_records)
            stale_journal = retirement_store.load("BTC")
            self.assertIsNotNone(stale_journal)
            self.assertEqual(len(stale_journal.items), 2)

            retirement_store.sync_symbol(
                "BTC",
                [
                    *initial_records,
                    confirmed_report_record(8, date(2026, 7, 8)),
                ],
            )
            journal_path = retirement_store.path_for("BTC")
            synchronized_bytes = journal_path.read_bytes()

            with self.assertRaises(ReportMemoryRetirementError):
                retirement_store.save(stale_journal)

            final_journal = retirement_store.load("BTC")
            self.assertIsNotNone(final_journal)
            self.assertEqual(len(final_journal.items), 3)
            self.assertEqual(journal_path.read_bytes(), synchronized_bytes)

    def test_update_transitions_latest_journal_without_external_stale_mutation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            retirement_store = ReportMemoryRetirementStore(
                root / "report_memory_retirements"
            )
            records = [
                confirmed_report_record(number, date(2026, 7, number))
                for number in range(1, 8)
            ]
            retirement_store.sync_symbol("BTC", records)
            stale_pending = retirement_store.load("BTC")
            self.assertIsNotNone(stale_pending)

            def begin_first(journal):
                return journal.model_copy(
                    update={
                        "items": [
                            journal.items[0].model_copy(
                                update={
                                    "state": "memory_call_started",
                                    "updated_at": utc_now(),
                                }
                            ),
                            *journal.items[1:],
                        ]
                    }
                )

            updated = retirement_store.update("BTC", begin_first)
            journal_path = retirement_store.path_for("BTC")
            updated_bytes = journal_path.read_bytes()

            with self.assertRaises(ReportMemoryRetirementError):
                retirement_store.save(stale_pending)

            self.assertEqual(updated.items[0].state, "memory_call_started")
            reloaded = retirement_store.load("BTC")
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.items[0].state, "memory_call_started")
            self.assertEqual(journal_path.read_bytes(), updated_bytes)

    def test_save_revalidates_tampered_journal_before_replacing_bytes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            retirement_store = ReportMemoryRetirementStore(
                root / "report_memory_retirements"
            )
            records = [
                confirmed_report_record(number, date(2026, 7, number))
                for number in range(1, 8)
            ]
            retirement_store.sync_symbol("BTC", records)
            journal_path = retirement_store.path_for("BTC")
            original_bytes = journal_path.read_bytes()

            for field_name, invalid_value in (
                ("marker", "[TradingAgents paper report: forged]"),
                ("session_id", "hermes_not-a-valid-session"),
            ):
                with self.subTest(field_name=field_name):
                    tampered = retirement_store.load("BTC")
                    self.assertIsNotNone(tampered)
                    object.__setattr__(tampered.items[0], field_name, invalid_value)

                    with self.assertRaises(ReportMemoryRetirementError):
                        retirement_store.save(tampered)

                    self.assertEqual(journal_path.read_bytes(), original_bytes)
                    self.assertEqual(
                        retirement_store.load("BTC").items[0].marker,
                        "[TradingAgents paper report: hermes_00000000000000000000000000000001]",
                    )

    def test_save_creates_and_idempotently_accepts_canonical_journal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            source_store = ReportMemoryRetirementStore(root / "source")
            source_store.sync_symbol(
                "BTC",
                [
                    confirmed_report_record(number, date(2026, 7, number))
                    for number in range(1, 8)
                ],
            )
            journal = source_store.load("BTC")
            self.assertIsNotNone(journal)

            retirement_store = ReportMemoryRetirementStore(root / "target")
            retirement_store.save(journal)
            journal_path = retirement_store.path_for("BTC")
            created_bytes = journal_path.read_bytes()
            retirement_store.save(journal)

            self.assertEqual(retirement_store.load("BTC"), journal)
            self.assertEqual(journal_path.read_bytes(), created_bytes)


if __name__ == "__main__":
    unittest.main()
