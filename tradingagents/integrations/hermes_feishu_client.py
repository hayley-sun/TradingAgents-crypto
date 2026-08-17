"""Secure configuration and request signing for Hermes Feishu notifications."""

import base64
import hashlib
import hmac
import json
import os
import re
import stat
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, Self
from urllib.parse import urlsplit

import requests
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from tradingagents.integrations.hermes_feishu_state import NotificationEvent


EXPECTED_JOB_NAMES = frozenset(
    {"daily_submit", "daily_archive", "review_processor", "review_memory"}
)
EXPECTED_CONFIG_FIELDS = frozenset(
    {"version", "webhook_url", "signing_secret", "jobs"}
)
WEBHOOK_PATH = re.compile(r"^/open-apis/bot/v2/hook/[A-Za-z0-9-]{16,128}$")
CONFIG_ERROR_MESSAGE = "Feishu notifier configuration unavailable"
MAX_FREE_FIELD_CHARACTERS = 500
MAX_REQUEST_BYTES = 20_000
MAX_RESPONSE_BYTES = 65_536
MAX_RETRY_AFTER_SECONDS = 86_400
SIGNED_ENVELOPE_RESERVE_BYTES = 256
MAX_RENDERED_CARD_BYTES = MAX_REQUEST_BYTES - SIGNED_ENVELOPE_RESERVE_BYTES
REPORT_DISCLAIMER = "仅用于研究和模拟交易，不构成交易建议"
TRUNCATION_NOTICE = "\n\n_其余内容因长度限制已省略_"
SECRET_ASSIGNMENT = re.compile(
    r"\b([A-Z0-9_-]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_-]*)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
SECRET_TOKEN = re.compile(r"\bsk-[A-Za-z0-9_-]+\b", re.IGNORECASE)


class FeishuConfigError(RuntimeError):
    """Raised when the private Feishu configuration is unavailable or invalid."""


@dataclass(frozen=True)
class ReportCardItem:
    symbol: str
    status: str
    processed_signal: str | None
    final_trade_decision: str | None
    error_code: str | None


@dataclass(frozen=True)
class ReportCardData:
    event_id: str
    trade_date: date
    state: str
    items: tuple[ReportCardItem, ...]
    report_path: Path


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    body: bytes
    retry_after_seconds: int | None


class FeishuDeliveryError(RuntimeError):
    def __init__(
        self, result: str, retry_after_seconds: int | None = None
    ) -> None:
        super().__init__(result)
        self.result = result
        self.retry_after_seconds = retry_after_seconds


class _Transport(Protocol):
    def post(self, url: str, payload: dict[str, Any]) -> TransportResponse: ...


class _ImmutableJobs(dict[str, str]):
    @classmethod
    def from_mapping(cls, values: dict[str, str]) -> Self:
        instance = dict.__new__(cls)
        dict.__init__(instance, values)
        return instance

    def copy(self) -> dict[str, str]:
        return dict(self)

    def _reject_mutation(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("jobs mapping is immutable")

    __init__ = _reject_mutation
    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation
    __ior__ = _reject_mutation


class FeishuNotifierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    webhook_url: str
    signing_secret: str = Field(min_length=1, max_length=512)
    jobs: dict[str, str]

    @field_validator("version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return value

    @field_validator("jobs")
    @classmethod
    def freeze_jobs(cls, value: dict[str, str]) -> dict[str, str]:
        return _ImmutableJobs.from_mapping(value)

    @field_serializer("jobs")
    def serialize_jobs(self, value: dict[str, str]) -> dict[str, str]:
        return dict(value)

    @model_validator(mode="after")
    def validate_boundary(self) -> Self:
        if (
            re.search(r"[\x00-\x20\x7f]", self.webhook_url) is not None
            or "?" in self.webhook_url
            or "#" in self.webhook_url
        ):
            raise ValueError("invalid Feishu notifier configuration")
        try:
            parts = urlsplit(self.webhook_url)
            port = parts.port
        except ValueError:
            raise ValueError("invalid Feishu notifier configuration") from None

        invalid = (
            parts.scheme != "https"
            or parts.netloc not in ("open.feishu.cn", "open.feishu.cn:443")
            or parts.hostname != "open.feishu.cn"
            or port not in (None, 443)
            or parts.username is not None
            or parts.password is not None
            or bool(parts.query)
            or bool(parts.fragment)
            or WEBHOOK_PATH.fullmatch(parts.path) is None
            or any(
                unicodedata.category(character) == "Cc"
                for character in self.signing_secret
            )
            or set(self.jobs) != EXPECTED_JOB_NAMES
            or len(set(self.jobs.values())) != 4
            or any(
                re.fullmatch(r"[0-9a-f]{12}", value) is None
                for value in self.jobs.values()
            )
        )
        if invalid:
            raise ValueError("invalid Feishu notifier configuration")
        return self


def feishu_signature(timestamp: int, secret: str) -> str:
    key = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(key, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_bounded_retry_after(value: str | None) -> int | None:
    if value is None or re.fullmatch(r"[0-9]{1,10}", value) is None:
        return None
    seconds = int(value)
    return seconds if seconds <= MAX_RETRY_AFTER_SECONDS else None


def _bounded_retry_after_seconds(value: object) -> int | None:
    if type(value) is not int or not 0 <= value <= MAX_RETRY_AFTER_SECONDS:
        return None
    return value


def _strict_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


class RequestsTransport:
    def __init__(self, connect_timeout: float = 3.05, read_timeout: float = 10):
        self.session = requests.Session()
        self.session.trust_env = False
        self.timeout = (connect_timeout, read_timeout)

    def post(self, url: str, payload: dict[str, Any]) -> TransportResponse:
        response = self.session.post(
            url,
            data=_strict_json_bytes(payload),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=self.timeout,
            allow_redirects=False,
            stream=True,
        )
        body = bytearray()
        for chunk in response.iter_content(4096):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                response.close()
                raise FeishuDeliveryError("response_too_large")
        return TransportResponse(
            response.status_code,
            bytes(body),
            parse_bounded_retry_after(response.headers.get("Retry-After")),
        )


def _safe_free_field(value: object | None) -> str:
    if value is None:
        return "不可用"
    normalized = re.sub(r"[\r\n]+", " ", str(value))
    redacted = SECRET_ASSIGNMENT.sub(r"\1=[REDACTED]", normalized)
    redacted = SECRET_TOKEN.sub("[REDACTED]", redacted)
    return redacted[:MAX_FREE_FIELD_CHARACTERS]


def _card_payload(title: str, color: str, body: str) -> dict[str, Any]:
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "template": color,
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": body},
                }
            ],
        },
    }


