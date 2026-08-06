import importlib
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from tradingagents.integrations import hermes_scheduled_review_runner as runner
from tradingagents.integrations.hermes_learning import LearningStore, ReviewStore
from tradingagents.integrations.hermes_mcp import (
    SessionStore,
    review_paper_decision_impl as real_review_paper_decision_impl,
)
from tradingagents.integrations.hermes_report_learning import ReportLearningStore, record_review_fact
from tradingagents.integrations.hermes_scheduled_reviews import (
    ScheduledReviewProcessReport,
    ScheduledReviewStore,
)
from tradingagents.integrations.schemas import AnalysisResult, AnalysisSession, PriceReference, utc_now
from tests.test_hermes_scheduled_reviews import archived_batch
from tests.test_hermes_report_learning import completed_session, paper_review


class HermesScheduledReviewRunnerTests(unittest.TestCase):
    def test_v2_runner_writes_report_facts_without_legacy_learning(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "hermes"
            schedule_store = ScheduledReviewStore(root / "review_schedules")
            session_store = SessionStore(root / "sessions")
            review_store = ReviewStore(root / "reviews")
            report_store = ReportLearningStore(root / "report_memories")
            learning_store = LearningStore(root / "memories")
            batch = archived_batch(workflow_version=2)
            for batch_item in batch.items:
                session_store.save(
                    AnalysisSession(
                        session_id=batch_item.session_id,
                        status="completed",
                        created_at=utc_now(),
                        completed_at=utc_now(),
                        request=batch.request.for_symbol(batch_item.symbol),
                        result=AnalysisResult(
                            reports={},
                            investment_plan="plan",
                            trader_investment_plan="trader plan",
                            final_trade_decision="FINAL TRANSACTION PROPOSAL: BUY",
                            processed_signal="BUY",
                        ),
                    )
                )
            schedule_store.create_or_load(batch)
            legacy_flags = []

            def review_spy(request_data, **kwargs):
                legacy_flags.append(kwargs["write_legacy_learning"])
                return real_review_paper_decision_impl(
                    request_data,
                    price_reference_resolver=lambda _symbol, trade_date, review_date: (
                        PriceReference(date=trade_date, usd_price=100.0, source="coinbase"),
                        PriceReference(date=review_date, usd_price=110.0, source="coinbase"),
                    ),
                    **kwargs,
                )

            with patch.object(
                runner.ScheduledReviewStore, "from_environment", return_value=schedule_store
            ), patch.object(
                runner.SessionStore, "from_environment", return_value=session_store
            ), patch.object(
                runner.ReviewStore, "from_environment", return_value=review_store
            ), patch.object(
                runner.ReportLearningStore, "from_environment", return_value=report_store
            ), patch.object(runner, "review_paper_decision_impl", side_effect=review_spy):
                code, payload = runner.run_process_due(date(2026, 8, 7))

            self.assertEqual(code, 0)
            self.assertEqual(payload["report_fact_count"], 3)
            self.assertEqual(legacy_flags, [False, False, False])
            self.assertFalse(learning_store.root.exists())
            self.assertEqual(len(report_store.records()), 3)

    def test_default_processor_routes_v2_review_to_report_fact(self):
        schedule_store = object()
        review_store = object()
        report_store = object()
        session = object()
        session_store = SimpleNamespace(load=lambda _session_id: session)
        review = SimpleNamespace(session_id="hermes_0123456789abcdef")

        def process(_store, _date, reviewer, fact_recorder):
            reviewer(review.session_id, date(2026, 8, 6), 1)
            reviewer(review.session_id, date(2026, 8, 6), 2)
            fact_recorder(review)
            return ScheduledReviewProcessReport(2, 2, 0, 0, report_fact_count=1)

        with patch.object(
            runner.ScheduledReviewStore, "from_environment", return_value=schedule_store
        ), patch.object(
            runner.SessionStore, "from_environment", return_value=session_store
        ), patch.object(
            runner.ReviewStore, "from_environment", return_value=review_store
        ), patch.object(
            runner.ReportLearningStore, "from_environment", return_value=report_store
        ), patch.object(
            runner, "process_due_reviews", side_effect=process
        ), patch.object(
            runner, "review_paper_decision_impl", return_value={"ok": True}
        ) as review_impl, patch.object(
            runner, "record_review_fact"
        ) as fact_recorder:
            _code, payload = runner.run_process_due(date(2026, 8, 7))

        self.assertTrue(review_impl.call_args_list[0].kwargs["write_legacy_learning"])
        self.assertFalse(review_impl.call_args_list[1].kwargs["write_legacy_learning"])
        fact_recorder.assert_called_once_with(report_store, session, review)
        self.assertEqual(payload["report_fact_count"], 1)

    def test_run_process_due_returns_safe_counts(self):
        seen = []

        def processor(current_date):
            seen.append(current_date)
            return ScheduledReviewProcessReport(
                due_count=3,
                reviewed_count=2,
                retryable_count=1,
                skipped_count=0,
            )

        code, payload = runner.run_process_due(date(2026, 8, 7), processor)

        self.assertEqual(code, 0)
        self.assertEqual(seen, [date(2026, 8, 7)])
        self.assertEqual(payload["mode"], "process-due")
        self.assertEqual(payload["reviewed_count"], 2)
        self.assertEqual(payload["retryable_count"], 1)
        self.assertEqual(payload["report_fact_count"], 0)

    def test_main_redacts_unexpected_failure(self):
        stdout = io.StringIO()
        with patch.object(
            runner, "run_process_due", side_effect=OSError("/private/key path")
        ), redirect_stdout(stdout):
            code = runner.main(
                ["process-due", "--current-utc-date", "2026-08-07"]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(
            payload["error"]["code"], "SCHEDULED_REVIEW_RUNNER_FAILED"
        )
        self.assertNotIn("/private/key path", stdout.getvalue())

    def test_memory_pending_returns_exact_bounded_work(self):
        item = SimpleNamespace(
            trade_date=date(2026, 8, 5),
            review_date=date(2026, 8, 6),
            symbol="BTC",
            horizon_days=1,
            review_id="review_0123456789abcdef",
            hermes_memory_entry="Exact scheduled lesson.",
        )

        code, payload = runner.run_memory_pending(1, lambda limit: [item][:limit])

        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["items"][0]["hermes_memory_entry"],
            "Exact scheduled lesson.",
        )

    def test_memory_pending_surfaces_unavailable_items_and_keeps_valid_work(self):
        item = SimpleNamespace(
            trade_date=date(2026, 8, 5),
            review_date=date(2026, 8, 6),
            symbol="ETH",
            horizon_days=1,
            review_id="review_1123456789abcdef",
            hermes_memory_entry="Exact valid lesson.",
        )
        listing = SimpleNamespace(
            items=(item,),
            unavailable_count=1,
            unavailable_review_ids=("review_0123456789abcdef",),
        )

        code, payload = runner.run_memory_pending(1, lambda _limit: listing)

        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["unavailable_count"], 1)
        self.assertEqual(
            payload["unavailable_review_ids"], ["review_0123456789abcdef"]
        )

    def test_memory_pending_bounds_unavailable_id_sample(self):
        unavailable_review_ids = tuple(
            f"review_{offset:032x}" for offset in range(19)
        )
        listing = SimpleNamespace(
            items=(),
            unavailable_review_ids=unavailable_review_ids,
            unavailable_count=len(unavailable_review_ids),
        )

        code, payload = runner.run_memory_pending(18, lambda _limit: listing)

        self.assertEqual(code, 0)
        self.assertEqual(payload["unavailable_count"], 19)
        self.assertEqual(len(payload["unavailable_review_ids"]), 18)

    def test_confirm_memory_returns_only_project_state(self):
        seen = []

        def confirmer(review_id, memory_path):
            seen.append((review_id, memory_path))
            return SimpleNamespace(state="completed")

        memory_path = Path("/tmp/test-hermes-memory.md")
        code, payload = runner.run_confirm_memory(
            "review_0123456789abcdef", memory_path, confirmer
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            seen, [("review_0123456789abcdef", memory_path)]
        )
        self.assertEqual(
            payload,
            {
                "ok": True,
                "mode": "confirm-memory",
                "review_id": "review_0123456789abcdef",
                "state": "completed",
            },
        )

    def test_confirm_report_memory_accepts_only_patched_canonical_memory_path(self):
        session_id = "hermes_0123456789abcdef"
        seen = []

        with TemporaryDirectory() as directory:
            memory_path = Path(directory) / "MEMORY.md"
            with patch.object(
                runner, "HERMES_MEMORY_PATH", memory_path, create=True
            ):
                code, payload = runner.run_confirm_report_memory(
                    session_id,
                    1,
                    memory_path,
                    lambda selected_session, selected_path: (
                        seen.append((selected_session, selected_path))
                        or SimpleNamespace(
                            confirmed_revision=1,
                            revisions=[SimpleNamespace(memory_state="confirmed")],
                        )
                    ),
                )

        self.assertEqual(code, 0)
        self.assertEqual(seen, [(session_id, 1)])
        self.assertEqual(payload["memory_state"], "confirmed")

    def test_confirm_report_memory_rejects_noncanonical_path_before_confirmer(self):
        session_id = "hermes_0123456789abcdef"
        rejected_paths = (Path("/dev/null"), Path("/tmp/alternate-report-memory.md"))
        seen = []

        with TemporaryDirectory() as directory:
            canonical_path = Path(directory) / "MEMORY.md"
            with patch.object(
                runner, "HERMES_MEMORY_PATH", canonical_path, create=True
            ):
                for candidate_path in rejected_paths:
                    with self.subTest(path=candidate_path):
                        code, payload = runner.run_confirm_report_memory(
                            session_id,
                            1,
                            candidate_path,
                            lambda *_args: (
                                seen.append(candidate_path)
                                or SimpleNamespace(
                                    confirmed_revision=1,
                                    revisions=[
                                        SimpleNamespace(memory_state="confirmed")
                                    ]
                                )
                            ),
                        )
                        self.assertEqual(code, 1)
                        self.assertEqual(
                            payload["error"]["code"],
                            "INVALID_SCHEDULED_REVIEW_REQUEST",
                        )
                        self.assertNotIn(str(candidate_path), json.dumps(payload))

        self.assertEqual(seen, [])

    def test_main_rejects_noncanonical_report_memory_confirmation_path_before_runner(self):
        session_id = "hermes_0123456789abcdef"
        stdout = io.StringIO()
        with TemporaryDirectory() as directory:
            canonical_path = Path(directory) / "MEMORY.md"
            with patch.object(
                runner, "HERMES_MEMORY_PATH", canonical_path, create=True
            ), patch.object(runner, "run_confirm_report_memory") as confirmer, redirect_stdout(stdout):
                code = runner.main(
                    [
                        "confirm-report-memory",
                        "--session-id",
                        session_id,
                        "--revision",
                        "1",
                        "--hermes-memory-path",
                        "/dev/null",
                    ]
                )

        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST"
        )
        self.assertNotIn("/dev/null", stdout.getvalue())
        confirmer.assert_not_called()

    def test_main_rejects_noncanonical_date_as_safe_json(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = runner.main(
                ["process-due", "--current-utc-date", "20260807"]
            )

        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "INVALID_SCHEDULED_REVIEW_REQUEST",
        )

    def test_main_rejects_memory_limit_above_agent_bound(self):
        stdout = io.StringIO()
        with patch.object(runner, "run_memory_pending") as pending, redirect_stdout(
            stdout
        ):
            code = runner.main(["memory-pending", "--limit", "19"])

        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "INVALID_SCHEDULED_REVIEW_REQUEST",
        )
        pending.assert_not_called()

    def test_report_reflection_pending_is_metadata_only_and_bounded(self):
        with TemporaryDirectory() as directory:
            report_store = ReportLearningStore(Path(directory) / "reports")
            session = completed_session()
            record_review_fact(report_store, session, paper_review(1))
            with patch.object(runner.ReportLearningStore, "from_environment", return_value=report_store):
                code, payload = runner.run_report_reflection_pending(18)

        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            set(payload["items"][0]),
            {"session_id", "symbol", "trade_date", "revision", "maturity_days"},
        )
        self.assertNotIn("Market report.", json.dumps(payload))

    def test_report_reflection_pending_is_oldest_first_bounded_and_revision_ordered(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)

        def record(position):
            session_id = f"hermes_{99 - position:016x}"
            revision = SimpleNamespace(
                revision=1,
                reflection_state="pending",
                created_at=start + timedelta(minutes=position),
            )
            return SimpleNamespace(
                session_id=session_id,
                symbol=f"S{position:02d}",
                trade_date=date(2026, 7, 1),
                reflected_revision=0,
                revisions=[revision],
                outcomes=[SimpleNamespace(horizon_days=1)],
            )

        records = [record(position) for position in reversed(range(20))]
        code, payload = runner.run_report_reflection_pending(
            18, lambda _limit: records
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 18)
        self.assertEqual(
            [item["session_id"] for item in payload["items"]],
            [record(position).session_id for position in range(18)],
        )

        revisions = [
            SimpleNamespace(
                revision=revision,
                reflection_state="pending",
                created_at=start + timedelta(minutes=3 - revision),
            )
            for revision in (1, 2, 3)
        ]
        multi_revision = SimpleNamespace(
            session_id="hermes_aaaaaaaaaaaaaaaa",
            symbol="BTC",
            trade_date=date(2026, 7, 1),
            reflected_revision=0,
            revisions=revisions,
            outcomes=[SimpleNamespace(horizon_days=value) for value in (1, 7, 15)],
        )
        _, ordered = runner.run_report_reflection_pending(
            18, lambda _limit: [multi_revision]
        )
        self.assertEqual(
            [item["revision"] for item in ordered["items"]], [1, 2, 3]
        )

        multi_revision.revisions[0].reflection_state = "attention_required"
        _, blocked = runner.run_report_reflection_pending(
            18, lambda _limit: [multi_revision]
        )
        self.assertEqual(blocked["items"], [])

    def test_report_reflection_evidence_returns_one_selected_packet(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            report_store = ReportLearningStore(root / "reports")
            session_store = SessionStore(root / "sessions")
            session = completed_session()
            session_store.save(session)
            record_review_fact(report_store, session, paper_review(1))
            with patch.object(runner.ReportLearningStore, "from_environment", return_value=report_store), patch.object(runner.SessionStore, "from_environment", return_value=session_store):
                code, payload = runner.run_report_reflection_evidence(session.session_id, 1)

        self.assertEqual(code, 0)
        self.assertEqual(payload["mode"], "report-reflection-evidence")
        self.assertEqual(payload["packet"]["session_id"], session.session_id)
        self.assertEqual(payload["packet"]["revision"], 1)
        self.assertIn("fields", payload["packet"])

    def test_report_reflection_evidence_rejects_invalid_id_and_revision_before_store_access(self):
        loader = lambda: (_ for _ in ()).throw(AssertionError("store accessed"))
        code, payload = runner.run_report_reflection_evidence("../bad", 1, loader=loader)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST")
        code, payload = runner.run_report_reflection_evidence("hermes_0123456789abcdef", 4, loader=loader)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST")

    def test_report_reflection_pending_rejects_non_integral_limits_before_lister(self):
        def sentinel(_limit):
            raise AssertionError("lister accessed")

        for invalid_limit in (True, 1.0):
            with self.subTest(invalid_limit=invalid_limit):
                code, payload = runner.run_report_reflection_pending(invalid_limit, sentinel)
                self.assertEqual(code, 1)
                self.assertEqual(payload["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST")

    def test_report_reflection_evidence_rejects_bool_revision_before_loader(self):
        def sentinel():
            raise AssertionError("loader accessed")

        code, payload = runner.run_report_reflection_evidence(
            "hermes_0123456789abcdef", True, loader=sentinel
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST")

    def test_main_rejects_report_reflection_limit_above_bound(self):
        stdout = io.StringIO()
        with patch.object(runner, "run_report_reflection_pending") as pending, redirect_stdout(stdout):
            code = runner.main(["report-reflection-pending", "--limit", "19"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST")
        pending.assert_not_called()

    def test_report_reflection_pending_redacts_unexpected_failure(self):
        stdout = io.StringIO()
        with patch.object(runner, "run_report_reflection_pending", side_effect=OSError("/private/raw")), redirect_stdout(stdout):
            code = runner.main(["report-reflection-pending", "--limit", "1"])
        self.assertEqual(code, 1)
        self.assertNotIn("/private/raw", stdout.getvalue())

    def test_report_memory_retirement_pending_returns_metadata_only(self):
        item = SimpleNamespace(
            symbol="BTC",
            session_id="hermes_0123456789abcdef",
            trade_date=date(2026, 8, 1),
            revision=3,
            state="pending",
            marker="[TradingAgents paper report: hermes_0123456789abcdef]",
            old_text="private marker",
        )

        code, payload = runner.run_report_memory_retirement_pending(
            18, lambda _limit: [item]
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(
            payload["items"],
            [
                {
                    "symbol": "BTC",
                    "session_id": "hermes_0123456789abcdef",
                    "trade_date": "2026-08-01",
                    "revision": 3,
                    "state": "pending",
                }
            ],
        )
        rendered = json.dumps(payload)
        self.assertNotIn("marker", rendered)
        self.assertNotIn("old_text", rendered)
        self.assertNotIn("private marker", rendered)

    def test_report_memory_retirement_pending_rejects_invalid_limit_before_lister(self):
        def sentinel(_limit):
            raise AssertionError("lister accessed")

        for invalid_limit in (True, 0, 19, 1.0):
            with self.subTest(invalid_limit=invalid_limit):
                code, payload = runner.run_report_memory_retirement_pending(
                    invalid_limit, sentinel
                )
                self.assertEqual(code, 1)
                self.assertEqual(
                    payload["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST"
                )

    def test_begin_report_memory_retirement_returns_only_persisted_remove_marker(self):
        session_id = "hermes_0123456789abcdef"
        marker = f"[TradingAgents paper report: {session_id}]"
        seen = []

        def starter(symbol, selected_session_id):
            seen.append((symbol, selected_session_id))
            return SimpleNamespace(
                symbol=symbol,
                session_id=selected_session_id,
                trade_date=date(2026, 8, 1),
                revision=3,
                state="memory_call_started",
                action="remove",
                old_text=marker,
            )

        code, payload = runner.run_begin_report_memory_retirement(
            "btc", session_id, starter
        )

        self.assertEqual(seen, [("BTC", session_id)])
        self.assertEqual(code, 0)
        self.assertEqual(
            payload,
            {
                "ok": True,
                "mode": "begin-report-memory-retirement",
                "symbol": "BTC",
                "session_id": session_id,
                "trade_date": "2026-08-01",
                "revision": 3,
                "state": "memory_call_started",
                "action": "remove",
                "old_text": marker,
            },
        )

    def test_begin_report_memory_retirement_hides_marker_during_verification_retry(self):
        operation = SimpleNamespace(
            symbol="BTC",
            session_id="hermes_0123456789abcdef",
            trade_date=date(2026, 8, 1),
            revision=3,
            state="verification_pending",
            action="remove",
            old_text="[TradingAgents paper report: hermes_0123456789abcdef]",
        )

        code, payload = runner.run_begin_report_memory_retirement(
            "BTC", operation.session_id, lambda *_args: operation
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "verification_pending")
        self.assertEqual(payload["action"], "remove")
        self.assertNotIn("old_text", payload)

    def test_retirement_begin_rejects_invalid_symbol_or_session_before_starter(self):
        def sentinel(*_args):
            raise AssertionError("starter accessed")

        for symbol, session_id in (
            ("../BTC", "hermes_0123456789abcdef"),
            ("BTC", "hermes_not-valid"),
        ):
            with self.subTest(symbol=symbol, session_id=session_id):
                code, payload = runner.run_begin_report_memory_retirement(
                    symbol, session_id, sentinel
                )
                self.assertEqual(code, 1)
                self.assertEqual(
                    payload["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST"
                )

    def test_confirm_report_memory_retirement_returns_safe_state_and_uses_absence_verifier(self):
        session_id = "hermes_0123456789abcdef"
        retirement_store = object()
        confirmed = SimpleNamespace(
            symbol="BTC",
            session_id=session_id,
            revision=3,
            state="retired",
            marker="[TradingAgents paper report: hermes_0123456789abcdef]",
        )
        seen = []

        def confirmer(store, symbol, selected_session_id, verifier):
            seen.append((store, symbol, selected_session_id))
            verifier(selected_session_id, confirmed.marker)
            return confirmed

        with TemporaryDirectory() as directory:
            memory_path = Path(directory) / "MEMORY.md"
            with patch.object(
                runner, "HERMES_MEMORY_PATH", memory_path, create=True
            ), patch.object(
                runner, "ReportMemoryRetirementStore", return_value=retirement_store
            ), patch.object(
                runner, "confirm_report_memory_retirement", side_effect=confirmer
            ), patch.object(
                runner,
                "verify_report_memory_absence",
                return_value=SimpleNamespace(ok=True, marker_occurrences=0),
            ) as verifier:
                code, payload = runner.run_confirm_report_memory_retirement(
                    "BTC", session_id, memory_path
                )

        self.assertEqual(code, 0)
        self.assertEqual(seen, [(retirement_store, "BTC", session_id)])
        verifier.assert_called_once_with(
            session_id, confirmed.marker, memory_path.resolve()
        )
        self.assertEqual(
            payload,
            {
                "ok": True,
                "mode": "confirm-report-memory-retirement",
                "symbol": "BTC",
                "session_id": session_id,
                "revision": 3,
                "state": "retired",
            },
        )
        self.assertNotIn("marker", json.dumps(payload))

    def test_retirement_confirm_rejects_invalid_symbol_or_session_before_confirmer(self):
        def sentinel(*_args):
            raise AssertionError("confirmer accessed")

        for symbol, session_id in (
            ("BTC/ETH", "hermes_0123456789abcdef"),
            ("BTC", "hermes_bad"),
        ):
            with self.subTest(symbol=symbol, session_id=session_id):
                code, payload = runner.run_confirm_report_memory_retirement(
                    symbol, session_id, Path("/tmp/not-accessed"), sentinel
                )
                self.assertEqual(code, 1)
                self.assertEqual(
                    payload["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST"
                )

    def test_quarantine_report_memory_retirement_returns_safe_state(self):
        session_id = "hermes_0123456789abcdef"
        seen = []

        def quarantiner(symbol, selected_session_id, error_code):
            seen.append((symbol, selected_session_id, error_code))
            return SimpleNamespace(state="attention_required")

        code, payload = runner.run_quarantine_report_memory_retirement(
            "BTC", session_id, "MEMORY_REMOVE_FAILED", quarantiner
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            seen, [("BTC", session_id, "MEMORY_REMOVE_FAILED")]
        )
        self.assertEqual(
            payload,
            {
                "ok": True,
                "mode": "quarantine-report-memory-retirement",
                "symbol": "BTC",
                "session_id": session_id,
                "revision": 3,
                "state": "attention_required",
            },
        )

    def test_retirement_quarantine_rejects_external_error_before_store_access(self):
        def sentinel(*_args):
            raise AssertionError("quarantiner accessed")

        code, payload = runner.run_quarantine_report_memory_retirement(
            "BTC", "hermes_0123456789abcdef", "EXTERNAL_ERROR", sentinel
        )

        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST")

    def test_report_memory_capacity_returns_count_only_success_and_failure(self):
        with TemporaryDirectory() as directory:
            memory_path = Path(directory) / "private-memory.md"
            secret = "operator secret memory entry"
            memory_path.write_text(secret, encoding="utf-8")
            with patch.object(
                runner, "HERMES_MEMORY_PATH", memory_path, create=True
            ):
                code, payload = runner.run_report_memory_capacity(memory_path, 40000)

                self.assertEqual(code, 0)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["configured_limit"], 40000)
                self.assertEqual(payload["current_chars"], len(secret))
                self.assertEqual(payload["reserved_report_chars"], 30897)
                self.assertEqual(payload["available_chars"], 40000 - len(secret))
                rendered = json.dumps(payload)
                self.assertNotIn(secret, rendered)
                self.assertNotIn(str(memory_path), rendered)
                self.assertNotIn("memory_path", payload)

                memory_path.write_text("x" * 9001, encoding="utf-8")
                code, failed = runner.run_report_memory_capacity(memory_path, 40000)

        self.assertEqual(code, 0)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error_code"], "MEMORY_CAPACITY_EXCEEDED")
        self.assertNotIn("x" * 100, json.dumps(failed))

    def test_report_memory_capacity_rejects_wrong_limit_before_verifier_or_path_access(self):
        def sentinel(*_args):
            raise AssertionError("capacity verifier accessed")

        for invalid_limit in (True, 39999, 40001, 40000.0):
            with self.subTest(invalid_limit=invalid_limit):
                code, payload = runner.run_report_memory_capacity(
                    Path("/tmp/not-accessed"), invalid_limit, sentinel
                )
                self.assertEqual(code, 1)
                self.assertEqual(
                    payload["error"]["code"], "INVALID_SCHEDULED_REVIEW_REQUEST"
                )

    def test_report_memory_capacity_drops_unsafe_verifier_metadata(self):
        untrusted_result = SimpleNamespace(
            ok="truthy",
            current_chars="private memory text",
            reserved_report_chars=True,
            available_chars=-1,
            error_code="/private/path",
        )

        memory_path = Path("/tmp/not-accessed").resolve()
        with patch.object(
            runner, "HERMES_MEMORY_PATH", memory_path, create=True
        ):
            code, payload = runner.run_report_memory_capacity(
                memory_path, 40000, lambda *_args: untrusted_result
            )

        self.assertEqual(code, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["current_chars"], 0)
        self.assertEqual(payload["reserved_report_chars"], 0)
        self.assertEqual(payload["available_chars"], 0)
        self.assertIsNone(payload["error_code"])

    def test_retirement_commands_reject_noncanonical_memory_paths_before_dependencies(self):
        session_id = "hermes_0123456789abcdef"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            canonical_path = root / "canonical" / "MEMORY.md"
            alternate_path = root / "alternate-memory.md"
            symlink_path = root / "memory-link.md"
            alternate_path.write_text("alternate", encoding="utf-8")
            symlink_path.symlink_to(alternate_path)
            rejected_paths = (
                Path("/dev/null"),
                alternate_path,
                root / "nested" / ".." / "wrong-memory.md",
                symlink_path,
            )
            confirm_calls = []
            capacity_calls = []

            with patch.object(
                runner, "HERMES_MEMORY_PATH", canonical_path, create=True
            ):
                for candidate_path in rejected_paths:
                    with self.subTest(command="confirm", path=candidate_path):
                        code, payload = runner.run_confirm_report_memory_retirement(
                            "BTC",
                            session_id,
                            candidate_path,
                            lambda *_args: (
                                confirm_calls.append(candidate_path)
                                or SimpleNamespace(state="retired")
                            ),
                        )
                        self.assertEqual(code, 1)
                        self.assertEqual(
                            payload["error"]["code"],
                            "INVALID_SCHEDULED_REVIEW_REQUEST",
                        )
                        self.assertNotIn(str(candidate_path), json.dumps(payload))
                    with self.subTest(command="capacity", path=candidate_path):
                        code, payload = runner.run_report_memory_capacity(
                            candidate_path,
                            40000,
                            lambda *_args: capacity_calls.append(candidate_path),
                        )
                        self.assertEqual(code, 1)
                        self.assertEqual(
                            payload["error"]["code"],
                            "INVALID_SCHEDULED_REVIEW_REQUEST",
                        )
                        self.assertNotIn(str(candidate_path), json.dumps(payload))

            self.assertEqual(confirm_calls, [])
            self.assertEqual(capacity_calls, [])

    def test_main_rejects_noncanonical_retirement_memory_paths_before_runner_call(self):
        session_id = "hermes_0123456789abcdef"
        with TemporaryDirectory() as directory:
            canonical_path = Path(directory) / "canonical" / "MEMORY.md"
            routes = (
                (
                    [
                        "confirm-report-memory-retirement",
                        "--symbol",
                        "BTC",
                        "--session-id",
                        session_id,
                        "--hermes-memory-path",
                        "/dev/null",
                    ],
                    "run_confirm_report_memory_retirement",
                ),
                (
                    [
                        "report-memory-capacity",
                        "--hermes-memory-path",
                        "/dev/null",
                        "--memory-char-limit",
                        "40000",
                    ],
                    "run_report_memory_capacity",
                ),
            )
            with patch.object(
                runner, "HERMES_MEMORY_PATH", canonical_path, create=True
            ):
                for arguments, function_name in routes:
                    stdout = io.StringIO()
                    with self.subTest(arguments=arguments), patch.object(
                        runner, function_name
                    ) as command, redirect_stdout(stdout):
                        self.assertEqual(runner.main(arguments), 1)
                    self.assertEqual(
                        json.loads(stdout.getvalue())["error"]["code"],
                        "INVALID_SCHEDULED_REVIEW_REQUEST",
                    )
                    self.assertNotIn("/dev/null", stdout.getvalue())
                    command.assert_not_called()

    def test_main_routes_retirement_and_capacity_modes(self):
        session_id = "hermes_0123456789abcdef"
        with TemporaryDirectory() as directory:
            memory_path = Path(directory) / "hermes-memory.md"
            routes = (
                (
                    ["report-memory-retirement-pending", "--limit", "1"],
                    "run_report_memory_retirement_pending",
                    (1,),
                ),
                (
                    [
                        "begin-report-memory-retirement",
                        "--symbol",
                        "BTC",
                        "--session-id",
                        session_id,
                    ],
                    "run_begin_report_memory_retirement",
                    ("BTC", session_id),
                ),
                (
                    [
                        "confirm-report-memory-retirement",
                        "--symbol",
                        "BTC",
                        "--session-id",
                        session_id,
                        "--hermes-memory-path",
                        str(memory_path),
                    ],
                    "run_confirm_report_memory_retirement",
                    ("BTC", session_id, memory_path.resolve()),
                ),
                (
                    [
                        "quarantine-report-memory-retirement",
                        "--symbol",
                        "BTC",
                        "--session-id",
                        session_id,
                        "--error-code",
                        "MEMORY_REMOVE_FAILED",
                    ],
                    "run_quarantine_report_memory_retirement",
                    ("BTC", session_id, "MEMORY_REMOVE_FAILED"),
                ),
                (
                    [
                        "report-memory-capacity",
                        "--hermes-memory-path",
                        str(memory_path),
                        "--memory-char-limit",
                        "40000",
                    ],
                    "run_report_memory_capacity",
                    (memory_path.resolve(), 40000),
                ),
            )

            with patch.object(
                runner, "HERMES_MEMORY_PATH", memory_path, create=True
            ):
                for arguments, function_name, expected_args in routes:
                    with self.subTest(arguments=arguments), patch.object(
                        runner, function_name, return_value=(0, {"ok": True})
                    ) as command, redirect_stdout(io.StringIO()):
                        self.assertEqual(runner.main(arguments), 0)
                    command.assert_called_once_with(*expected_args)

    def test_bootstrap_loads_only_scheduled_review_environment(self):
        bootstrap = importlib.import_module(
            "tradingagents.integrations.hermes_scheduled_review_bootstrap"
        )
        config_text = """
mcp_servers:
  tradingagents_crypto:
    env:
      TRADINGAGENTS_RESULTS_DIR: /tmp/review-results
      DEEPSEEK_API_KEY: must-not-load
      COINGECKO_DEMO_API_KEY: coingecko-key
      CRYPTOCOMPARE_API_KEY: cryptocompare-key
      UNRELATED_VALUE: must-not-load
"""
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(config_text, encoding="utf-8")
            environment = {"EXISTING": "value"}

            loaded = bootstrap.load_scheduled_review_environment(
                config_path, environment
            )

        self.assertTrue(loaded)
        self.assertEqual(environment["EXISTING"], "value")
        self.assertEqual(
            environment["TRADINGAGENTS_RESULTS_DIR"], "/tmp/review-results"
        )
        self.assertEqual(environment["COINGECKO_DEMO_API_KEY"], "coingecko-key")
        self.assertEqual(
            environment["CRYPTOCOMPARE_API_KEY"], "cryptocompare-key"
        )
        self.assertNotIn("DEEPSEEK_API_KEY", environment)
        self.assertNotIn("UNRELATED_VALUE", environment)

    def test_bootstrap_rejects_config_without_results_directory(self):
        bootstrap = importlib.import_module(
            "tradingagents.integrations.hermes_scheduled_review_bootstrap"
        )
        config_text = """
mcp_servers:
  tradingagents_crypto:
    env:
      COINGECKO_DEMO_API_KEY: coingecko-key
"""
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(config_text, encoding="utf-8")
            environment = {}

            loaded = bootstrap.load_scheduled_review_environment(
                config_path, environment
            )

        self.assertFalse(loaded)
        self.assertEqual(environment, {})

    def test_bootstrap_stops_before_import_when_config_load_fails(self):
        bootstrap = importlib.import_module(
            "tradingagents.integrations.hermes_scheduled_review_bootstrap"
        )
        stdout = io.StringIO()
        with patch.object(
            bootstrap, "_load_default_environment", return_value=False
        ), patch.object(bootstrap, "import_module") as importer, redirect_stdout(stdout):
            code = bootstrap.main(["process-due"])

        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "SCHEDULED_REVIEW_RUNNER_FAILED",
        )
        importer.assert_not_called()

    def test_bootstrap_loads_environment_before_runner_import(self):
        bootstrap = importlib.import_module(
            "tradingagents.integrations.hermes_scheduled_review_bootstrap"
        )
        events = []

        class FakeRunner:
            @staticmethod
            def main(arguments):
                events.append(("runner", arguments))
                return 0

        with patch.object(
            bootstrap,
            "_load_default_environment",
            side_effect=lambda: events.append(("load", None)) or True,
        ), patch.object(
            bootstrap,
            "import_module",
            side_effect=lambda module: (
                events.append(("import", module)) or FakeRunner
            ),
        ):
            code = bootstrap.main(["memory-pending", "--limit", "1"])

        self.assertEqual(code, 0)
        self.assertEqual(
            events,
            [
                ("load", None),
                (
                    "import",
                    "tradingagents.integrations.hermes_scheduled_review_runner",
                ),
                ("runner", ["memory-pending", "--limit", "1"]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
