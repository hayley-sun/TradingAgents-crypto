"""Safely load the deterministic daily-report runner for Hermes Cron."""

import json
import sys
from importlib import import_module


def _failure(mode: str) -> dict[str, object]:
    return {
        "ok": False,
        "mode": mode,
        "error": {
            "code": "REPORT_RUNNER_FAILED",
            "message": "The daily report command could not complete.",
            "suggested_action": "Inspect the safe Cron result and retry later.",
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Delegate to the runner while redacting import and startup failures."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    mode = arguments[0] if arguments and arguments[0] in {"submit", "archive"} else "unknown"
    try:
        runner = import_module("tradingagents.integrations.hermes_daily_report_runner")
        return runner.main(arguments)
    except Exception:
        print(json.dumps(_failure(mode), ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
