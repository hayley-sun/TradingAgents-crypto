"""Secure configuration and request signing for Hermes Feishu notifications."""

import base64
import hashlib
import hmac
import os
import re
import stat
import unicodedata
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)


EXPECTED_JOB_NAMES = frozenset(
    {"daily_submit", "daily_archive", "review_processor", "review_memory"}
)
EXPECTED_CONFIG_FIELDS = frozenset(
    {"version", "webhook_url", "signing_secret", "jobs"}
)
WEBHOOK_PATH = re.compile(r"^/open-apis/bot/v2/hook/[A-Za-z0-9-]{16,128}$")
CONFIG_ERROR_MESSAGE = "Feishu notifier configuration unavailable"


class FeishuConfigError(RuntimeError):
    """Raised when the private Feishu configuration is unavailable or invalid."""


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