def _serialized_json_size(payload: dict[str, Any]) -> int:
    return len(_strict_json_bytes(payload))


def _interactive_card(title: str, color: str, lines: list[str]) -> dict[str, Any]:
    body = "\n".join(lines)
    payload = _card_payload(title, color, body)
    if _serialized_json_size(payload) <= MAX_RENDERED_CARD_BYTES:
        return payload

    lower = 0
    upper = len(body)
    while lower < upper:
        midpoint = (lower + upper + 1) // 2
        candidate = _card_payload(
            title, color, body[:midpoint] + TRUNCATION_NOTICE
        )
        if _serialized_json_size(candidate) <= MAX_RENDERED_CARD_BYTES:
            lower = midpoint
        else:
            upper = midpoint - 1
    return _card_payload(title, color, body[:lower] + TRUNCATION_NOTICE)


def _report_item_line(item: ReportCardItem) -> str:
    return (
        f"- **{_safe_free_field(item.symbol)}** | "
        f"状态: {_safe_free_field(item.status)} | "
        f"信号: {_safe_free_field(item.processed_signal)} | "
        f"决策: {_safe_free_field(item.final_trade_decision)} | "
        f"错误码: {_safe_free_field(item.error_code)}"
    )


def render_report_card(
    report: ReportCardData, previous: ReportCardData | None
) -> dict[str, Any]:
    lines = [
        f"**事件 ID**: `{_safe_free_field(report.event_id)}`",
        REPORT_DISCLAIMER,
        f"**交易日期**: {report.trade_date.isoformat()}",
        f"**归档状态**: {_safe_free_field(report.state)}",
        f"**本地报告**: `{_safe_free_field(report.report_path)}`",
    ]
    if previous is not None:
        previous_items = {item.symbol: item for item in previous.items}
        lines.extend(
            [
                "",
                f"**最近归档对比 | {previous.trade_date.isoformat()}**",
                f"前次归档状态: {_safe_free_field(previous.state)}",
            ]
        )
        for item in report.items:
            prior = previous_items.get(item.symbol)
            lines.append(
                f"- **{_safe_free_field(item.symbol)}** | "
                f"前次状态: {_safe_free_field(prior.status if prior else None)} | "
                "前次信号: "
                f"{_safe_free_field(prior.processed_signal if prior else None)} | "
                "前次决策: "
                f"{_safe_free_field(prior.final_trade_decision if prior else None)}"
            )
    lines.extend(["", "**当前结果**"])
    lines.extend(_report_item_line(item) for item in report.items)
    return _interactive_card(
        f"TradingAgents 日报 | {report.trade_date.isoformat()}",
        "green",
        lines,
    )


def render_failure_card(event: NotificationEvent) -> dict[str, Any]:
    lines = [
        f"**事件 ID**: `{_safe_free_field(event.event_id)}`",
        "**错误类型**: `CRON_EXECUTION_FAILED`",
        f"**任务名称**: {_safe_free_field(event.job_name)}",
        f"**Job ID**: `{_safe_free_field(event.job_id)}`",
        f"**Execution ID**: `{_safe_free_field(event.execution_id)}`",
        f"**发生时间**: {event.created_at.isoformat()}",
        "请在服务器上检查该 execution 的安全运行记录。",
    ]
    return _interactive_card("TradingAgents 定时任务失败", "red", lines)


