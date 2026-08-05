import multiprocessing
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingagents.integrations import hermes_learning
from tradingagents.integrations.hermes_learning import (
    LearningStorageError,
    LearningStore,
)
from tradingagents.integrations.schemas import (
    PaperDecisionReview,
    PriceReference,
    ReportCausalHypothesis,
    ReportLearningOutcome,
    ReportLearningRecord,
    ReportLearningRevision,
    ReportOutcomeAssessment,
    ReportReflection,
    ReportSourceMetadata,
    utc_now,
)


def report_learning_record(
    *,
    session_number: int = 100,
    trade_date: date = date(2026, 7, 1),
    horizons: tuple[int, ...] = (1,),
    lesson: str | None = None,
) -> ReportLearningRecord:
    now = utc_now()
    outcomes = [
        ReportLearningOutcome(
            review_id=f"review_{session_number * 10 + position:032x}",
            horizon_days=horizon,
            review_date=trade_date + timedelta(days=horizon),
            raw_return_pct=float(position),
            verdict="correct",
        )
        for position, horizon in enumerate(horizons, start=1)
    ]
    revisions = []
    for position in range(1, len(outcomes) + 1):
        included = outcomes[:position]
        reflection = ReportReflection(
            decision_thesis="Buy after confirmation.",
            overall_assessment="The decision used the available evidence.",
            outcome_assessments=[
                ReportOutcomeAssessment(
                    horizon_days=outcome.horizon_days,
                    assessment=f"T+{outcome.horizon_days} outcome assessment.",
                )
                for outcome in included
            ],
            reasoning_strengths=["The entry condition was explicit."],
            causal_hypotheses=[
                ReportCausalHypothesis(
                    statement="Momentum persisted.",
                    evidence=["Price held above support."],
                    confidence="medium",
                )
            ],
            mistakes_or_missed_opportunities=[],
            next_decision_checks=["Confirm volume."],
        )
        revision_lesson = (
            lesson
            if position == len(outcomes) and lesson is not None
            else f"Report lesson {session_number} revision {position}."
        )
        revisions.append(
            ReportLearningRevision(
                revision=position,
                outcome_review_ids=[outcome.review_id for outcome in included],
                reflection_state="ready",
                memory_state="add_pending" if position == 1 else "replace_pending",
                source_fields=[
                    ReportSourceMetadata(
                        name="market_report", sha256="a" * 64, truncated=False
                    )
                ],
                reflection=reflection,
                lesson=revision_lesson,
                hermes_memory_entry=revision_lesson,
                created_at=now,
                updated_at=now,
            )
        )
    return ReportLearningRecord(
        session_id=f"hermes_{session_number:032x}",
        symbol="BTC",
        trade_date=trade_date,
        action="BUY",
        source_digest="b" * 64,
        desired_revision=len(outcomes),
        reflected_revision=len(outcomes),
        outcomes=outcomes,
        revisions=revisions,
        created_at=now,
        updated_at=now,
    )


def legacy_review(
    number: int, *, session_number: int | None, lesson: str | None = None
) -> PaperDecisionReview:
    trade_date = date(2026, 6, 1)
    review_date = trade_date + timedelta(days=number + 1)
    return PaperDecisionReview(
        review_id=f"review_{number:032x}",
        session_id=(
            f"hermes_{session_number:032x}"
            if session_number is not None
            else f"hermes_{number:032x}"
        ),
        symbol="BTC",
        trade_date=trade_date,
        review_date=review_date,
        horizon_days=number + 1,
        action="BUY",
        entry_price=PriceReference(
            date=trade_date, usd_price=100.0, source="coinbase"
        ),
        review_price=PriceReference(
            date=review_date, usd_price=101.0, source="coinbase"
        ),
        raw_return_pct=1.0,
        verdict="correct",
        created_at=utc_now(),
        hermes_memory_entry=lesson or f"Legacy lesson {number}.",
    )


