"""Safely load the deterministic daily-report runner for Hermes Cron."""

import json
import os
import sys
from importlib import import_module
from pathlib import Path
from typing import MutableMapping


CRON_ENVIRONMENT_KEYS = (
    "TRADINGAGENTS_RESULTS_DIR",
    "DEEPSEEK_API_KEY",
    "FINNHUB_API_KEY",
    "COINGECKO_DEMO_API_KEY",
    "COINGECKO_PRO_API_KEY",
    "CRYPTOCOMPARE_API_KEY",
)


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


def load_tradingagents_cron_environment(
    config_path: Path, environment: MutableMapping[str, str]
) -> bool:
    """Load only TradingAgents' explicitly allowed Cron runtime values."""
    try:
        import yaml

        config = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except Exception:
        return False

    if not isinstance(config, dict):
        return False
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return False
    server = servers.get("tradingagents_crypto")
    if not isinstance(server, dict):
        return False
    values = server.get("env")
    if not isinstance(values, dict):
        return False

    selected = {
        key: value
        for key in CRON_ENVIRONMENT_KEYS
        if isinstance((value := values.get(key)), str) and value.strip()
    }
    environment.update(selected)
    return True


def _load_default_cron_environment() -> bool:
    hermes_home = os.environ.get("HERMES_HOME")
    config_path = (
        Path(hermes_home).expanduser() / "config.yaml"
        if hermes_home
        else Path.home() / ".hermes" / "config.yaml"
    )
    return load_tradingagents_cron_environment(config_path, os.environ)


def main(argv: list[str] | None = None) -> int:
    """Delegate to the runner while redacting import and startup failures."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    mode = arguments[0] if arguments and arguments[0] in {"submit", "archive"} else "unknown"
    try:
        _load_default_cron_environment()
        runner = import_module("tradingagents.integrations.hermes_daily_report_runner")
        return runner.main(arguments)
    except Exception:
        print(json.dumps(_failure(mode), ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
