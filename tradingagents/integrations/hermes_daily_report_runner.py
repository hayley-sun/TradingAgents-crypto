"""Deterministic no-agent Hermes daily report commands."""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from tradingagents.integrations.hermes_mcp import start_daily_report_batch_impl


SHANGHAI = ZoneInfo("Asia/Shanghai")
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
