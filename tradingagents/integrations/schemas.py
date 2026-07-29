"""Pydantic schemas shared by the Hermes MCP integration."""

import math
import re
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


SUPPORTED_ANALYSTS = ("market", "social", "news", "fundamentals")
SUPPORTED_PROVIDERS = (
    "openai",
    "anthropic",
    "google",
    "deepseek",
    "openrouter",
    "ollama",
)

_SESSION_ID_PATTERN = re.compile(r"^hermes_[0-9a-f]{16,64}$")
_REVIEW_ID_PATTERN = re.compile(r"^review_[0-9a-f]{16,64}$")


def is_valid_session_id(session_id: str) -> bool:
    """Return whether a value is an opaque Hermes session identifier."""
    return isinstance(session_id, str) and bool(_SESSION_ID_PATTERN.fullmatch(session_id))


def is_valid_review_id(review_id: str) -> bool:
    """Return whether a value is an opaque Hermes review identifier."""
    return isinstance(review_id, str) and bool(_REVIEW_ID_PATTERN.fullmatch(review_id))


def utc_now() -> datetime:
    """Return the current timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisRequest(_StrictModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{2,20}$")
    trade_date: date
    analysts: list[str] = Field(min_length=1, max_length=4)
    research_depth: Literal[1, 3, 5]
    llm_provider: str
    quick_model: str = Field(max_length=200)
    deep_model: str = Field(max_length=200)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().upper()

    @field_validator("analysts", mode="before")
    @classmethod
    def normalize_analysts(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(analyst, str) for analyst in value):
            return value
        normalized = [analyst.strip().lower() for analyst in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("analysts must not contain duplicates")
        unsupported = set(normalized) - set(SUPPORTED_ANALYSTS)
        if unsupported:
            raise ValueError("unsupported analyst")
        return normalized

    @field_validator("llm_provider", mode="before")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        if normalized not in SUPPORTED_PROVIDERS:
            raise ValueError("unsupported provider")
        return normalized

    @field_validator("quick_model", "deep_model", mode="before")
    @classmethod
    def normalize_model_name(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("model name must not be blank")
        return normalized


class AnalysisResult(_StrictModel):
    reports: dict[str, str]
    investment_plan: str
    trader_investment_plan: str
    final_trade_decision: str
    processed_signal: str


class ToolError(_StrictModel):
    code: str
    message: str
    suggested_action: str


class AnalysisSession(_StrictModel):
    schema_version: Literal[1] = 1
    session_id: str
    status: Literal["running", "completed", "failed"] = "running"
    created_at: datetime
    completed_at: datetime | None = None
    request: AnalysisRequest
    result: AnalysisResult | None = None
    error: ToolError | None = None

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not is_valid_session_id(value):
            raise ValueError("invalid session id")
        return value


class ReviewRequest(_StrictModel):
    session_id: str
    review_date: date

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not is_valid_session_id(value):
            raise ValueError("invalid session id")
        return value


class PriceReference(_StrictModel):
    date: date
    usd_price: float = Field(gt=0)
    source: Literal["coingecko"]

    @field_validator("usd_price")
    @classmethod
    def validate_finite_price(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("USD price must be finite")
        return value


class PaperDecisionReview(_StrictModel):
    schema_version: Literal[1] = 1
    review_id: str
    session_id: str
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{2,20}$")
    trade_date: date
    review_date: date
    action: Literal["BUY", "SELL", "HOLD", "UNPARSEABLE"]
    entry_price: PriceReference
    review_price: PriceReference
    raw_return_pct: float
    verdict: Literal["correct", "incorrect", "flat", "not_scored"]
    created_at: datetime
    hermes_memory_entry: str = Field(min_length=1, max_length=1000)

    @field_validator("review_id")
    @classmethod
    def validate_review_id(cls, value: str) -> str:
        if not is_valid_review_id(value):
            raise ValueError("invalid review id")
        return value

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not is_valid_session_id(value):
            raise ValueError("invalid session id")
        return value

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().upper()

    @field_validator("raw_return_pct")
    @classmethod
    def validate_finite_return(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("return percentage must be finite")
        return value


class SymbolLearningEntry(_StrictModel):
    review_id: str
    review_date: date
    lesson: str = Field(min_length=1, max_length=1000)

    @field_validator("review_id")
    @classmethod
    def validate_review_id(cls, value: str) -> str:
        if not is_valid_review_id(value):
            raise ValueError("invalid review id")
        return value


class SymbolLearningIndex(_StrictModel):
    schema_version: Literal[1] = 1
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{2,20}$")
    updated_at: datetime
    entries: list[SymbolLearningEntry] = Field(max_length=20)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().upper()
