import unittest
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


if __name__ == "__main__":
    unittest.main()
