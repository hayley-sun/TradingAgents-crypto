import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone

from tradingagents.integrations import hermes_daily_report_runner as runner


class HermesDailyReportRunnerTests(unittest.TestCase):
    def test_shanghai_trade_date_converts_utc_before_selecting_day(self):
        instant = datetime(2026, 7, 30, 16, 30, tzinfo=timezone.utc)

        self.assertEqual(runner.shanghai_trade_date(instant), date(2026, 7, 31))

    def test_submit_uses_fixed_paper_research_request_once(self):
        captured = []

        def submit(request):
            captured.append(request)
            return {
                "ok": True,
                "data": {"batch": {"batch_id": "report_0000000000000000"}},
            }

        code, payload = runner.run_submit(date(2026, 7, 30), submit)

        self.assertEqual(code, 0)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["trade_date"], "2026-07-30")
        self.assertEqual(captured[0]["symbols"], ["BTC", "ETH", "SOL"])
        self.assertEqual(captured[0]["analysts"], ["market", "news", "fundamentals"])
        self.assertEqual(captured[0]["research_depth"], 1)
        self.assertEqual(captured[0]["llm_provider"], "deepseek")
        self.assertEqual(captured[0]["quick_model"], "deepseek-v4-flash")
        self.assertEqual(captured[0]["deep_model"], "deepseek-v4-pro")
        self.assertEqual(payload["batch_id"], "report_0000000000000000")

    def test_submit_returns_nonzero_and_safe_error(self):
        code, payload = runner.run_submit(
            date(2026, 7, 30),
            lambda _request: {
                "ok": False,
                "error": {"code": "REPORT_BATCH_UNREADABLE"},
            },
        )

        self.assertEqual(code, 1)
        self.assertEqual(payload, {
            "ok": False,
            "mode": "submit",
            "error": {"code": "REPORT_BATCH_UNREADABLE"},
        })

    def test_narrative_is_deterministic_and_chinese(self):
        summary = {
            "state": "ready",
            "items": [
                {
                    "symbol": "BTC",
                    "status": "completed",
                    "processed_signal": "BUY",
                    "final_trade_decision": "Buy on confirmation",
                    "error": None,
                }
            ],
        }

        first = runner.render_archive_narrative(summary, None)
        second = runner.render_archive_narrative(summary, None)

        self.assertEqual(first, second)
        self.assertIn("批次状态：ready", first)
        self.assertIn("BTC：状态 completed", first)
        self.assertIn("仅用于研究和模拟交易", first)

    def test_archive_active_returns_zero_without_calling_archive(self):
        archive_called = False

        def archive(_trade_date, _narrative):
            nonlocal archive_called
            archive_called = True
            return {"ok": True}

        code, payload = runner.run_archive(
            date(2026, 7, 30),
            lambda _trade_date: {
                "ok": True,
                "data": {
                    "summary": {"state": "active", "items": []},
                    "previous_report": None,
                },
            },
            archive,
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "active")
        self.assertFalse(archive_called)

    def test_archive_passes_one_deterministic_narrative_to_existing_archive(self):
        seen = []
        lookup = lambda _trade_date: {
            "ok": True,
            "data": {
                "summary": {
                    "state": "degraded",
                    "items": [
                        {
                            "symbol": "ETH",
                            "status": "failed",
                            "processed_signal": None,
                            "final_trade_decision": None,
                            "error": {"code": "ANALYSIS_FAILED"},
                        }
                    ],
                },
                "previous_report": None,
            },
        }

        def archive(trade_date, narrative):
            seen.append((trade_date, narrative))
            return {
                "ok": True,
                "data": {
                    "filename": "2026-07-30.md",
                    "sha256": "0" * 64,
                    "state": "degraded",
                },
            }

        code, payload = runner.run_archive(date(2026, 7, 30), lookup, archive)

        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "2026-07-30")
        self.assertIn("ANALYSIS_FAILED", seen[0][1])
        self.assertEqual(payload["filename"], "2026-07-30.md")

    def test_main_rejects_noncanonical_trade_date_as_safe_json(self):
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            code = runner.main(["submit", "--trade-date", "20260730"])

        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "INVALID_REPORT_REQUEST",
        )


if __name__ == "__main__":
    unittest.main()
