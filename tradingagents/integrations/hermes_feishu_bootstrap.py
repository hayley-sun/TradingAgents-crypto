"""Safely load private Feishu configuration before importing its runner."""

import json
import sys
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path



def load_private_config(path: Path) -> object:
    """Late-load the private-config loader after the startup boundary begins."""

    from tradingagents.integrations.hermes_feishu_client import (
        load_private_config as real_loader,
    )

    return real_loader(path)


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


def _emit_failure() -> int:
    try:
        line = json.dumps(
            _failure(), ensure_ascii=True, sort_keys=True, allow_nan=False
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return 1
    try:
        sys.stdout.write(line + "\n")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return 1
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Load validated config, then run the notifier without exposing it."""

    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        config_path = (
            Path.home() / ".hermes" / "secrets" / "feishu-notifier.yaml"
        )
        config = load_private_config(config_path)
        runner = import_module(
            "tradingagents.integrations.hermes_feishu_notifier"
        )
        result = runner.main(arguments, config=config)
        if type(result) is not int or result not in {0, 1}:
            raise ValueError("invalid notifier result")
        return result
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return _emit_failure()


if __name__ == "__main__":
    raise SystemExit(main())
