import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from tradingagents.integrations.hermes_learning import (
    LearningStore,
    ReviewStore,
    review_completed_session,
)
from tradingagents.integrations.hermes_review_verifier import verify_review_consistency
from tradingagents.integrations.hermes_report_learning import (
    ReportLearningStore,
    record_review_fact,
)
from tradingagents.integrations.hermes_scheduled_reviews import (
    ScheduledReviewConfirmationError,
    ScheduledReviewStore,
    confirm_scheduled_memory,
    list_pending_memory,
    process_due_reviews,
)
from tradingagents.integrations.hermes_report_learning import ReportLearningStore
from tradingagents.integrations.schemas import (
    DailyReportArchive,
    DailyReportArchiveItem,
    DailyReportBatch,
    DailyReportBatchItem,
    DailyReportRequest,
    AnalysisResult,
    AnalysisSession,
    PaperDecisionReview,
    PriceReference,
    ScheduledReviewItem,
    utc_now,
)


SESSION_IDS = {
    "BTC": "hermes_0000000000000001",
    "ETH": "hermes_0000000000000002",
    "SOL": "hermes_0000000000000003",
}


def archived_batch(
    statuses: dict[str, str] | None = None,
    *,
    trade_date: date = date(2026, 8, 5),
    batch_id: str = "report_0123456789abcdef",
    session_ids: dict[str, str] = SESSION_IDS,
    workflow_version: int | None = None,
) -> DailyReportBatch:
    item_statuses = statuses or {symbol: "completed" for symbol in session_ids}
    request = DailyReportRequest(
        trade_date=trade_date,
        symbols=list(session_ids),
        analysts=["market", "news", "fundamentals"],
        research_depth=1,
        llm_provider="deepseek",
        quick_model="deepseek-v4-flash",
        deep_model="deepseek-v4-pro",
    )
    return DailyReportBatch(
        batch_id=batch_id,
        request=request,
        created_at=utc_now(),
        items=[
            DailyReportBatchItem(symbol=symbol, session_id=session_id)
            for symbol, session_id in session_ids.items()
        ],
        archive=DailyReportArchive(
            filename=f"{trade_date.isoformat()}.md",
            sha256="0" * 64,
            state="degraded" if "failed" in item_statuses.values() else "ready",
            archived_at=utc_now(),
            items=[
                DailyReportArchiveItem(
                    symbol=symbol,
                    status=item_statuses[symbol],
                    error_code=(
                        "ANALYSIS_FAILED"
                        if item_statuses[symbol] == "failed"
                        else None
                    ),
                )
                for symbol in session_ids
            ],
            scheduled_review_version=workflow_version,
        ),
    )


