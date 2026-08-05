"""Pydantic schemas shared by the Hermes MCP integration."""

import math
import re
from datetime import date, datetime, timezone
from typing import Annotated, Literal

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

_NonBlankText100 = Annotated[str, Field(min_length=1, max_length=100)]
_NonBlankText400 = Annotated[str, Field(min_length=1, max_length=400)]
_NonBlankText600 = Annotated[str, Field(min_length=1, max_length=600)]
_NonBlankText800 = Annotated[str, Field(min_length=1, max_length=800)]
_NonBlankText6000 = Annotated[str, Field(min_length=1, max_length=6000)]


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


def _require_nonblank_text(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    return normalized


def _normalize_text_list(value: list[str], field_name: str) -> list[str]:
    if not isinstance(value, list):
        return value
    return [_require_nonblank_text(item, field_name) for item in value]


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
    scheduled_review_version: Literal[1, 2] | None = None


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
    workflow_version: Literal[1, 2] = 1
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
    horizon_days: int | None = Field(default=None, gt=0)
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
    session_id: str | None = None

    @field_validator("review_id")
    @classmethod
    def validate_review_id(cls, value: str) -> str:
        if not is_valid_review_id(value):
            raise ValueError("invalid review id")
        return value

    @field_validator("session_id")
    @classmethod
    def validate_optional_session_id(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_session_id(value):
            raise ValueError("invalid session id")
        return value


class ReportSourceMetadata(_StrictModel):
    name: _NonBlankText100
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    truncated: bool

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("source name must not be blank")
        return normalized


class ReportEvidenceField(_StrictModel):
    name: _NonBlankText100
    excerpt: str = Field(max_length=2000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    truncated: bool

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("evidence field name must not be blank")
        return normalized


class ReportEvidencePacket(_StrictModel):
    schema_version: Literal[1] = 1
    session_id: str
    revision: int = Field(ge=1, le=3)
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    outcome_review_ids: list[str] = Field(min_length=1, max_length=3)
    fields: list[ReportEvidenceField] = Field(min_length=2, max_length=11)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not is_valid_session_id(value):
            raise ValueError("invalid session id")
        return value

    @field_validator("outcome_review_ids")
    @classmethod
    def validate_outcome_review_ids(cls, value: list[str]) -> list[str]:
        if any(not is_valid_review_id(review_id) for review_id in value):
            raise ValueError("invalid review id")
        if len(value) != len(set(value)):
            raise ValueError("outcome review ids must be unique")
        return value

    @field_validator("fields")
    @classmethod
    def validate_unique_field_names(
        cls, value: list[ReportEvidenceField]
    ) -> list[ReportEvidenceField]:
        names = [field.name for field in value]
        if len(names) != len(set(names)):
            raise ValueError("evidence field names must be unique")
        return value


class ReportLearningOutcome(_StrictModel):
    review_id: str
    horizon_days: Literal[1, 7, 15]
    review_date: date
    raw_return_pct: float
    verdict: Literal["correct", "incorrect", "flat", "not_scored"]

    @field_validator("review_id")
    @classmethod
    def validate_review_id(cls, value: str) -> str:
        if not is_valid_review_id(value):
            raise ValueError("invalid review id")
        return value

    @field_validator("raw_return_pct")
    @classmethod
    def validate_finite_return(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("return percentage must be finite")
        return value


class ReportCausalHypothesis(_StrictModel):
    statement: _NonBlankText400
    evidence: list[_NonBlankText100] = Field(min_length=1, max_length=4)
    confidence: Literal["low", "medium", "high"]

    @field_validator("statement", mode="before")
    @classmethod
    def normalize_statement(cls, value: str) -> str:
        return _require_nonblank_text(value, "causal hypothesis statement")

    @field_validator("evidence", mode="before")
    @classmethod
    def normalize_evidence(cls, value: list[str]) -> list[str]:
        return _normalize_text_list(value, "causal hypothesis evidence")


class ReportOutcomeAssessment(_StrictModel):
    horizon_days: Literal[1, 7, 15]
    assessment: _NonBlankText400

    @field_validator("assessment", mode="before")
    @classmethod
    def normalize_assessment(cls, value: str) -> str:
        return _require_nonblank_text(value, "outcome assessment")


class ReportReflection(_StrictModel):
    decision_thesis: _NonBlankText600
    technical_context: str | None = Field(default=None, max_length=600)
    sentiment_context: str | None = Field(default=None, max_length=600)
    news_context: str | None = Field(default=None, max_length=600)
    fundamental_context: str | None = Field(default=None, max_length=600)
    overall_assessment: _NonBlankText800
    outcome_assessments: list[ReportOutcomeAssessment] = Field(min_length=1, max_length=3)
    reasoning_strengths: list[_NonBlankText400] = Field(max_length=3)
    causal_hypotheses: list[ReportCausalHypothesis] = Field(min_length=1, max_length=3)
    mistakes_or_missed_opportunities: list[_NonBlankText400] = Field(max_length=3)
    next_decision_checks: list[_NonBlankText400] = Field(min_length=1, max_length=5)

    @field_validator(
        "decision_thesis",
        "overall_assessment",
        mode="before",
    )
    @classmethod
    def normalize_reflection_text(cls, value: str) -> str:
        return _require_nonblank_text(value, "reflection text")

    @field_validator(
        "reasoning_strengths",
        "mistakes_or_missed_opportunities",
        "next_decision_checks",
        mode="before",
    )
    @classmethod
    def normalize_reflection_lists(cls, value: list[str]) -> list[str]:
        return _normalize_text_list(value, "reflection list item")

    @model_validator(mode="after")
    def require_unique_outcome_horizons(self) -> "ReportReflection":
        horizons = [assessment.horizon_days for assessment in self.outcome_assessments]
        if len(horizons) != len(set(horizons)):
            raise ValueError("outcome assessment horizons must be unique")
        return self


class ReportLearningRevision(_StrictModel):
    revision: int = Field(ge=1, le=3)
    outcome_review_ids: list[str] = Field(min_length=1, max_length=3)
    reflection_state: Literal["pending", "ready", "attention_required"]
    memory_state: Literal[
        "blocked",
        "add_pending",
        "replace_pending",
        "memory_call_started",
        "verification_pending",
        "confirmed",
        "attention_required",
    ]
    source_fields: list[ReportSourceMetadata] = Field(min_length=1, max_length=8)
    reflection_attempt_count: int = Field(default=0, ge=0)
    last_error_code: str | None = Field(default=None, max_length=100)
    reflection: ReportReflection | None = None
    lesson: str | None = Field(default=None, max_length=6000)
    hermes_memory_entry: str | None = Field(default=None, max_length=6000)
    created_at: datetime
    updated_at: datetime
    verified_at: datetime | None = None

    @field_validator("outcome_review_ids")
    @classmethod
    def validate_outcome_review_ids(cls, value: list[str]) -> list[str]:
        if any(not is_valid_review_id(review_id) for review_id in value):
            raise ValueError("invalid review id")
        if len(value) != len(set(value)):
            raise ValueError("outcome review ids must be unique")
        return value

    @field_validator("source_fields")
    @classmethod
    def validate_unique_source_field_names(
        cls, value: list[ReportSourceMetadata]
    ) -> list[ReportSourceMetadata]:
        names = [source.name for source in value]
        if len(names) != len(set(names)):
            raise ValueError("source field names must be unique")
        return value


class ReportLearningRecord(_StrictModel):
    schema_version: Literal[1] = 1
    session_id: str
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{2,20}$")
    trade_date: date
    action: Literal["BUY", "SELL", "HOLD", "UNPARSEABLE"]
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    desired_revision: int = Field(ge=1, le=3)
    reflected_revision: int = Field(default=0, ge=0, le=3)
    confirmed_revision: int = Field(default=0, ge=0, le=3)
    outcomes: list[ReportLearningOutcome] = Field(min_length=1, max_length=3)
    revisions: list[ReportLearningRevision] = Field(min_length=1, max_length=3)
    created_at: datetime
    updated_at: datetime

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

    @model_validator(mode="after")
    def require_coherent_snapshots(self) -> "ReportLearningRecord":
        horizons = [outcome.horizon_days for outcome in self.outcomes]
        if horizons != sorted(horizons) or len(horizons) != len(set(horizons)):
            raise ValueError("outcome horizons must be unique and ordered")
        if not (
            self.desired_revision == len(self.outcomes) == len(self.revisions)
        ):
            raise ValueError("desired revision, outcomes, and revisions must have equal counts")
        expected_revisions = list(range(1, self.desired_revision + 1))
        revisions = [revision.revision for revision in self.revisions]
        if revisions != expected_revisions:
            raise ValueError("revisions must run from 1 through desired revision")
        outcome_ids = [outcome.review_id for outcome in self.outcomes]
        for revision_number, revision in enumerate(self.revisions, start=1):
            expected_ids = outcome_ids[:revision_number]
            if revision.outcome_review_ids != expected_ids:
                raise ValueError("revision outcomes must match the revision-number prefix")
        if not (
            0
            <= self.confirmed_revision
            <= self.reflected_revision
            <= self.desired_revision
        ):
            raise ValueError("revision state must be ordered")

        unconfirmed_memory_states = {
            "add_pending",
            "replace_pending",
            "memory_call_started",
            "verification_pending",
            "attention_required",
        }
        for revision in self.revisions:
            content = (
                revision.reflection,
                revision.lesson,
                revision.hermes_memory_entry,
            )
            if revision.revision <= self.reflected_revision:
                if revision.reflection_state != "ready" or any(
                    item is None for item in content
                ):
                    raise ValueError("reflected revisions require ready reflection content")
            elif (
                revision.reflection_state == "ready"
                or any(item is not None for item in content)
                or revision.memory_state != "blocked"
            ):
                raise ValueError("unreflected revisions must remain blocked without content")

            if revision.revision <= self.confirmed_revision:
                if revision.memory_state != "confirmed" or revision.verified_at is None:
                    raise ValueError("confirmed revisions require confirmation verification")
            elif revision.memory_state == "confirmed" or revision.verified_at is not None:
                raise ValueError("unconfirmed revisions cannot be confirmed or verified")
            elif (
                revision.revision <= self.reflected_revision
                and revision.memory_state not in unconfirmed_memory_states
            ):
                raise ValueError("reflected revisions require an active memory state")
            elif (
                revision.revision <= self.reflected_revision
                and revision.memory_state in {"add_pending", "replace_pending"}
                and revision.memory_state
                != ("add_pending" if revision.revision == 1 else "replace_pending")
            ):
                raise ValueError("initial memory state must match the revision operation")
        return self


class ReportLearningIndexEntry(_StrictModel):
    session_id: str
    trade_date: date
    maturity_days: Literal[1, 7, 15]
    reflected_revision: int = Field(ge=1, le=3)
    updated_at: datetime
    lesson: _NonBlankText6000

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not is_valid_session_id(value):
            raise ValueError("invalid session id")
        return value

    @field_validator("lesson", mode="before")
    @classmethod
    def normalize_lesson(cls, value: str) -> str:
        return _require_nonblank_text(value, "lesson")


class SymbolLearningIndex(_StrictModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"schema_version": {"const": 1}},
                    },
                    "then": {"required": ["entries"]},
                }
            ]
        },
    )

    schema_version: Literal[1, 2] = 1
    symbol: str = Field(pattern=r"^[A-Za-z0-9]{2,20}$")
    updated_at: datetime
    entries: list[SymbolLearningEntry] = Field(default_factory=list)
    report_entries: list[ReportLearningIndexEntry] = Field(default_factory=list)
    legacy_entries: list[SymbolLearningEntry] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def require_v1_entries(cls, value: object) -> object:
        if isinstance(value, dict):
            schema_version = value.get("schema_version", 1)
            if schema_version in (1, "1") and "entries" not in value:
                raise ValueError("v1 indexes require entries")
        return value

    @field_validator("symbol", mode="before")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().upper()

    @model_validator(mode="after")
    def require_versioned_entries(self) -> "SymbolLearningIndex":
        if self.schema_version == 1:
            if self.report_entries or self.legacy_entries:
                raise ValueError("v1 indexes permit only entries")
        elif self.entries:
            raise ValueError("v2 indexes require empty entries")
        return self
