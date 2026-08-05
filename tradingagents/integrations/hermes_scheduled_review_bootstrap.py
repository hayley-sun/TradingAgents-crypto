"""Load allowlisted Hermes configuration before scheduled-review imports."""

import json
import os
import sys
from importlib import import_module
from pathlib import Path
from typing import MutableMapping


SCHEDULED_REVIEW_ENVIRONMENT_KEYS = (
    "TRADINGAGENTS_RESULTS_DIR",
    "COINGECKO_DEMO_API_KEY",
    "COINGECKO_PRO_API_KEY",
    "CRYPTOCOMPARE_API_KEY",
)


def load_scheduled_review_environment(
    config_path: Path, environment: MutableMapping[str, str]
) -> bool:
    """Load only explicitly allowed scheduled-review runtime values."""
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
    results_dir = values.get("TRADINGAGENTS_RESULTS_DIR")
    if not isinstance(results_dir, str) or not results_dir.strip():
        return False
    selected = {
        key: value
        for key in SCHEDULED_REVIEW_ENVIRONMENT_KEYS
        if isinstance((value := values.get(key)), str) and value.strip()
    }
    environment.update(selected)
    return True


def _load_default_environment() -> bool:
    hermes_home = os.environ.get("HERMES_HOME")
    config_path = (
        Path(hermes_home).expanduser() / "config.yaml"
        if hermes_home
        else Path.home() / ".hermes" / "config.yaml"
    )
    return load_scheduled_review_environment(config_path, os.environ)


def _failure(mode: str) -> dict[str, object]:
    return {
        "ok": False,
        "mode": mode,
        "error": {
            "code": "SCHEDULED_REVIEW_RUNNER_FAILED",
            "message": "The scheduled paper-review command could not complete.",
            "suggested_action": "Inspect the safe Cron result and retry later.",
        },
    }


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    mode = arguments[0] if arguments else "unknown"
    try:
        if not _load_default_environment():
            raise RuntimeError("scheduled-review environment unavailable")
        runner = import_module(
            "tradingagents.integrations.hermes_scheduled_review_runner"
        )
        return runner.main(arguments)
    except Exception:
        print(json.dumps(_failure(mode), ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    sys.exit(main())