class HermesScheduledReviewTests(unittest.TestCase):
    def test_new_archive_creates_v2_plan(self):
        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            plan = store.create_or_load(archived_batch(workflow_version=2))

        self.assertEqual(plan.workflow_version, 2)

    def test_v2_due_review_completes_fact_without_memory_pending(self):
        seen = []

        def reviewer(_session_id, _review_date, version):
            self.assertEqual(version, 2)
            item = plan.items[0]
            return {
                "ok": True,
                "data": {"review": {
                    "review_id": item.review_id,
                    "session_id": item.session_id,
                    "symbol": item.symbol,
                    "trade_date": plan.trade_date.isoformat(),
                    "review_date": item.review_date.isoformat(),
                    "horizon_days": item.horizon_days,
                    "action": "BUY",
                    "entry_price": {"date": plan.trade_date.isoformat(), "usd_price": 100.0, "source": "coinbase"},
                    "review_price": {"date": item.review_date.isoformat(), "usd_price": 110.0, "source": "coinbase"},
                    "raw_return_pct": 10.0,
                    "verdict": "correct",
                    "created_at": utc_now().isoformat(),
                    "hermes_memory_entry": "legacy lesson",
                }},
            }

        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            plan = store.create_or_load(archived_batch(workflow_version=2))
            report = process_due_reviews(
                store,
                date(2026, 8, 7),
                reviewer,
                fact_recorder=lambda review: seen.append(review.review_id),
            )
            item = store.find_item(plan.items[0].review_id)[1]

        self.assertEqual(seen, [plan.items[0].review_id])
        self.assertEqual(item.state, "completed")
        self.assertEqual(report.report_fact_count, 1)

    def test_v1_due_review_keeps_memory_pending(self):
        def reviewer(_session_id, _review_date, _version):
            item = plan.items[0]
            return {"ok": True, "data": {"review": {
                "review_id": item.review_id,
                "session_id": item.session_id,
                "symbol": item.symbol,
                "trade_date": plan.trade_date.isoformat(),
                "review_date": item.review_date.isoformat(),
                "horizon_days": item.horizon_days,
                "action": "BUY",
                "entry_price": {"date": plan.trade_date.isoformat(), "usd_price": 100.0, "source": "coinbase"},
                "review_price": {"date": item.review_date.isoformat(), "usd_price": 110.0, "source": "coinbase"},
                "raw_return_pct": 10.0,
                "verdict": "correct",
                "created_at": utc_now().isoformat(),
                "hermes_memory_entry": "legacy lesson",
            }}}

        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            plan = store.create_or_load(archived_batch())
            process_due_reviews(store, date(2026, 8, 7), reviewer)
            item = store.find_item(plan.items[0].review_id)[1]

        self.assertEqual(item.state, "memory_pending")

    def test_v2_fact_recording_failure_remains_retryable(self):
        def reviewer(_session_id, _review_date, _version):
            item = plan.items[0]
            review = PaperDecisionReview(
                review_id=item.review_id,
                session_id=item.session_id,
                symbol=item.symbol,
                trade_date=plan.trade_date,
                review_date=item.review_date,
                horizon_days=item.horizon_days,
                action="BUY",
                entry_price=PriceReference(date=plan.trade_date, usd_price=100.0, source="coinbase"),
                review_price=PriceReference(date=item.review_date, usd_price=110.0, source="coinbase"),
                raw_return_pct=10.0,
                verdict="correct",
                created_at=utc_now(),
                hermes_memory_entry="legacy lesson",
            )
            return {"ok": True, "data": {"review": review.model_dump(mode="json")}}

        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            plan = store.create_or_load(archived_batch(workflow_version=2))
            report = process_due_reviews(
                store,
                date(2026, 8, 7),
                reviewer,
                fact_recorder=lambda _review: (_ for _ in ()).throw(OSError("private")),
            )
            item = store.find_item(plan.items[0].review_id)[1]

        self.assertEqual(item.state, "review_pending")
        self.assertEqual(item.last_error_code, "REPORT_FACT_WRITE_FAILED")
        self.assertEqual(report.retryable_count, 1)
        self.assertEqual(report.report_fact_count, 0)

    def test_v2_later_horizon_waits_for_same_session_and_other_sessions_continue(self):
        session_ids = {
            "BTC": SESSION_IDS["BTC"],
            "ETH": SESSION_IDS["ETH"],
        }
        calls = []
        fail_btc_t1 = True

        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = ScheduledReviewStore(root / "review_schedules")
            report_store = ReportLearningStore(root / "report_memories")
            batch = archived_batch(
                session_ids=session_ids,
                workflow_version=2,
            )
            plan = store.create_or_load(batch)
            sessions = {
                item.session_id: AnalysisSession(
                    session_id=item.session_id,
                    status="completed",
                    created_at=utc_now(),
                    completed_at=utc_now(),
                    request=batch.request.for_symbol(item.symbol),
                    result=AnalysisResult(
                        reports={"market": "Archived market evidence."},
                        investment_plan="plan",
                        trader_investment_plan="trader plan",
                        final_trade_decision="FINAL TRANSACTION PROPOSAL: BUY",
                        processed_signal="BUY",
                    ),
                )
                for item in batch.items
            }

            def reviewer(session_id, review_date, _version):
                nonlocal fail_btc_t1
                item = next(
                    candidate
                    for candidate in plan.items
                    if candidate.session_id == session_id
                    and candidate.review_date == review_date
                )
                calls.append((item.symbol, item.horizon_days))
                if item.symbol == "BTC" and item.horizon_days == 1 and fail_btc_t1:
                    return {"ok": False, "error": {"code": "PRICE_DATA_UNAVAILABLE"}}
                review = PaperDecisionReview(
                    review_id=item.review_id,
                    session_id=item.session_id,
                    symbol=item.symbol,
                    trade_date=plan.trade_date,
                    review_date=item.review_date,
                    horizon_days=item.horizon_days,
                    action="BUY",
                    entry_price=PriceReference(
                        date=plan.trade_date, usd_price=100.0, source="coinbase"
                    ),
                    review_price=PriceReference(
                        date=item.review_date, usd_price=110.0, source="coinbase"
                    ),
                    raw_return_pct=10.0,
                    verdict="correct",
                    created_at=utc_now(),
                    hermes_memory_entry=(
                        f"Exact {item.symbol} T+{item.horizon_days} lesson."
                    ),
                )
                return {"ok": True, "data": {"review": review.model_dump(mode="json")}}

            def fact_recorder(review):
                record_review_fact(report_store, sessions[review.session_id], review)

            process_due_reviews(
                store, date(2026, 8, 14), reviewer, fact_recorder=fact_recorder
            )
            first_calls = list(calls)
            first_btc = [
                item
                for item in store.load(plan.trade_date).items
                if item.symbol == "BTC"
            ]
            first_eth = [
                item
                for item in store.load(plan.trade_date).items
                if item.symbol == "ETH"
            ]

            fail_btc_t1 = False
            calls.clear()
            process_due_reviews(
                store, date(2026, 8, 14), reviewer, fact_recorder=fact_recorder
            )
            second_calls = list(calls)
            btc_record = report_store.load(session_ids["BTC"])

        self.assertEqual(
            [(item.state, item.last_error_code) for item in first_eth],
            [("completed", None), ("completed", None), ("review_pending", None)],
        )
        self.assertEqual(first_calls, [("BTC", 1), ("ETH", 1), ("ETH", 7)])
        self.assertEqual([item.state for item in first_btc], ["review_pending"] * 3)
        self.assertEqual(second_calls, [("BTC", 1), ("BTC", 7)])
        self.assertEqual(
            [outcome.horizon_days for outcome in btc_record.outcomes], [1, 7]
        )

    def test_state_conflict_does_not_overwrite_external_transition(self):
        def reviewer(_session_id, _review_date, _version):
            item = plan.items[0]
            store.transition_item(
                item.review_id,
                "review_pending",
                state="attention_required",
                last_error_code="EXTERNAL_REVIEW",
                updated_at=utc_now(),
            )
            review = PaperDecisionReview(
                review_id=item.review_id,
                session_id=item.session_id,
                symbol=item.symbol,
                trade_date=plan.trade_date,
                review_date=item.review_date,
                horizon_days=item.horizon_days,
                action="BUY",
                entry_price=PriceReference(date=plan.trade_date, usd_price=100.0, source="coinbase"),
                review_price=PriceReference(date=item.review_date, usd_price=110.0, source="coinbase"),
                raw_return_pct=10.0,
                verdict="correct",
                created_at=utc_now(),
                hermes_memory_entry="legacy lesson",
            )
            return {"ok": True, "data": {"review": review.model_dump(mode="json")}}

        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            plan = store.create_or_load(
                archived_batch(session_ids={"BTC": SESSION_IDS["BTC"]})
            )
            report = process_due_reviews(store, date(2026, 8, 7), reviewer)
            item = store.find_item(plan.items[0].review_id)[1]

        self.assertEqual(item.state, "attention_required")
        self.assertEqual(item.last_error_code, "EXTERNAL_REVIEW")
        self.assertEqual(report.retryable_count, 1)

    def test_non_skipped_item_requires_session_and_review_ids(self):
        with self.assertRaises(ValidationError):
            ScheduledReviewItem(
                symbol="BTC",
                horizon_days=1,
                review_date="2026-08-06",
                state="review_pending",
                updated_at=utc_now(),
            )

    def test_ready_batch_creates_three_horizons_per_symbol(self):
        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")

            plan = store.create_or_load(archived_batch())
            loaded = store.load(date(2026, 8, 5))

        self.assertEqual(plan, loaded)
        self.assertEqual(len(plan.items), 9)
        self.assertEqual(
            [(item.symbol, item.horizon_days) for item in plan.items],
            [
                (symbol, horizon)
                for symbol in ("BTC", "ETH", "SOL")
                for horizon in (1, 7, 15)
            ],
        )
        self.assertEqual(
            [item.review_date for item in plan.items[:3]],
            [date(2026, 8, 6), date(2026, 8, 12), date(2026, 8, 20)],
        )
        self.assertTrue(all(item.state == "review_pending" for item in plan.items))

    def test_degraded_batch_creates_skipped_horizons_for_failed_symbol(self):
        statuses = {"BTC": "completed", "ETH": "failed", "SOL": "completed"}
        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")

            plan = store.create_or_load(archived_batch(statuses))

        eth_items = [item for item in plan.items if item.symbol == "ETH"]
        self.assertEqual([item.state for item in eth_items], ["skipped"] * 3)
        self.assertEqual(
            [item.skip_reason for item in eth_items], ["ANALYSIS_FAILED"] * 3
        )

    def test_review_date_must_be_fully_elapsed(self):
        calls = []

        def reviewer(session_id, review_date, _version):
            calls.append((session_id, review_date))
            plan = store.load(date(2026, 8, 5))
            item = next(
                candidate
                for candidate in plan.items
                if candidate.session_id == session_id
                and candidate.review_date == review_date
            )
            review = PaperDecisionReview(
                review_id=item.review_id,
                session_id=session_id,
                symbol=item.symbol,
                trade_date=plan.trade_date,
                review_date=review_date,
                horizon_days=item.horizon_days,
                action="BUY",
                entry_price=PriceReference(
                    date=plan.trade_date, usd_price=100.0, source="coinbase"
                ),
                review_price=PriceReference(
                    date=review_date, usd_price=110.0, source="coinbase"
                ),
                raw_return_pct=10.0,
                verdict="correct",
                created_at=utc_now(),
                hermes_memory_entry=f"Exact {item.symbol} T+{item.horizon_days} lesson.",
            )
            return {
                "ok": True,
                "data": {"review": review.model_dump(mode="json")},
            }

        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            store.create_or_load(archived_batch())

            same_day = process_due_reviews(store, date(2026, 8, 6), reviewer)
            next_day = process_due_reviews(store, date(2026, 8, 7), reviewer)
            plan = store.load(date(2026, 8, 5))

        self.assertEqual(same_day.reviewed_count, 0)
        self.assertEqual(next_day.reviewed_count, 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            [item.state for item in plan.items if item.horizon_days == 1],
            ["memory_pending"] * 3,
        )
        self.assertTrue(
            all(
                item.state == "review_pending"
                for item in plan.items
                if item.horizon_days != 1
            )
        )

    def test_same_review_date_is_processed_by_trade_date_then_symbol(self):
        later_session_ids = {
            "BTC": "hermes_0000000000000011",
            "ETH": "hermes_0000000000000012",
            "SOL": "hermes_0000000000000013",
        }
        calls = []

        def reviewer(session_id, review_date, _version):
            calls.append((session_id, review_date))
            item = next(
                candidate
                for plan in store.plans()
                for candidate in plan.items
                if candidate.session_id == session_id
                and candidate.review_date == review_date
            )
            return {
                "ok": True,
                "data": {
                    "review": {
                        "review_id": item.review_id,
                        "session_id": session_id,
                        "review_date": review_date.isoformat(),
                    }
                },
            }

        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            store.create_or_load(archived_batch())
            store.create_or_load(
                archived_batch(
                    trade_date=date(2026, 8, 11),
                    batch_id="report_1123456789abcdef",
                    session_ids=later_session_ids,
                )
            )

            process_due_reviews(store, date(2026, 8, 13), reviewer)

        same_date_sessions = [
            session_id
            for session_id, review_date in calls
            if review_date == date(2026, 8, 12)
        ]
        self.assertEqual(
            same_date_sessions,
            [*SESSION_IDS.values(), *later_session_ids.values()],
        )

    def test_price_failure_remains_retryable(self):
        def failing_reviewer(_session_id, _review_date, _version):
            return {
                "ok": False,
                "error": {"code": "PRICE_DATA_UNAVAILABLE"},
            }

        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            store.create_or_load(archived_batch())

            report = process_due_reviews(
                store, date(2026, 8, 7), failing_reviewer
            )
            item = store.load(date(2026, 8, 5)).items[0]

        self.assertEqual(report.retryable_count, 3)
        self.assertEqual(item.state, "review_pending")
        self.assertEqual(item.attempt_count, 1)
        self.assertEqual(item.last_error_code, "PRICE_DATA_UNAVAILABLE")

    def test_successful_review_with_wrong_horizon_requires_attention(self):
        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            plan = store.create_or_load(archived_batch())
            item = plan.items[0]
            mismatched_review = PaperDecisionReview(
                review_id=item.review_id,
                session_id=item.session_id,
                symbol=item.symbol,
                trade_date=plan.trade_date,
                review_date=item.review_date,
                horizon_days=7,
                action="BUY",
                entry_price=PriceReference(
                    date=plan.trade_date, usd_price=100.0, source="coinbase"
                ),
                review_price=PriceReference(
                    date=item.review_date, usd_price=110.0, source="coinbase"
                ),
                raw_return_pct=10.0,
                verdict="correct",
                created_at=utc_now(),
                hermes_memory_entry="Mismatched scheduled lesson.",
            )

            process_due_reviews(
                store,
                date(2026, 8, 7),
                lambda _session_id, _review_date, _version: {
                    "ok": True,
                    "data": {
                        "review": mismatched_review.model_dump(mode="json")
                    },
                },
            )
            persisted = store.load(plan.trade_date)

        persisted_item = next(
            candidate for candidate in persisted.items if candidate.review_id == item.review_id
        )
        self.assertEqual(persisted_item.state, "attention_required")
        self.assertEqual(
            persisted_item.last_error_code, "REVIEW_IDENTITY_MISMATCH"
        )

    def test_pending_memory_is_canonical_and_bounded(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = ScheduledReviewStore(root / "review_schedules")
            review_store = ReviewStore(root / "reviews")
            plan = store.create_or_load(archived_batch())
            item = plan.items[0]
            review = PaperDecisionReview(
                review_id=item.review_id,
                session_id=item.session_id,
                symbol=item.symbol,
                trade_date=plan.trade_date,
                review_date=item.review_date,
                horizon_days=item.horizon_days,
                action="BUY",
                entry_price=PriceReference(
                    date=plan.trade_date, usd_price=100.0, source="coinbase"
                ),
                review_price=PriceReference(
                    date=item.review_date, usd_price=110.0, source="coinbase"
                ),
                raw_return_pct=10.0,
                verdict="correct",
                created_at=utc_now(),
                hermes_memory_entry="Exact scheduled BTC T+1 lesson.",
            )
            review_store.save(review)
            store.update_item(
                item.review_id, state="memory_pending", updated_at=utc_now()
            )

            work = list_pending_memory(store, review_store.load, limit=1)

        self.assertEqual(len(work), 1)
        self.assertEqual(work[0].review_id, item.review_id)
        self.assertEqual(
            work[0].hermes_memory_entry, "Exact scheduled BTC T+1 lesson."
        )

    def test_pending_memory_rejects_limit_above_agent_bound(self):
        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")

            with self.assertRaises(ValueError):
                list_pending_memory(store, lambda _review_id: None, limit=19)

    def test_invalid_oldest_memory_item_does_not_block_later_valid_item(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = ScheduledReviewStore(root / "review_schedules")
            review_store = ReviewStore(root / "reviews")
            plan = store.create_or_load(archived_batch())
            invalid_item = plan.items[0]
            valid_item = plan.items[3]
            for item in (invalid_item, valid_item):
                store.update_item(
                    item.review_id, state="memory_pending", updated_at=utc_now()
                )
            invalid_review = PaperDecisionReview(
                review_id=invalid_item.review_id,
                session_id=invalid_item.session_id,
                symbol=invalid_item.symbol,
                trade_date=plan.trade_date,
                review_date=invalid_item.review_date,
                horizon_days=7,
                action="BUY",
                entry_price=PriceReference(
                    date=plan.trade_date, usd_price=100.0, source="coinbase"
                ),
                review_price=PriceReference(
                    date=invalid_item.review_date,
                    usd_price=110.0,
                    source="coinbase",
                ),
                raw_return_pct=10.0,
                verdict="correct",
                created_at=utc_now(),
                hermes_memory_entry="Invalid oldest lesson.",
            )
            valid_review = invalid_review.model_copy(
                update={
                    "review_id": valid_item.review_id,
                    "session_id": valid_item.session_id,
                    "symbol": valid_item.symbol,
                    "review_date": valid_item.review_date,
                    "horizon_days": valid_item.horizon_days,
                    "review_price": PriceReference(
                        date=valid_item.review_date,
                        usd_price=110.0,
                        source="coinbase",
                    ),
                    "hermes_memory_entry": "Valid later lesson.",
                }
            )
            review_store.save(invalid_review)
            review_store.save(valid_review)

            work = list_pending_memory(store, review_store.load, limit=1)

        self.assertEqual([item.review_id for item in work], [valid_item.review_id])

    def test_confirmation_completes_only_after_successful_verification(self):
        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            plan = store.create_or_load(archived_batch())
            item = plan.items[0]
            store.update_item(
                item.review_id, state="memory_pending", updated_at=utc_now()
            )

            confirmed = confirm_scheduled_memory(
                store, item.review_id, lambda review_id: review_id == item.review_id
            )

        self.assertEqual(confirmed.state, "completed")
        self.assertIsNotNone(confirmed.verified_at)

    def test_failed_confirmation_requires_attention(self):
        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            plan = store.create_or_load(archived_batch())
            item = plan.items[0]
            store.update_item(
                item.review_id, state="memory_pending", updated_at=utc_now()
            )

            with self.assertRaises(ScheduledReviewConfirmationError):
                confirm_scheduled_memory(
                    store,
                    item.review_id,
                    lambda _review_id: (_ for _ in ()).throw(
                        RuntimeError("memory mismatch details")
                    ),
                )
            quarantined = store.load(date(2026, 8, 5)).items[0]

        self.assertEqual(quarantined.state, "attention_required")
        self.assertEqual(
            quarantined.last_error_code, "REVIEW_CONSISTENCY_FAILED"
        )

    def test_stale_failed_confirmation_cannot_overwrite_completed(self):
        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            plan = store.create_or_load(archived_batch())
            item = plan.items[0]
            store.update_item(
                item.review_id, state="memory_pending", updated_at=utc_now()
            )

            def stale_verifier(review_id):
                confirm_scheduled_memory(store, review_id, lambda _review_id: True)
                raise RuntimeError("stale verifier failure")

            with self.assertRaises(ScheduledReviewConfirmationError):
                confirm_scheduled_memory(store, item.review_id, stale_verifier)
            persisted = store.find_item(item.review_id)[1]

        self.assertEqual(persisted.state, "completed")

    def test_stale_successful_confirmation_cannot_overwrite_attention(self):
        with TemporaryDirectory() as directory:
            store = ScheduledReviewStore(Path(directory) / "review_schedules")
            plan = store.create_or_load(archived_batch())
            item = plan.items[0]
            store.update_item(
                item.review_id, state="memory_pending", updated_at=utc_now()
            )

            def stale_verifier(review_id):
                with self.assertRaises(ScheduledReviewConfirmationError):
                    confirm_scheduled_memory(
                        store,
                        review_id,
                        lambda _review_id: (_ for _ in ()).throw(
                            RuntimeError("canonical verifier failure")
                        ),
                    )
                return True

            with self.assertRaises(ScheduledReviewConfirmationError):
                confirm_scheduled_memory(store, item.review_id, stale_verifier)
            persisted = store.find_item(item.review_id)[1]

        self.assertEqual(persisted.state, "attention_required")

    def test_t_plus_one_end_to_end_leaves_later_horizons_pending(self):
        with TemporaryDirectory() as directory:
            results_root = Path(directory) / "results"
            hermes_root = results_root / "hermes"
            store = ScheduledReviewStore(hermes_root / "review_schedules")
            review_store = ReviewStore(hermes_root / "reviews")
            learning_store = LearningStore(hermes_root / "memories")
            batch = archived_batch()
            store.create_or_load(batch)
            sessions = {
                item.session_id: AnalysisSession(
                    session_id=item.session_id,
                    status="completed",
                    created_at=utc_now(),
                    completed_at=utc_now(),
                    request=batch.request.for_symbol(item.symbol),
                    result=AnalysisResult(
                        reports={},
                        investment_plan="plan",
                        trader_investment_plan="trader plan",
                        final_trade_decision="FINAL TRANSACTION PROPOSAL: BUY",
                        processed_signal="BUY",
                    ),
                )
                for item in batch.items
            }

            def reviewer(session_id, review_date, _version):
                review = review_completed_session(
                    sessions[session_id],
                    review_date,
                    lambda _symbol, trade_date, observed_date: (
                        PriceReference(
                            date=trade_date, usd_price=100.0, source="coinbase"
                        ),
                        PriceReference(
                            date=observed_date, usd_price=110.0, source="coinbase"
                        ),
                    ),
                    review_store,
                    learning_store,
                    current_date=date(2026, 8, 7),
                )
                return {"ok": True, "data": {"review": review.model_dump(mode="json")}}

            process_report = process_due_reviews(
                store, date(2026, 8, 7), reviewer
            )
            work = list_pending_memory(store, review_store.load, limit=18)
            memory_path = Path(directory) / "MEMORY.md"
            memory_path.write_text(
                "# Memory\n\n" + "\n".join(item.hermes_memory_entry for item in work),
                encoding="utf-8",
            )
            for item in work:
                confirm_scheduled_memory(
                    store,
                    item.review_id,
                    lambda review_id: verify_review_consistency(
                        review_id, results_root, memory_path
                    ),
                )
            persisted = store.load(date(2026, 8, 5))
            review_count = len(list((hermes_root / "reviews").glob("review_*.json")))
            indexed_symbols = {
                symbol
                for symbol in ("BTC", "ETH", "SOL")
                if learning_store.load(symbol) is not None
            }

        self.assertEqual(process_report.reviewed_count, 3)
        self.assertEqual(len(work), 3)
        self.assertEqual(review_count, 3)
        self.assertEqual(indexed_symbols, {"BTC", "ETH", "SOL"})
        self.assertEqual(
            [item.state for item in persisted.items if item.horizon_days == 1],
            ["completed"] * 3,
        )
        self.assertTrue(
            all(
                item.state == "review_pending"
                for item in persisted.items
                if item.horizon_days in {7, 15}
            )
        )


if __name__ == "__main__":
    unittest.main()
