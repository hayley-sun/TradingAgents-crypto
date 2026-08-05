import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from tradingagents.integrations.schemas import (
    AnalysisRequest,
    AnalysisResult,
    AnalysisSession,
    DailyReportBatch,
    DailyReportBatchItem,
    DailyReportArchive,
    DailyReportRequest,
    PaperDecisionReview,
    PriceReference,
    ReviewRequest,
    ReportCausalHypothesis,
    ReportLearningIndexEntry,
    ReportLearningRecord,
    ReportLearningRevision,
    ReportLearningOutcome,
    ReportOutcomeAssessment,
    ReportReflection,
    ReportSourceMetadata,
    ScheduledReviewPlan,
    SymbolLearningEntry,
    SymbolLearningIndex,
    ToolError,
    is_valid_review_id,
    is_valid_session_id,
    utc_now,
)


class HermesSchemaTests(unittest.TestCase):
    def test_hermes_modules_remain_compatible_with_python_310_datetime(self):
        project_root = Path(__file__).resolve().parents[1]
        for relative_path in (
            "tradingagents/dataflows/crypto_price_references.py",
            "tradingagents/integrations/hermes_maintenance.py",
        ):
            with self.subTest(relative_path=relative_path):
                text = (project_root / relative_path).read_text(encoding="ascii")
                self.assertNotIn("from datetime import UTC", text)
                self.assertNotIn("datetime.UTC", text)

    def test_deepseek_request_normalizes_values(self):
        request = AnalysisRequest(
            symbol="  btcusdt ",
            trade_date="2026-07-28",
            analysts=[" MARKET ", "Social"],
            research_depth=3,
            llm_provider=" DeepSeek ",
            quick_model="  quick-model ",
            deep_model=" deep-model ",
        )

        self.assertEqual(request.symbol, "BTCUSDT")
        self.assertEqual(request.trade_date, date(2026, 7, 28))
        self.assertEqual(request.analysts, ["market", "social"])
        self.assertEqual(request.llm_provider, "deepseek")
        self.assertEqual(request.quick_model, "quick-model")
        self.assertEqual(request.deep_model, "deep-model")

    def test_invalid_analyst_provider_and_depth_are_rejected(self):
        values = {
            "symbol": "BTC",
            "trade_date": "2026-07-28",
            "analysts": ["market"],
            "research_depth": 1,
            "llm_provider": "openai",
            "quick_model": "quick",
            "deep_model": "deep",
        }

        for field, invalid_value in (
            ("analysts", ["unknown"]),
            ("llm_provider", "unknown"),
            ("research_depth", 2),
        ):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                AnalysisRequest(**{**values, field: invalid_value})

    def test_non_string_analyst_is_rejected_with_validation_error(self):
        with self.assertRaises(ValidationError):
            AnalysisRequest(
                symbol="BTC",
                trade_date="2026-07-28",
                analysts=[{}],
                research_depth=1,
                llm_provider="openai",
                quick_model="quick",
                deep_model="deep",
            )

    def test_session_ids_reject_empty_and_path_traversal(self):
        for session_id in ("", "../hermes_0123456789abcdef", "hermes_", "hermes_ABCDEF0123456789"):
            self.assertFalse(is_valid_session_id(session_id))

    def test_session_id_shape_and_utc_now(self):
        self.assertTrue(is_valid_session_id("hermes_0123456789abcdef"))
        self.assertTrue(is_valid_session_id("hermes_" + "a" * 64))
        self.assertFalse(is_valid_session_id("hermes_" + "a" * 15))
        self.assertFalse(is_valid_session_id("hermes_" + "g" * 16))

        current = utc_now()
        self.assertIsNotNone(current.tzinfo)
        self.assertEqual(current.utcoffset(), timezone.utc.utcoffset(current))

    def test_review_models_normalize_and_reject_invalid_values(self):
        request = ReviewRequest(
            session_id="hermes_0123456789abcdef",
            review_date="2026-07-29",
        )
        entry_price = PriceReference(
            date="2026-07-28", usd_price=100.0, source="coingecko"
        )
        review_price = PriceReference(
            date="2026-07-29", usd_price=110.0, source="cryptocompare"
        )
        review = PaperDecisionReview(
            review_id="review_0123456789abcdef",
            session_id=request.session_id,
            symbol=" btc ",
            trade_date="2026-07-28",
            review_date=request.review_date,
            action="BUY",
            entry_price=entry_price,
            review_price=review_price,
            raw_return_pct=10.0,
            verdict="correct",
            created_at=utc_now(),
            hermes_memory_entry="Paper-trading research lesson for BTC.",
        )
        index = SymbolLearningIndex(
            symbol=" btc ",
            updated_at=utc_now(),
            entries=[
                SymbolLearningEntry(
                    review_id=review.review_id,
                    review_date=review.review_date,
                    lesson=review.hermes_memory_entry,
                )
            ],
        )

        self.assertEqual(request.review_date, date(2026, 7, 29))
        self.assertTrue(is_valid_review_id(review.review_id))
        self.assertEqual(review.symbol, "BTC")
        self.assertEqual(review.review_price.source, "cryptocompare")
        self.assertEqual(index.symbol, "BTC")
        self.assertEqual(index.entries[0].review_id, review.review_id)

        with self.assertRaises(ValidationError):
            ReviewRequest(session_id="../session", review_date="2026-07-29")
        with self.assertRaises(ValidationError):
            PriceReference(date="2026-07-29", usd_price=0, source="coingecko")
        with self.assertRaises(ValidationError):
            PriceReference(date="2026-07-29", usd_price=1, source="unknown")
        with self.assertRaises(ValidationError):
            PaperDecisionReview.model_validate(
                {
                    **review.model_dump(),
                    "review_id": "../review",
                    "raw_return_pct": float("nan"),
                }
            )

    def test_models_forbid_extra_fields(self):
        with self.assertRaises(ValidationError):
            AnalysisRequest(
                symbol="BTC",
                trade_date="2026-07-28",
                analysts=["market"],
                research_depth=1,
                llm_provider="openai",
                quick_model="quick",
                deep_model="deep",
                extra="forbidden",
            )

    def test_session_and_result_models(self):
        request = AnalysisRequest(
            symbol="BTC",
            trade_date="2026-07-28",
            analysts=["market"],
            research_depth=1,
            llm_provider="ollama",
            quick_model="quick",
            deep_model="deep",
        )
        result = AnalysisResult(
            reports={"market": "report"},
            investment_plan="plan",
            trader_investment_plan="trader plan",
            final_trade_decision="buy",
            processed_signal="bullish",
        )
        session = AnalysisSession(
            session_id="hermes_0123456789abcdef",
            request=request,
            result=result,
            created_at=datetime.now(timezone.utc),
        )

        self.assertEqual(session.schema_version, 1)
        self.assertEqual(session.status, "running")
        self.assertIsNone(session.completed_at)

    def test_queued_session_tracks_worker_metadata(self):
        request = AnalysisRequest(
            symbol="BTC",
            trade_date="2026-07-28",
            analysts=["market"],
            research_depth=1,
            llm_provider="ollama",
            quick_model="quick",
            deep_model="deep",
        )

        session = AnalysisSession(
            session_id="hermes_0123456789abcdef",
            request=request,
            created_at=datetime.now(timezone.utc),
            status="queued",
        )

        self.assertEqual(session.status, "queued")
        self.assertIsNone(session.started_at)
        self.assertIsNone(session.worker_pid)

    def test_tool_error_has_string_fields(self):
        error = ToolError(code="bad_request", message="Invalid request", suggested_action="Retry")
        self.assertEqual(error.code, "bad_request")

    def test_daily_report_request_normalizes_unique_symbols(self):
        request = DailyReportRequest(
            trade_date="2026-07-29",
            symbols=[" btc ", "ETH", "sol"],
            analysts=[" MARKET ", "news", "fundamentals"],
            research_depth=1,
            llm_provider=" DeepSeek ",
            quick_model=" deepseek-v4-flash ",
            deep_model=" deepseek-v4-pro ",
        )

        self.assertEqual(request.trade_date, date(2026, 7, 29))
        self.assertEqual(request.symbols, ["BTC", "ETH", "SOL"])
        self.assertEqual(request.analysts, ["market", "news", "fundamentals"])
        self.assertEqual(request.llm_provider, "deepseek")

    def test_daily_report_request_rejects_duplicate_symbols_and_extra_fields(self):
        values = {
            "trade_date": "2026-07-29",
            "symbols": ["BTC", "btc"],
            "analysts": ["market"],
            "research_depth": 1,
            "llm_provider": "deepseek",
            "quick_model": "quick",
            "deep_model": "deep",
        }

        with self.assertRaises(ValidationError):
            DailyReportRequest(**values)
        with self.assertRaises(ValidationError):
            DailyReportRequest(**{**values, "symbols": ["BTC"], "extra": True})

    def test_daily_report_batch_item_requires_exactly_one_outcome(self):
        with self.assertRaises(ValidationError):
            DailyReportBatchItem(symbol="BTC")
        with self.assertRaises(ValidationError):
            DailyReportBatchItem(
                symbol="BTC",
                session_id="hermes_0123456789abcdef",
                submission_error=ToolError(
                    code="WORKER_START_FAILED",
                    message="The analysis worker could not be started.",
                    suggested_action="Retry later.",
                ),
            )

        item = DailyReportBatchItem(
            symbol=" btc ", session_id="hermes_0123456789abcdef"
        )
        self.assertEqual(item.symbol, "BTC")

    def test_daily_report_batch_allows_empty_items_while_submission_starts(self):
        request = DailyReportRequest(
            trade_date="2026-07-29",
            symbols=["BTC"],
            analysts=["market"],
            research_depth=1,
            llm_provider="deepseek",
            quick_model="quick",
            deep_model="deep",
        )

        batch = DailyReportBatch(
            batch_id="report_0123456789abcdef",
            request=request,
            created_at=utc_now(),
            items=[],
        )

        self.assertEqual(batch.items, [])

    def test_scheduled_review_versions_are_backward_compatible(self):
        archive_values = {
            "filename": "2026-08-05.md",
            "sha256": "a" * 64,
            "state": "ready",
            "archived_at": utc_now(),
            "items": [{"symbol": "BTC", "status": "completed"}],
        }
        self.assertIsNone(DailyReportArchive(**archive_values).scheduled_review_version)
        self.assertEqual(
            DailyReportArchive(**archive_values, scheduled_review_version=1)
            .scheduled_review_version,
            1,
        )
        self.assertEqual(
            DailyReportArchive(**archive_values, scheduled_review_version=2)
            .scheduled_review_version,
            2,
        )
        with self.assertRaises(ValidationError):
            DailyReportArchive(**archive_values, scheduled_review_version=3)

        plan = ScheduledReviewPlan(
            batch_id="report_0123456789abcdef",
            trade_date="2026-08-05",
            created_at=utc_now(),
            items=[],
        )
        self.assertEqual(plan.workflow_version, 1)
        old_plan_json = plan.model_dump_json(exclude={"workflow_version"})
        self.assertEqual(
            ScheduledReviewPlan.model_validate_json(old_plan_json).workflow_version,
            1,
        )
        self.assertEqual(
            ScheduledReviewPlan.model_validate(
                {**plan.model_dump(), "workflow_version": 2}
            ).workflow_version,
            2,
        )

    def test_report_learning_record_enforces_bounded_revision_snapshots(self):
        outcome = ReportLearningOutcome(
            review_id="review_0123456789abcdef",
            horizon_days=1,
            review_date="2026-08-06",
            raw_return_pct=1.25,
            verdict="correct",
        )
        reflection = ReportReflection(
            decision_thesis="Buy on breakout confirmation.",
            overall_assessment="The decision followed the strongest available evidence.",
            outcome_assessments=[
                ReportOutcomeAssessment(horizon_days=1, assessment="The T+1 move confirmed momentum.")
            ],
            reasoning_strengths=["The entry condition was explicit."],
            causal_hypotheses=[
                ReportCausalHypothesis(
                    statement="Momentum persisted after the breakout.",
                    evidence=["Price closed above resistance."],
                    confidence="high",
                )
            ],
            mistakes_or_missed_opportunities=["Position sizing could have been clearer."],
            next_decision_checks=["Confirm volume before entry."],
        )
        revision = ReportLearningRevision(
            revision=1,
            outcome_review_ids=[outcome.review_id],
            reflection_state="ready",
            memory_state="confirmed",
            source_fields=[ReportSourceMetadata(name="report", sha256="b" * 64, truncated=False)],
            reflection_attempt_count=0,
            reflection=reflection,
            lesson="Retain explicit breakout confirmation.",
            hermes_memory_entry="Retain explicit breakout confirmation.",
            created_at=utc_now(),
            updated_at=utc_now(),
            verified_at=utc_now(),
        )
        record = ReportLearningRecord(
            session_id="hermes_0123456789abcdef",
            symbol=" btc ",
            trade_date="2026-08-05",
            action="BUY",
            source_digest="c" * 64,
            desired_revision=1,
            reflected_revision=1,
            confirmed_revision=1,
            outcomes=[outcome],
            revisions=[revision],
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.assertEqual(record.symbol, "BTC")

        invalid_revisions = (
            {**revision.model_dump(), "revision": 4},
            {**revision.model_dump(), "outcome_review_ids": ["../review"]},
            {**revision.model_dump(), "source_fields": []},
            {**revision.model_dump(), "reflection_attempt_count": -1},
        )
        for invalid_revision in invalid_revisions:
            with self.subTest(invalid_revision=invalid_revision), self.assertRaises(ValidationError):
                ReportLearningRevision.model_validate(invalid_revision)
        with self.assertRaises(ValidationError):
            ReportReflection.model_validate({**reflection.model_dump(), "extra": True})
        with self.assertRaises(ValidationError):
            ReportLearningRecord.model_validate(
                {**record.model_dump(), "revisions": [revision.model_dump()] * 4}
            )

    def test_report_learning_defaults_and_optional_text_are_backward_compatible(self):
        outcome = ReportLearningOutcome(
            review_id="review_0123456789abcdef",
            horizon_days=1,
            review_date="2026-08-06",
            raw_return_pct=0.0,
            verdict="flat",
        )
        try:
            reflection = ReportReflection(
                decision_thesis="Wait for confirmation.",
                technical_context=" ",
                sentiment_context="",
                news_context=" ",
                fundamental_context="",
                overall_assessment="The decision was appropriately cautious.",
                outcome_assessments=[
                    {"horizon_days": 1, "assessment": "The first outcome was flat."}
                ],
                reasoning_strengths=[],
                causal_hypotheses=[
                    {
                        "statement": "The market lacked a catalyst.",
                        "evidence": ["Volume remained muted."],
                        "confidence": "low",
                    }
                ],
                mistakes_or_missed_opportunities=[],
                next_decision_checks=["Check volume."],
            )
            revision = ReportLearningRevision(
                revision=1,
                outcome_review_ids=[outcome.review_id],
                reflection_state="pending",
                memory_state="blocked",
                source_fields=[
                    ReportSourceMetadata(
                        name="report", sha256="f" * 64, truncated=False
                    )
                ],
                last_error_code="../diagnostic detail",
                reflection=reflection,
                lesson="",
                hermes_memory_entry=" ",
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            pending_revision = ReportLearningRevision(
                revision=1,
                outcome_review_ids=[outcome.review_id],
                reflection_state="pending",
                memory_state="blocked",
                source_fields=revision.source_fields,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            record = ReportLearningRecord(
                session_id="hermes_0123456789abcdef",
                symbol="BTC",
                trade_date="2026-08-05",
                action="HOLD",
                source_digest="a" * 64,
                desired_revision=1,
                outcomes=[outcome],
                revisions=[pending_revision],
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        except ValidationError as exc:
            self.fail(f"compatible report-learning payload was rejected: {exc}")

        self.assertEqual(revision.reflection_attempt_count, 0)
        self.assertEqual(record.reflected_revision, 0)
        self.assertEqual(record.confirmed_revision, 0)
        self.assertEqual(revision.last_error_code, "../diagnostic detail")
        self.assertEqual(revision.lesson, "")
        self.assertEqual(revision.hermes_memory_entry, " ")
        self.assertEqual(reflection.technical_context, " ")

    def test_report_learning_json_schema_exposes_string_bounds(self):
        source_name = ReportSourceMetadata.model_json_schema()["properties"]["name"]
        self.assertEqual(
            (source_name.get("minLength"), source_name.get("maxLength")), (1, 100)
        )

        hypothesis = ReportCausalHypothesis.model_json_schema()["properties"]
        self.assertEqual(
            (hypothesis["statement"]["minLength"], hypothesis["statement"]["maxLength"]),
            (1, 400),
        )
        self.assertEqual(
            (
                hypothesis["evidence"]["items"]["minLength"],
                hypothesis["evidence"]["items"]["maxLength"],
            ),
            (1, 100),
        )

        assessment = ReportOutcomeAssessment.model_json_schema()["properties"]["assessment"]
        self.assertEqual(
            (assessment["minLength"], assessment["maxLength"]), (1, 400)
        )

        reflection = ReportReflection.model_json_schema()["properties"]
        for field_name, maximum in (
            ("decision_thesis", 600),
            ("overall_assessment", 800),
        ):
            with self.subTest(field_name=field_name):
                self.assertEqual(reflection[field_name]["minLength"], 1)
                self.assertEqual(reflection[field_name]["maxLength"], maximum)
        for field_name in (
            "technical_context",
            "sentiment_context",
            "news_context",
            "fundamental_context",
        ):
            with self.subTest(field_name=field_name):
                text_schema = reflection[field_name]["anyOf"][0]
                self.assertNotIn("minLength", text_schema)
                self.assertEqual(text_schema["maxLength"], 600)
        for field_name in (
            "reasoning_strengths",
            "mistakes_or_missed_opportunities",
            "next_decision_checks",
        ):
            with self.subTest(field_name=field_name):
                self.assertEqual(reflection[field_name]["items"]["minLength"], 1)
                self.assertEqual(reflection[field_name]["items"]["maxLength"], 400)

        lesson = ReportLearningIndexEntry.model_json_schema()["properties"]["lesson"]
        self.assertEqual((lesson["minLength"], lesson["maxLength"]), (1, 6000))

        revision = ReportLearningRevision.model_json_schema()["properties"]
        for field_name, maximum in (
            ("last_error_code", 100),
            ("lesson", 6000),
            ("hermes_memory_entry", 6000),
        ):
            with self.subTest(field_name=field_name):
                text_schema = revision[field_name]["anyOf"][0]
                self.assertNotIn("minLength", text_schema)
                self.assertNotIn("pattern", text_schema)
                self.assertEqual(text_schema["maxLength"], maximum)

    def test_report_reflection_rejects_oversized_list_items(self):
        with self.assertRaises(ValidationError):
            ReportCausalHypothesis(
                statement="Momentum persisted.",
                evidence=["e" * 101],
                confidence="medium",
            )

        reflection_values = {
            "decision_thesis": "Wait for confirmation.",
            "overall_assessment": "The decision was appropriately cautious.",
            "outcome_assessments": [
                {"horizon_days": 1, "assessment": "The first outcome was flat."}
            ],
            "reasoning_strengths": ["s" * 401],
            "causal_hypotheses": [
                {
                    "statement": "The market lacked a catalyst.",
                    "evidence": ["Volume remained muted."],
                    "confidence": "low",
                }
            ],
            "mistakes_or_missed_opportunities": [],
            "next_decision_checks": ["Check volume."],
        }
        with self.assertRaises(ValidationError):
            ReportReflection(**reflection_values)

    def test_report_learning_record_requires_coherent_snapshots(self):
        outcomes = [
            ReportLearningOutcome(
                review_id="review_0123456789abcdef",
                horizon_days=1,
                review_date="2026-08-06",
                raw_return_pct=0.0,
                verdict="flat",
            ),
            ReportLearningOutcome(
                review_id="review_abcdef0123456789",
                horizon_days=7,
                review_date="2026-08-12",
                raw_return_pct=-1.0,
                verdict="incorrect",
            ),
            ReportLearningOutcome(
                review_id="review_1111111111111111",
                horizon_days=15,
                review_date="2026-08-20",
                raw_return_pct=2.0,
                verdict="correct",
            ),
        ]
        reflection = ReportReflection(
            decision_thesis="Wait for confirmation.",
            overall_assessment="The evidence became stronger over time.",
            outcome_assessments=[
                {"horizon_days": 1, "assessment": "The first outcome was flat."}
            ],
            reasoning_strengths=[],
            causal_hypotheses=[
                {
                    "statement": "Momentum developed gradually.",
                    "evidence": ["The longer horizon became positive."],
                    "confidence": "medium",
                }
            ],
            mistakes_or_missed_opportunities=[],
            next_decision_checks=["Check persistence."],
        )
        source_fields = [
            ReportSourceMetadata(name="report", sha256="d" * 64, truncated=False)
        ]
        now = utc_now()
        revisions = [
            ReportLearningRevision(
                revision=1,
                outcome_review_ids=[outcomes[0].review_id],
                reflection_state="ready",
                memory_state="confirmed",
                source_fields=source_fields,
                reflection=reflection,
                lesson="Initial lesson.",
                hermes_memory_entry="Initial memory.",
                created_at=now,
                updated_at=now,
                verified_at=now,
            ),
            ReportLearningRevision(
                revision=2,
                outcome_review_ids=[outcome.review_id for outcome in outcomes[:2]],
                reflection_state="ready",
                memory_state="verification_pending",
                source_fields=source_fields,
                reflection=reflection,
                lesson="Updated lesson.",
                hermes_memory_entry="Updated memory.",
                created_at=now,
                updated_at=now,
            ),
            ReportLearningRevision(
                revision=3,
                outcome_review_ids=[outcome.review_id for outcome in outcomes],
                reflection_state="attention_required",
                memory_state="blocked",
                source_fields=source_fields,
                created_at=now,
                updated_at=now,
            ),
        ]
        values = {
            "session_id": "hermes_0123456789abcdef",
            "symbol": "BTC",
            "trade_date": "2026-08-05",
            "action": "BUY",
            "source_digest": "e" * 64,
            "desired_revision": 3,
            "reflected_revision": 2,
            "confirmed_revision": 1,
            "outcomes": outcomes,
            "revisions": revisions,
            "created_at": now,
            "updated_at": now,
        }
        self.assertEqual(ReportLearningRecord(**values).reflected_revision, 2)

        invalid_histories = {
            "revision one contains two outcomes": [
                {
                    **revisions[0].model_dump(),
                    "outcome_review_ids": [
                        outcome.review_id for outcome in outcomes[:2]
                    ],
                },
                revisions[1],
                revisions[2],
            ],
            "outcome sets decrease": [
                revisions[0],
                revisions[1],
                {
                    **revisions[2].model_dump(),
                    "outcome_review_ids": [outcomes[0].review_id],
                },
            ],
            "reflected revision is pending without content": [
                revisions[0],
                {
                    **revisions[1].model_dump(),
                    "reflection_state": "pending",
                    "memory_state": "blocked",
                    "reflection": None,
                    "lesson": None,
                    "hermes_memory_entry": None,
                },
                revisions[2],
            ],
            "confirmed revision has unconfirmed state": [
                {
                    **revisions[0].model_dump(),
                    "memory_state": "add_pending",
                    "verified_at": None,
                },
                revisions[1],
                revisions[2],
            ],
            "unconfirmed revision is marked confirmed": [
                revisions[0],
                {
                    **revisions[1].model_dump(),
                    "memory_state": "confirmed",
                    "verified_at": now,
                },
                revisions[2],
            ],
            "unreflected revision contains reflection content": [
                revisions[0],
                revisions[1],
                {
                    **revisions[2].model_dump(),
                    "reflection_state": "ready",
                    "reflection": reflection.model_dump(),
                    "lesson": "Premature lesson.",
                    "hermes_memory_entry": "Premature memory.",
                },
            ],
        }
        for scenario, invalid_revisions in invalid_histories.items():
            with self.subTest(scenario=scenario), self.assertRaises(ValidationError):
                ReportLearningRecord(**{**values, "revisions": invalid_revisions})

        with self.assertRaises(ValidationError):
            ReportLearningRecord(
                **{
                    **values,
                    "desired_revision": 2,
                    "revisions": revisions[:2],
                }
            )

        with self.assertRaises(ValidationError):
            ReportLearningRecord(
                **{**values, "outcomes": [outcomes[1], outcomes[0], outcomes[2]]}
            )

    def test_symbol_learning_index_v2_separates_report_and_legacy_entries(self):
        report_entry = ReportLearningIndexEntry(
            session_id="hermes_0123456789abcdef",
            trade_date="2026-08-05",
            maturity_days=1,
            reflected_revision=1,
            updated_at=utc_now(),
            lesson="Retain explicit breakout confirmation.",
        )
        legacy_entry = SymbolLearningEntry(
            review_id="review_0123456789abcdef",
            review_date="2026-08-06",
            lesson="Legacy lesson.",
            session_id="hermes_0123456789abcdef",
        )
        index = SymbolLearningIndex(
            schema_version=2,
            symbol="btc",
            updated_at=utc_now(),
            entries=[],
            report_entries=[report_entry],
            legacy_entries=[legacy_entry],
        )
        self.assertEqual(index.symbol, "BTC")
        with self.assertRaises(ValidationError):
            SymbolLearningIndex(symbol="BTC", updated_at=utc_now())
        self.assertIn(
            "entries", SymbolLearningIndex.model_json_schema()["required"]
        )
        legacy_json = SymbolLearningIndex(
            symbol="BTC",
            updated_at=utc_now(),
            entries=[legacy_entry],
        ).model_dump_json(exclude={"report_entries", "legacy_entries"})
        self.assertEqual(
            SymbolLearningIndex.model_validate_json(legacy_json).schema_version,
            1,
        )
        with self.assertRaises(ValidationError):
            SymbolLearningIndex(
                **{**index.model_dump(), "entries": [legacy_entry]}
            )


if __name__ == "__main__":
    unittest.main()
