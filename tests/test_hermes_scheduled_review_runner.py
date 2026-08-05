import importlib
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from tradingagents.integrations import hermes_scheduled_review_runner as runner
from tradingagents.integrations.hermes_scheduled_reviews import (
    ScheduledReviewProcessReport,
)


class HermesScheduledReviewRunnerTests(unittest.TestCase):
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
            side_effect=lambda: events.append(("load", None)),
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
