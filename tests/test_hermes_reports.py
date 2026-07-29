import unittest
from datetime import date
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory

from tradingagents.integrations.hermes_reports import (
    PAPER_TRADING_DISCLAIMER,
    ReportArchiveConflict,
    ReportBatchActive,
    ReportBatchConflict,
    ReportBatchStore,
)
from tradingagents.integrations.schemas import (
    AnalysisResult,
    AnalysisSession,
    DailyReportRequest,
    ToolError,
    utc_now,
)


SESSION_IDS = {
    "BTC": "hermes_0000000000000001",
    "ETH": "hermes_0000000000000002",
    "SOL": "hermes_0000000000000003",
}


class FakeStarter:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        self.calls.append(request.symbol)
        return SESSION_IDS[request.symbol]


class PartiallyFailingStarter(FakeStarter):
    def __call__(self, request):
        self.calls.append(request.symbol)
        if request.symbol == "ETH":
            return ToolError(
                code="WORKER_START_FAILED",
                message="The analysis worker could not be started.",
                suggested_action="Retry later.",
            )
        return SESSION_IDS[request.symbol]


def make_request(**updates):
    values = {
        "trade_date": "2026-07-29",
        "symbols": ["BTC", "ETH", "SOL"],
        "analysts": ["market", "news", "fundamentals"],
        "research_depth": 1,
        "llm_provider": "deepseek",
        "quick_model": "deepseek-v4-flash",
        "deep_model": "deepseek-v4-pro",
    }
    return DailyReportRequest(**{**values, **updates})


def make_store(directory):
    return ReportBatchStore(Path(directory) / "report_batches")


def make_session(symbol, status="completed", error=None):
    request = make_request(symbols=[symbol]).for_symbol(symbol)
    result = None
    if status == "completed":
        result = AnalysisResult(
            reports={"market": f"{symbol} market report"},
            investment_plan=f"{symbol} plan",
            trader_investment_plan=f"{symbol} trader plan",
            final_trade_decision=f"{symbol} decision",
            processed_signal=f"{symbol} signal",
        )
    return AnalysisSession(
        session_id=SESSION_IDS[symbol],
        status=status,
        created_at=utc_now(),
        completed_at=utc_now() if status in {"completed", "failed"} else None,
        request=request,
        result=result,
        error=error,
    )


