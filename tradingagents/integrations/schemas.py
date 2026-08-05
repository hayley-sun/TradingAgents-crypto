"""Pydantic schemas shared by the Hermes MCP integration."""

import math
import re
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
_REPORT_BATCH_ID_PATTERN = re.compile(r"^report_[0-9a-f]{16,64}$")
_SYMBOL_PATTERN = re.compile(r"^[A-Za-z0-9]{2,20}$")


def is_valid_session_id(session_id: str) -> bool:
    """Return whether a value is an opaque Hermes session identifier."""
    return isinstance(session_id, str) and bool(_SESSION_ID_PATTERN.fullmatch(session_id))


def is_valid_review_id(review_id: str) -> bool:
    """Return whether a value is an opaque Hermes review identifier."""
    return isinstance(review_id, str) and bool(_REVIEW_ID_PATTERN.fullmatch(review_id))


def is_valid_report_batch_id(batch_id: str) -> bool:
    """Return whether a value is a valid opaque daily report batch ID."""
    return isinstance(batch_id, str) and bool(_REPORT_BATCH_ID_PATTERN.fullmatch(batch_id))


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


class DailyReportRequest(_StrictModel):
    trade_date: date
    symbols: list[str] = Field(min_length=1, max_length=5)
    analysts: list[str] = Field(min_length=1, max_length=4)
    research_depth: Literal[1, 3, 5]
    llm_provider: str
    quick_model: str = Field(max_length=200)
    deep_model: str = Field(max_length=200)

    @field_validator("symbols", mode="before")
    @classmethod
    def normalize_symbols(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list) or not all(isinstance(symbol, str) for symbol in value):
            return value
        normalized = [symbol.strip().upper() for symbol in value]
        if len(normalized) != len(set(normalized)):
            raise ValueError("symbols must not contain duplicates")
        if any(not _SYMBOL_PATTERN.fullmatch(symbol) for symbol in normalized):
            raise ValueError("invalid symbol")
        return normalized

    @field_validator("analysts", mode="before")
    @classmethod
    def normalize_analysts(cls, value: list[str]) -> list[str]:
        return AnalysisRequest.normalize_analysts(value)

    @field_validator("llm_provider", mode="before")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return AnalysisRequest.normalize_provider(value)

    @field_validator("quick_model", "deep_model", mode="before")
    @classmethod
    def normalize_model_name(cls, value: str) -> str:
        return AnalysisRequest.normalize_model_name(value)

    def for_symbol(self, symbol: str) -> AnalysisRequest:
        return AnalysisRequest(
            symbol=symbol,
            trade_date=self.trade_date,
            analysts=self.analysts,
            research_depth=self.research_depth,
            llm_provider=self.llm_provider,
            quick_model=self.quick_model,
            deep_model=self.deep_model,
        )


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


class DailyReportBatchItem(_StrictModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{2,20}$")
    session_id: str | None = None
    submission_error: ToolError | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().upper()

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_session_id(value):
            raise ValueError("invalid session id")
        return value

    @model_validator(mode="after")
    def require_one_outcome(self) -> "DailyReportBatchItem":
        if (self.session_id is None) == (self.submission_error is None):
            raise ValueError("exactly one of session_id or submission_error is required")
        return self


class DailyReportArchive(_StrictModel):
    filename: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}\.md$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["ready", "degraded"]
    archived_at: datetime
    items: list["DailyReportArchiveItem"] = Field(min_length=1, max_length=5)
    scheduled_review_version: Literal[1] | None = None


class DailyReportArchiveItem(_StrictModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{2,20}$")
    status: Literal[
        "completed", "failed", "submission_failed", "missing", "unreadable"
    ]
    processed_signal: str | None = Field(default=None, max_length=10000)
    final_trade_decision: str | None = Field(default=None, max_length=10000)
    error_code: str | None = Field(default=None, max_length=100)

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().upper()


class DailyReportBatch(_StrictModel):
    schema_version: Literal[1] = 1
    batch_id: str
    request: DailyReportRequest
    created_at: datetime
    items: list[DailyReportBatchItem] = Field(max_length=5)
    archive: DailyReportArchive | None = None

    @field_validator("batch_id")
    @classmethod
    def validate_batch_id(cls, value: str) -> str:
        if not is_valid_report_batch_id(value):
            raise ValueError("invalid report batch id")
        return value

    @model_validator(mode="after")
    def require_requested_symbols_once(self) -> "DailyReportBatch":
        item_symbols = [item.symbol for item in self.items]
        if item_symbols != self.request.symbols[: len(item_symbols)]:
            raise ValueError("batch items must be an ordered request-symbol prefix")
        return self


class ScheduledReviewItem(_StrictModel):
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{2,20}$")
    session_id: str | None = None
    horizon_days: Literal[1, 7, 15]
    review_date: date
    review_id: str | None = None
    state: Literal[
        "review_pending",
        "memory_pending",
        "completed",
        "skipped",
        "attention_required",
    ] = "review_pending"
    attempt_count: int = Field(default=0, ge=0)
    last_error_code: str | None = Field(default=None, max_length=100)
    skip_reason: str | None = Field(default=None, max_length=100)
    updated_at: datetime
    verified_at: datetime | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().upper()

    @field_validator("session_id")
    @classmethod
    def validate_optional_session_id(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_session_id(value):
            raise ValueError("invalid session id")
        return value

    @field_validator("review_id")
    @classmethod
    def validate_optional_review_id(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_review_id(value):
            raise ValueError("invalid review id")
        return value

    @model_validator(mode="after")
    def require_state_identity(self) -> "ScheduledReviewItem":
        if self.state == "skipped":
            if not self.skip_reason:
                raise ValueError("skipped review requires a reason")
        elif self.session_id is None or self.review_id is None:
            raise ValueError("active review requires session and review ids")
        if self.state == "completed" and self.verified_at is None:
            raise ValueError("completed review requires verification time")
        return self


class ScheduledReviewPlan(_StrictModel):
    schema_version: Literal[1] = 1
    batch_id: str
    trade_date: date
    created_at: datetime
    items: list[ScheduledReviewItem] = Field(max_length=15)

    @field_validator("batch_id")
    @classmethod
    def validate_batch_id(cls, value: str) -> str:
        if not is_valid_report_batch_id(value):
            raise ValueError("invalid report batch id")
        return value

    @model_validator(mode="after")
    def require_unique_items(self) -> "ScheduledReviewPlan":
        identities = [(item.symbol, item.horizon_days) for item in self.items]
        if len(identities) != len(set(identities)):
            raise ValueError("scheduled review items must be unique")
        return self


class AnalysisSession(_StrictModel):
    schema_version: Literal[1] = 1
    session_id: str
    status: Literal["queued", "running", "completed", "failed"] = "running"
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    worker_pid: int | None = Field(default=None, ge=1)
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
    source: Literal["coingecko", "cryptocompare", "coinbase"]

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
