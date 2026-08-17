"""Safely load private Feishu configuration before importing its runner."""

import json
import sys
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path

from tradingagents.integrations.hermes_feishu_client import load_private_config


CONFIG_PATH = Path.home() / ".hermes" / "secrets" / "feishu-notifier.yaml"


def _failure() -> dict[str, object]:
    return {
        "ok": False,
        "mode": "run",
        "error": {
            "code": "FEISHU_NOTIFIER_FAILED",
            "message": "The Feishu notifier could not complete.",
            "suggested_action": (
                "Inspect the safe notifier Cron result and private configuration."
            ),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Load validated config, then run the notifier without exposing it."""

    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        config = load_private_config(CONFIG_PATH)
        runner = import_module(
            "tradingagents.integrations.hermes_feishu_notifier"
        )
        result = runner.main(arguments, config=config)
        if type(result) is not int:
            raise ValueError("invalid notifier result")
        return result
    except Exception:
        print(json.dumps(_failure(), ensure_ascii=True, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