class HermesReportsTests(unittest.TestCase):
    def test_create_or_load_returns_one_batch_for_matching_request(self):
        with TemporaryDirectory() as directory:
            store = make_store(directory)
            starter = FakeStarter()

            first = store.create_or_load(make_request(), starter)
            repeated = store.create_or_load(make_request(), starter)

        self.assertEqual(first.batch_id, repeated.batch_id)
        self.assertEqual(starter.calls, ["BTC", "ETH", "SOL"])

    def test_create_or_load_rejects_changed_request_for_same_trade_date(self):
        with TemporaryDirectory() as directory:
            store = make_store(directory)
            store.create_or_load(make_request(), FakeStarter())

            with self.assertRaises(ReportBatchConflict):
                store.create_or_load(make_request(research_depth=3), FakeStarter())

    def test_summary_is_active_until_every_session_is_terminal(self):
        with TemporaryDirectory() as directory:
            store = make_store(directory)
            batch = store.create_or_load(make_request(), FakeStarter())
            sessions = {
                SESSION_IDS["BTC"]: make_session("BTC"),
                SESSION_IDS["ETH"]: make_session("ETH", status="running"),
                SESSION_IDS["SOL"]: make_session("SOL"),
            }

            summary = store.summarize(batch, sessions.get)

        self.assertEqual(summary.state, "active")

    def test_summary_is_degraded_when_a_session_failed(self):
        with TemporaryDirectory() as directory:
            store = make_store(directory)
            batch = store.create_or_load(make_request(), FakeStarter())
            sessions = {
                SESSION_IDS["BTC"]: make_session("BTC"),
                SESSION_IDS["ETH"]: make_session(
                    "ETH",
                    status="failed",
                    error=ToolError(
                        code="ANALYSIS_FAILED",
                        message="The analysis could not be completed.",
                        suggested_action="Retry later.",
                    ),
                ),
                SESSION_IDS["SOL"]: make_session("SOL"),
            }

            summary = store.summarize(batch, sessions.get)

        self.assertEqual(summary.state, "degraded")

    def test_partial_submission_is_persisted_without_duplicate_starts(self):
        with TemporaryDirectory() as directory:
            store = make_store(directory)
            starter = PartiallyFailingStarter()

            batch = store.create_or_load(make_request(), starter)
            repeated = store.create_or_load(make_request(), starter)

        self.assertEqual(batch, repeated)
        self.assertEqual(starter.calls, ["BTC", "ETH", "SOL"])
        self.assertEqual(batch.items[1].submission_error.code, "WORKER_START_FAILED")

    def test_summary_marks_loader_error_as_unreadable(self):
        with TemporaryDirectory() as directory:
            store = make_store(directory)
            batch = store.create_or_load(make_request(symbols=["BTC"]), FakeStarter())

            def unreadable(_session_id):
                raise OSError("cannot read")

            summary = store.summarize(batch, unreadable)

        self.assertEqual(summary.state, "degraded")
        self.assertEqual(summary.items[0].status, "unreadable")
        self.assertEqual(summary.items[0].error.code, "SESSION_UNREADABLE")

    def test_archive_rejects_active_batch_without_creating_a_file(self):
        with TemporaryDirectory() as directory:
            store = make_store(directory)
            batch = store.create_or_load(make_request(symbols=["BTC"]), FakeStarter())
            sessions = {SESSION_IDS["BTC"]: make_session("BTC", status="running")}

            with self.assertRaises(ReportBatchActive):
                store.archive(batch, sessions.get, "Chinese narrative")

            report_path = Path(directory) / "reports" / "2026-07-29.md"
            self.assertFalse(report_path.exists())

    def test_archive_is_immutable_and_has_disclaimer_and_owner_only_mode(self):
        with TemporaryDirectory() as directory:
            store = make_store(directory)
            batch = store.create_or_load(make_request(symbols=["BTC"]), FakeStarter())
            sessions = {SESSION_IDS["BTC"]: make_session("BTC")}

            first = store.archive(batch, sessions.get, "Chinese narrative")
            repeated = store.archive(batch, sessions.get, "Chinese narrative")

            with self.assertRaises(ReportArchiveConflict):
                store.archive(batch, sessions.get, "Different narrative")

            text = first.path.read_text(encoding="utf-8")
            mode = S_IMODE(first.path.stat().st_mode)

        self.assertEqual(first.sha256, repeated.sha256)
        self.assertIn(PAPER_TRADING_DISCLAIMER, text)
        self.assertEqual(mode, 0o600)

    def test_archive_writes_degraded_report_after_terminal_failure(self):
        with TemporaryDirectory() as directory:
            store = make_store(directory)
            batch = store.create_or_load(make_request(symbols=["BTC"]), FakeStarter())
            sessions = {
                SESSION_IDS["BTC"]: make_session(
                    "BTC",
                    status="failed",
                    error=ToolError(
                        code="ANALYSIS_FAILED",
                        message="The analysis could not be completed.",
                        suggested_action="Retry later.",
                    ),
                )
            }

            archive = store.archive(batch, sessions.get, "Chinese narrative")
            text = archive.path.read_text(encoding="utf-8")

        self.assertEqual(archive.state, "degraded")
        self.assertIn("ANALYSIS_FAILED", text)

    def test_previous_snapshot_uses_latest_earlier_archived_batch(self):
        with TemporaryDirectory() as directory:
            store = make_store(directory)
            earlier = store.create_or_load(
                make_request(trade_date="2026-07-28", symbols=["BTC"]), FakeStarter()
            )
            sessions = {SESSION_IDS["BTC"]: make_session("BTC")}
            store.archive(earlier, sessions.get, "Earlier narrative")

            snapshot = store.previous_snapshot(date(2026, 7, 29))

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.trade_date, date(2026, 7, 28))
        self.assertEqual(snapshot.items[0].processed_signal, "BTC signal")


if __name__ == "__main__":
    unittest.main()
