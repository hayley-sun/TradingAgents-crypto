"""Detached process entry point for long-running Hermes analyses."""

import sys

from tradingagents.integrations.hermes_mcp import run_queued_analysis


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    result = run_queued_analysis(sys.argv[1])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