def render_missing_archive_card(event: NotificationEvent) -> dict[str, Any]:
    lines = [
        f"**事件 ID**: `{_safe_free_field(event.event_id)}`",
        f"**交易日期**: {event.trade_date.isoformat() if event.trade_date else '不可用'}",
        f"**批次状态**: {_safe_free_field(event.batch_state)}",
        f"**任务名称**: {_safe_free_field(event.job_name)}",
        f"**Job ID**: `{_safe_free_field(event.job_id)}`",
        f"**Execution ID**: `{_safe_free_field(event.execution_id)}`",
        "请检查 sessions 与下一次归档执行。",
    ]
    return _interactive_card("TradingAgents 日报待归档", "orange", lines)


def render_test_card(event_id: str, now: datetime) -> dict[str, Any]:
    lines = [
        f"**事件 ID**: `{_safe_free_field(event_id)}`",
        f"**验收时间**: {now.isoformat()}",
        "飞书通知配置已通过发送验收。",
    ]
    return _interactive_card(
        "TradingAgents 飞书通知配置验收", "orange", lines
    )


def _serialize_payload(payload: dict[str, Any]) -> bytes | None:
    try:
        return _strict_json_bytes(payload)
    except (TypeError, ValueError):
        return None


def _reject_nonstandard_json_constant(_value: str) -> object:
    raise ValueError("nonstandard JSON constant")


class FeishuClient:
    def __init__(
        self,
        config: FeishuNotifierConfig,
        transport: _Transport | None = None,
        clock: Callable[[], int | float] = time.time,
    ) -> None:
        self.config = config
        self.transport = transport if transport is not None else RequestsTransport()
        self.clock = clock

    def send(self, payload: dict[str, Any]) -> None:
        timestamp = int(self.clock())
        outbound = dict(payload)
        outbound["timestamp"] = str(timestamp)
        outbound["sign"] = feishu_signature(
            timestamp, self.config.signing_secret
        )
        serialized = _serialize_payload(outbound)
        if serialized is None or len(serialized) > MAX_REQUEST_BYTES:
            raise FeishuDeliveryError("http_error")

        response: TransportResponse | None = None
        transport_error: str | None = None
        retry_after_seconds: int | None = None
        try:
            response = self.transport.post(self.config.webhook_url, outbound)
        except requests.exceptions.Timeout:
            transport_error = "timeout"
        except requests.exceptions.ConnectionError:
            transport_error = "connection_error"
        except requests.exceptions.RequestException:
            transport_error = "connection_error"
        except FeishuDeliveryError as error:
            transport_error = (
                error.result
                if error.result == "response_too_large"
                else "http_error"
            )
            retry_after_seconds = error.retry_after_seconds
        if transport_error is not None:
            raise FeishuDeliveryError(transport_error, retry_after_seconds)

        if response is None:
            raise FeishuDeliveryError("connection_error")
        if 300 <= response.status_code < 400:
            raise FeishuDeliveryError("redirect_rejected")
        if response.status_code == 429:
            raise FeishuDeliveryError(
                "rate_limited",
                _bounded_retry_after_seconds(response.retry_after_seconds),
            )
        if not 200 <= response.status_code < 300:
            raise FeishuDeliveryError("http_error")
        try:
            decoded = json.loads(
                response.body, parse_constant=_reject_nonstandard_json_constant
            )
        except (ValueError, UnicodeDecodeError):
            decoded = None
        if not isinstance(decoded, dict) or "code" not in decoded:
            raise FeishuDeliveryError("invalid_response")
        if type(decoded["code"]) is not int or decoded["code"] != 0:
            raise FeishuDeliveryError("feishu_error")


def _load_private_config(path: str | os.PathLike[str]) -> FeishuNotifierConfig:
    descriptor: int | None = None
    try:
        config_path = Path(path)
        owner = os.geteuid()
        parent_metadata = os.lstat(config_path.parent)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != owner
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        ):
            raise ValueError("invalid private configuration directory")

        descriptor = os.open(
            config_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        file_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_metadata.st_mode)
            or file_metadata.st_uid != owner
            or stat.S_IMODE(file_metadata.st_mode) != 0o600
        ):
            raise ValueError("invalid private configuration file")

        stream = os.fdopen(descriptor, mode="r", encoding="utf-8")
        descriptor = None
        with stream:
            payload = yaml.safe_load(stream)
        if not isinstance(payload, dict) or set(payload) != EXPECTED_CONFIG_FIELDS:
            raise ValueError("invalid private configuration fields")
        return FeishuNotifierConfig.model_validate(payload)
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def load_private_config(path: str | os.PathLike[str]) -> FeishuNotifierConfig:
    try:
        return _load_private_config(path)
    except Exception:
        pass
    raise FeishuConfigError(CONFIG_ERROR_MESSAGE)