def _controlled_report_upsert(root, record_payload, write_started, allow_write):
    from tradingagents.integrations import hermes_learning as learning_module

    original_write = learning_module._atomic_json_write

    def controlled_write(destination, value):
        write_started.put(True)
        allow_write.wait(timeout=5)
        original_write(destination, value)

    learning_module._atomic_json_write = controlled_write
    learning_module.LearningStore(Path(root)).upsert_report(
        ReportLearningRecord.model_validate(record_payload)
    )


def _controlled_legacy_upsert(root, review_payload, write_started, allow_write):
    from tradingagents.integrations import hermes_learning as learning_module

    original_write = learning_module._atomic_json_write

    def controlled_write(destination, value):
        write_started.put(True)
        allow_write.wait(timeout=5)
        original_write(destination, value)

    learning_module._atomic_json_write = controlled_write
    learning_module.LearningStore(Path(root)).upsert(
        PaperDecisionReview.model_validate(review_payload)
    )


class HermesReportLearningTests(unittest.TestCase):
    def test_report_learning_limits_are_explicit(self):
        self.assertEqual(getattr(hermes_learning, "REPORT_LESSON_LIMIT", None), 5)
        self.assertEqual(getattr(hermes_learning, "RECENT_REPORT_LIMIT", None), 3)
        self.assertEqual(getattr(hermes_learning, "MATURE_REPORT_LIMIT", None), 2)
        self.assertEqual(
            getattr(hermes_learning, "GRAPH_LESSON_TOTAL_MAX_CHARS", None), 12000
        )

    def test_repeated_report_upsert_is_idempotent(self):
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            record = report_learning_record()
            first = store.upsert_report(record)
            path = store.path_for("BTC")
            first_bytes = path.read_bytes()
            first_mtime = path.stat().st_mtime_ns
            second = store.upsert_report(record)
            second_bytes = path.read_bytes()
            second_mtime = path.stat().st_mtime_ns

        self.assertEqual(len(first.report_entries), 1)
        self.assertEqual(second, first)
        self.assertEqual(second_bytes, first_bytes)
        self.assertEqual(second_mtime, first_mtime)

    def test_stale_report_revision_does_not_downgrade_or_rewrite_index(self):
        revision_one = report_learning_record(session_number=110, horizons=(7,))
        revision_two = report_learning_record(
            session_number=110, horizons=(1, 7)
        )
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            current = store.upsert_report(revision_two)
            path = store.path_for("BTC")
            current_bytes = path.read_bytes()
            current_mtime = path.stat().st_mtime_ns

            stale_result = store.upsert_report(revision_one)

            self.assertEqual(stale_result, current)
            self.assertEqual(path.read_bytes(), current_bytes)
            self.assertEqual(path.stat().st_mtime_ns, current_mtime)
            self.assertEqual(store.load("BTC").report_entries[0].reflected_revision, 2)

    def test_equal_report_revision_with_changed_identity_or_content_conflicts(self):
        current = report_learning_record(session_number=120)
        conflicts = (
            report_learning_record(
                session_number=120, trade_date=date(2026, 7, 2)
            ),
            report_learning_record(session_number=120, horizons=(7,)),
            report_learning_record(session_number=120, lesson="Conflicting lesson."),
        )
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            store.upsert_report(current)
            path = store.path_for("BTC")
            current_bytes = path.read_bytes()
            for conflict in conflicts:
                with self.subTest(conflict=conflict), self.assertRaisesRegex(
                    LearningStorageError, "^report learning index conflicts$"
                ):
                    store.upsert_report(conflict)
                self.assertEqual(path.read_bytes(), current_bytes)

    def test_higher_report_revision_with_changed_trade_date_conflicts(self):
        current = report_learning_record(session_number=125)
        changed_date = report_learning_record(
            session_number=125,
            trade_date=date(2026, 7, 2),
            horizons=(1, 7),
        )
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            original_index = store.upsert_report(current)
            path = store.path_for("BTC")
            original_bytes = path.read_bytes()

            with self.assertRaisesRegex(
                LearningStorageError, "^report learning index conflicts$"
            ):
                store.upsert_report(changed_date)

            self.assertEqual(store.load("BTC"), original_index)
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_higher_report_revision_with_decreased_maturity_conflicts(self):
        current = report_learning_record(session_number=126, horizons=(15,))
        decreased_maturity = report_learning_record(
            session_number=126, horizons=(1, 7)
        )
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            original_index = store.upsert_report(current)
            path = store.path_for("BTC")
            original_bytes = path.read_bytes()

            with self.assertRaisesRegex(
                LearningStorageError, "^report learning index conflicts$"
            ):
                store.upsert_report(decreased_maturity)

            self.assertEqual(store.load("BTC"), original_index)
            self.assertEqual(path.read_bytes(), original_bytes)

    def test_higher_report_revision_with_increased_maturity_updates(self):
        current = report_learning_record(session_number=127)
        increased_maturity = report_learning_record(
            session_number=127, horizons=(1, 7)
        )
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            store.upsert_report(current)

            updated = store.upsert_report(increased_maturity)

        self.assertEqual(len(updated.report_entries), 1)
        self.assertEqual(updated.report_entries[0].trade_date, current.trade_date)
        self.assertEqual(updated.report_entries[0].reflected_revision, 2)
        self.assertEqual(updated.report_entries[0].maturity_days, 7)
        self.assertEqual(
            updated.report_entries[0].lesson,
            increased_maturity.revisions[-1].lesson,
        )

    def test_concurrent_report_revisions_finish_at_highest_revision(self):
        revision_one = report_learning_record(session_number=130, horizons=(7,))
        revision_two = report_learning_record(
            session_number=130, horizons=(1, 7)
        )
        context = multiprocessing.get_context("fork")
        for first, second in (
            (revision_one, revision_two),
            (revision_two, revision_one),
        ):
            with self.subTest(
                first_revision=first.reflected_revision
            ), TemporaryDirectory() as directory:
                write_started = context.Queue()
                allow_write = context.Event()
                processes = [
                    context.Process(
                        target=_controlled_report_upsert,
                        args=(
                            directory,
                            record.model_dump(mode="json"),
                            write_started,
                            allow_write,
                        ),
                    )
                    for record in (first, second)
                ]
                processes[0].start()
                write_started.get(timeout=5)
                processes[1].start()
                allow_write.set()
                for process in processes:
                    process.join(timeout=5)
                    self.assertEqual(process.exitcode, 0)

                index = LearningStore(Path(directory)).load("BTC")

            self.assertEqual(len(index.report_entries), 1)
            self.assertEqual(index.report_entries[0].reflected_revision, 2)
            self.assertEqual(index.report_entries[0].maturity_days, 7)

    def test_concurrent_v1_legacy_upsert_and_first_report_migration_preserve_both(self):
        context = multiprocessing.get_context("fork")
        review = legacy_review(8, session_number=8)
        record = report_learning_record(session_number=140)
        with TemporaryDirectory() as directory:
            write_started = context.Queue()
            allow_write = context.Event()
            legacy_process = context.Process(
                target=_controlled_legacy_upsert,
                args=(
                    directory,
                    review.model_dump(mode="json"),
                    write_started,
                    allow_write,
                ),
            )
            report_process = context.Process(
                target=_controlled_report_upsert,
                args=(
                    directory,
                    record.model_dump(mode="json"),
                    write_started,
                    allow_write,
                ),
            )
            legacy_process.start()
            write_started.get(timeout=5)
            report_process.start()
            allow_write.set()
            for process in (legacy_process, report_process):
                process.join(timeout=5)
                self.assertEqual(process.exitcode, 0)

            index = LearningStore(Path(directory)).load("BTC")

        self.assertEqual(index.schema_version, 2)
        self.assertEqual(
            [entry.review_id for entry in index.legacy_entries], [review.review_id]
        )
        self.assertEqual(
            [entry.session_id for entry in index.report_entries], [record.session_id]
        )

    def test_report_upsert_rejects_non_reflected_or_invalid_snapshot(self):
        valid = report_learning_record()
        invalid_records = (
            valid.model_copy(update={"reflected_revision": 0}),
            valid.model_copy(update={"reflected_revision": 2}),
            valid.model_copy(
                update={
                    "revisions": [
                        valid.revisions[0].model_copy(
                            update={"reflection_state": "pending"}
                        )
                    ]
                }
            ),
            valid.model_copy(
                update={
                    "revisions": [valid.revisions[0].model_copy(update={"lesson": None})]
                }
            ),
        )
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            for record in invalid_records:
                with self.subTest(record=record), self.assertRaises(ValueError):
                    store.upsert_report(record)

    def test_balanced_selection_keeps_newest_and_recent_mature_reports(self):
        records = [
            report_learning_record(
                session_number=number,
                trade_date=date(2026, 7, number - 200),
                horizons=(1, 7, 15) if number in {201, 202, 204} else (1,),
            )
            for number in range(201, 207)
        ]
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            for record in records:
                store.upsert_report(record)

            lessons = store.lessons_for("BTC")

        self.assertEqual(
            lessons,
            [
                records[5].revisions[-1].lesson,
                records[4].revisions[-1].lesson,
                records[3].revisions[-1].lesson,
                records[1].revisions[-1].lesson,
                records[2].revisions[-1].lesson,
            ],
        )

    def test_selection_fills_other_reports_then_deduplicated_legacy(self):
        reports = [
            report_learning_record(
                session_number=number,
                trade_date=date(2026, 7, number - 300),
            )
            for number in range(301, 305)
        ]
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            for report in reports:
                store.upsert_report(report)
            for review in (
                legacy_review(1, session_number=1),
                legacy_review(2, session_number=1),
                legacy_review(3, session_number=2),
                legacy_review(4, session_number=3),
            ):
                store.upsert(review)
            index = store.load("BTC")
            unknown_entries = [
                entry.model_copy(update={"session_id": None})
                for entry in index.legacy_entries[-2:]
            ]
            index = index.model_copy(
                update={"legacy_entries": index.legacy_entries + unknown_entries}
            )
            from tradingagents.integrations.hermes_learning import _atomic_json_write

            _atomic_json_write(store.path_for("BTC"), index.model_dump(mode="json"))

            lessons = store.lessons_for("BTC", limit=9)

        self.assertEqual(lessons[:4], [r.revisions[-1].lesson for r in reversed(reports)])
        self.assertEqual(lessons[4:], [
            "Legacy lesson 4.",
            "Legacy lesson 3.",
            "Legacy lesson 2.",
            "Legacy lesson 2.",
        ])

    def test_selection_deduplicates_legacy_session_already_used_by_report(self):
        report = report_learning_record(session_number=500)
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            store.upsert(legacy_review(6, session_number=500))
            store.upsert(legacy_review(5, session_number=5))
            store.upsert(legacy_review(4, session_number=4))
            store.upsert_report(report)

            lessons = store.lessons_for("BTC", limit=3)

        self.assertEqual(
            lessons,
            [
                report.revisions[-1].lesson,
                "Legacy lesson 5.",
                "Legacy lesson 4.",
            ],
        )

    def test_selection_respects_total_character_budget_without_splitting(self):
        first = "a" * 6000
        second = "b" * 6000
        third = "c"
        with TemporaryDirectory() as directory:
            store = LearningStore(Path(directory))
            store.upsert_report(report_learning_record(session_number=401, lesson=first))
            store.upsert_report(report_learning_record(session_number=402, lesson=second))
            store.upsert_report(report_learning_record(session_number=403, lesson=third))

            lessons = store.lessons_for("BTC")

        self.assertLessEqual(
            sum(map(len, lessons)),
            hermes_learning.GRAPH_LESSON_TOTAL_MAX_CHARS,
        )
        self.assertEqual(lessons, [third, second])
        self.assertNotIn(first[:100], lessons)


if __name__ == "__main__":
    unittest.main()
