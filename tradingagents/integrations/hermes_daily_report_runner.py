"""Deterministic no-agent Hermes daily report commands."""

import argparse
import json
import re
import sys
from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from tradingagents.integrations.hermes_mcp import (
    archive_daily_report_impl,
    get_daily_report_batch_impl,
    start_daily_report_batch_impl,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*)\s*[:=]\s*[^\s,;]+"
)
_SECRET_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
FIXED_REQUEST = {
    "symbols": ["BTC", "ETH", "SOL"],
    "analysts": ["market", "news", "fundamentals"],
    "research_depth": 1,
    "llm_provider": "deepseek",
    "quick_model": "deepseek-v4-flash",
    "deep_model": "deepseek-v4-pro",
}


def shanghai_trade_date(now: datetime | None = None) -> date:
    """Return the calendar date for an instant in Asia/Shanghai."""
    instant = now or datetime.now(SHANGHAI)
    return instant.astimezone(SHANGHAI).date()


def run_submit(
    trade_date: date,
    submit: Callable[[dict[str, Any]], dict[str, Any]] = start_daily_report_batch_impl,
) -> tuple[int, dict[str, Any]]:
    """Create or load one fixed daily paper-research batch."""
    result = submit({**FIXED_REQUEST, "trade_date": trade_date.isoformat()})
    if not result.get("ok"):
        return 1, {"ok": False, "mode": "submit", "error": result["error"]}
    return 0, {
        "ok": True,
        "mode": "submit",
        "trade_date": trade_date.isoformat(),
        "batch_id": result["data"]["batch"]["batch_id"],
    }


def _short(value: object, limit: int = 500) -> str:
    text = str(value or "不可用").replace("\r", " ").replace("\n", " ").strip()
    text = _SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", text)
    text = _SECRET_TOKEN.sub("[REDACTED]", text)
    return text if len(text) <= limit else f"{text[:limit]}..."


def _item_value(item: dict[str, Any], key: str) -> object:
    value = item.get(key)
    if value:
        return value
    error = item.get("error")
    if isinstance(error, dict):
        return error.get("code")
    return None


def render_archive_narrative(
    summary: dict[str, Any], previous: dict[str, Any] | None
) -> str:
    """Render a bounded deterministic Chinese report narrative."""
    lines = [
        "本报告基于已持久化的日度研究批次生成，仅用于研究和模拟交易。",
        f"批次状态：{summary['state']}。",
    ]
    for item in summary["items"]:
        lines.append(
            f"{item['symbol']}：状态 {item['status']}；"
            f"信号：{_short(_item_value(item, 'processed_signal'))}；"
            f"决策：{_short(_item_value(item, 'final_trade_decision'))}。"
        )
    if previous is None:
        lines.append("无可比较的上一份归档报告。")
    else:
        lines.append(f"上一份归档交易日：{previous['trade_date']}。")
        for item in previous.get("items", []):
            lines.append(
                f"上一期 {item['symbol']}：状态 {item['status']}；"
                f"信号：{_short(_item_value(item, 'processed_signal'))}；"
                f"决策：{_short(_item_value(item, 'final_trade_decision'))}。"
            )
    lines.append("风险提示：信号与决策可能失效，不构成交易建议。")
    return "\n".join(lines)


def run_archive(
    trade_date: date,
    lookup: Callable[[str], dict[str, Any]] = get_daily_report_batch_impl,
    archive: Callable[[str, str], dict[str, Any]] = archive_daily_report_impl,
) -> tuple[int, dict[str, Any]]:
    """Archive one terminal report batch without using an LLM."""
    lookup_result = lookup(trade_date.isoformat())
    if not lookup_result.get("ok"):
        return 1, {"ok": False, "mode": "archive", "error": lookup_result["error"]}

    data = lookup_result["data"]
    summary = data["summary"]
    if summary["state"] == "active":
        return 0, {
            "ok": True,
            "mode": "archive",
            "state": "active",
            "trade_date": trade_date.isoformat(),
        }

    result = archive(
        trade_date.isoformat(),
        render_archive_narrative(summary, data["previous_report"]),
    )
    if not result.get("ok"):
        return 1, {"ok": False, "mode": "archive", "error": result["error"]}
    return 0, {
        "ok": True,
        "mode": "archive",
        "trade_date": trade_date.isoformat(),
        **result["data"],
    }


def _invalid_request() -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "INVALID_REPORT_REQUEST",
            "message": "The daily report command is invalid.",
            "suggested_action": "Use submit or archive with an ISO trade date.",
        },
    }


def _runner_failure(mode: str) -> dict[str, Any]:
    return {
        "ok": False,
        "mode": mode,
        "error": {
            "code": "REPORT_RUNNER_FAILED",
            "message": "The daily report command could not complete.",
            "suggested_action": "Inspect the safe Cron result and retry later.",
        },
    }


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parse_trade_date(value: str | None) -> date:
    if value is None:
        return shanghai_trade_date()
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("invalid trade date")
    return parsed


def main(argv: list[str] | None = None) -> int:
    """Run one safe no-agent submit or archive command."""
    parser = _SafeArgumentParser(add_help=False)
    parser.add_argument("mode", choices=("submit", "archive"))
    parser.add_argument("--trade-date")
    try:
        arguments = parser.parse_args(argv)
        trade_date = _parse_trade_date(arguments.trade_date)
    except (TypeError, ValueError):
        payload = _invalid_request()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1

    try:
        code, payload = (
            run_submit(trade_date)
            if arguments.mode == "submit"
            else run_archive(trade_date)
        )
    except Exception:
        code, payload = 1, _runner_failure(arguments.mode)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
