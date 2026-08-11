"""Durable report-level facts aggregated from paper-decision reviews."""

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import fcntl
from pydantic import ValidationError

from tradingagents.integrations.hermes_learning import (
    LearningStore,
    extract_paper_action,
)
from tradingagents.integrations.schemas import (
    AnalysisSession,
    PaperDecisionReview,
    ReportEvidenceField,
    ReportEvidencePacket,
    ReportLearningOutcome,
    ReportLearningRecord,
    ReportLearningRevision,
    ReportReflection,
    ReportSourceMetadata,
    is_valid_session_id,
    utc_now,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_MEMORY_MARKER = "[TradingAgents paper report: {session_id}]"
_MEMORY_MARKER_PREFIX = REPORT_MEMORY_MARKER.split("{session_id}")[0]
MAX_REPORT_REVISIONS = 3
EVIDENCE_PACKET_MAX_BYTES = 4096
REPORT_LESSON_MAX_CHARS = 2400
HERMES_REPORT_MEMORY_MAX_CHARS = 512
HERMES_REPORT_MEMORY_MAX_BYTES = 1536
HERMES_MEMORY_CHAR_LIMIT = 40000
HERMES_REPORT_ENTRY_RESERVATION = 60
HERMES_ENTRY_DELIMITER_CHARS = 3
MAX_REFLECTION_ATTEMPTS = 3
EVIDENCE_FIELD_ORDER = (
    "report.market",
    "report.sentiment",
    "report.news",
    "report.fundamentals",
    "investment_plan",
    "trader_plan",
    "final_decision",
    "processed_signal",
)
_ALLOWED_HORIZONS = (1, 7, 15)
_INITIAL_EVIDENCE_EXCERPT_CHARS = 1200
_MIN_EVIDENCE_EXCERPT_CHARS = 32
_TRUNCATION_MARKER = "\n...[truncated]...\n"
_REFLECTION_ERROR_CODES = frozenset(
    {
        "REFLECTION_SCHEMA_INVALID",
        "REFLECTION_EVIDENCE_INVALID",
        "REFLECTION_OUTCOMES_INVALID",
        "REFLECTION_VERDICT_SECTIONS_INVALID",
        "REFLECTION_UNSAFE_CONTENT",
    }
)


class ReportLearningError(RuntimeError):
    """Raised when report-level learning facts cannot be persisted safely."""


class ReportLearningConflict(ReportLearningError):
    """Raised when incoming facts conflict with a persisted report identity."""


class ReportReflectionRejected(ReportLearningError):
    """Raised when reflection content fails bounded domain validation."""

    def __init__(self, error_code: str):
        if error_code not in _REFLECTION_ERROR_CODES:
            raise ValueError("invalid reflection error code")
        super().__init__("report reflection rejected")
        self.error_code = error_code


class ReportReflectionRetryDeferred(ReportLearningError):
    """The pending revision already consumed its UTC-date attempt."""


@dataclass(frozen=True)
class RenderedReportLesson:
    lesson: str
    hermes_memory_entry: str


def _atomic_json_write(destination: Path, value: dict) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="ascii",
            dir=destination.parent,
            prefix=f".{destination.stem}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(value, temporary_file, ensure_ascii=True, indent=2)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class ReportLearningStore:
    """Filesystem-backed report facts keyed by opaque analysis session ID."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    @classmethod
    def from_environment(cls) -> "ReportLearningStore":
        configured = os.getenv("TRADINGAGENTS_RESULTS_DIR")
        results_root = Path(configured) if configured else PROJECT_ROOT / "results"
        return cls(results_root / "hermes" / "report_memories")

    def path_for(self, session_id: str) -> Path:
        if not is_valid_session_id(session_id):
            raise ValueError("invalid session id")
        return self.root / f"{session_id}.json"

    def load(self, session_id: str) -> ReportLearningRecord | None:
        path = self.path_for(session_id)
        if not path.exists():
            return None
        try:
            with path.open(encoding="ascii") as record_file:
                return ReportLearningRecord.model_validate(json.load(record_file))
        except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise ReportLearningError("report learning record unavailable") from error

    def records(self) -> list[ReportLearningRecord]:
        if not self.root.exists():
            return []
        try:
            session_ids = sorted(
                path.stem
                for path in self.root.glob("hermes_*.json")
                if path.is_file()
            )
            return [
                record
                for session_id in session_ids
                if (record := self.load(session_id)) is not None
            ]
        except (OSError, ValueError, ReportLearningError) as error:
            raise ReportLearningError("report learning records unavailable") from error

    def _save_unlocked(self, record: ReportLearningRecord) -> None:
        try:
            _atomic_json_write(
                self.path_for(record.session_id), record.model_dump(mode="json")
            )
        except OSError as error:
            raise ReportLearningError("report learning record unavailable") from error

    def save(self, record: ReportLearningRecord) -> None:
        with self.locked():
            self._save_unlocked(record)

    @contextmanager
    def locked(self) -> Iterator[None]:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / ".report-learning.lock").open(
                "a", encoding="ascii"
            ) as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise ReportLearningError("report learning store unavailable") from error

    def update(
        self,
        session_id: str,
        updater: Callable[
            [ReportLearningRecord | None], ReportLearningRecord
        ],
    ) -> ReportLearningRecord:
        """Apply one locked read-modify-write operation for a report record."""
        with self.locked():
            current = self.load(session_id)
            updated = updater(current)
            if updated.session_id != session_id:
                raise ValueError("report learning update changed session id")
            if updated == current:
                return updated
            self._save_unlocked(updated)
            return updated


def _source_values(session: AnalysisSession) -> dict[str, str]:
    if session.result is None:
        raise ValueError("session is not completed")
    result = session.result
    return {
        "report.market": result.reports.get("market", ""),
        "report.sentiment": result.reports.get("sentiment", ""),
        "report.news": result.reports.get("news", ""),
        "report.fundamentals": result.reports.get("fundamentals", ""),
        "investment_plan": result.investment_plan,
        "trader_plan": result.trader_investment_plan,
        "final_decision": result.final_trade_decision,
        "processed_signal": result.processed_signal,
    }


def _head_tail_excerpt(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    if max_chars <= 0:
        return "", bool(value)
    marker = _TRUNCATION_MARKER
    if max_chars <= len(marker):
        head_chars = (max_chars + 1) // 2
        return value[:head_chars] + value[-(max_chars - head_chars) :], True
    available = max_chars - len(marker)
    head_chars = (available + 1) // 2
    tail_chars = available - head_chars
    return value[:head_chars] + marker + value[-tail_chars:], True


def _outcome_evidence_field(outcome: ReportLearningOutcome) -> ReportEvidenceField:
    excerpt = (
        f"T+{outcome.horizon_days} | verdict={outcome.verdict} | "
        f"raw_return_pct={format(outcome.raw_return_pct, '.10g')} | "
        f"review_date={outcome.review_date.isoformat()} | "
        f"review_id={outcome.review_id}"
    )
    canonical = json.dumps(
        outcome.model_dump(mode="json"),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return ReportEvidenceField(
        name=f"outcome.t{outcome.horizon_days}",
        excerpt=excerpt,
        sha256=hashlib.sha256(canonical).hexdigest(),
        truncated=False,
    )


def _packet_size(packet: ReportEvidencePacket) -> int:
    return len(
        json.dumps(packet.model_dump(mode="json"), ensure_ascii=True).encode("utf-8")
    )


def _packet_from_parts(
    session_id: str,
    symbol: str,
    trade_date,
    action: str,
    revision: int,
    source_digest: str,
    source_values: dict[str, str],
    outcomes: list[ReportLearningOutcome],
) -> ReportEvidencePacket:
    limits = {
        name: min(len(source_values[name]), _INITIAL_EVIDENCE_EXCERPT_CHARS)
        for name in EVIDENCE_FIELD_ORDER
    }

    def assemble() -> ReportEvidencePacket:
        source_fields = []
        for name in EVIDENCE_FIELD_ORDER:
            excerpt, truncated = _head_tail_excerpt(source_values[name], limits[name])
            source_fields.append(
                ReportEvidenceField(
                    name=name,
                    excerpt=excerpt,
                    sha256=hashlib.sha256(
                        source_values[name].encode("utf-8")
                    ).hexdigest(),
                    truncated=truncated,
                )
            )
        return ReportEvidencePacket(
            session_id=session_id,
            symbol=symbol,
            trade_date=trade_date,
            action=action,
            revision=revision,
            source_digest=source_digest,
            outcome_review_ids=[outcome.review_id for outcome in outcomes],
            outcome_horizons=[outcome.horizon_days for outcome in outcomes],
            fields=[
                *source_fields,
                *[_outcome_evidence_field(outcome) for outcome in outcomes],
            ],
        )

    packet = assemble()
    for name in reversed(EVIDENCE_FIELD_ORDER):
        if _packet_size(packet) <= EVIDENCE_PACKET_MAX_BYTES:
            break
        original_limit = limits[name]
        minimum = min(len(source_values[name]), _MIN_EVIDENCE_EXCERPT_CHARS)
        limits[name] = minimum
        packet = assemble()
        if _packet_size(packet) > EVIDENCE_PACKET_MAX_BYTES:
            continue
        low = minimum
        high = original_limit
        while low < high:
            candidate = (low + high + 1) // 2
            limits[name] = candidate
            candidate_packet = assemble()
            if _packet_size(candidate_packet) <= EVIDENCE_PACKET_MAX_BYTES:
                low = candidate
                packet = candidate_packet
            else:
                high = candidate - 1
        limits[name] = low
        packet = assemble()

    if _packet_size(packet) > EVIDENCE_PACKET_MAX_BYTES:
        raise ReportLearningError("evidence packet exceeds byte limit")
    return packet


def _packet_source_metadata(
    packet: ReportEvidencePacket,
) -> list[ReportSourceMetadata]:
    return [
        ReportSourceMetadata(
            name=field.name,
            sha256=field.sha256,
            truncated=field.truncated,
        )
        for field in packet.fields
        if field.name in EVIDENCE_FIELD_ORDER
    ]


def _source_digest(source_values: dict[str, str]) -> str:
    canonical = json.dumps(
        source_values,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _source_field_identity(
    source_fields: list[ReportSourceMetadata],
) -> tuple[tuple[str, str, bool], ...]:
    return tuple(
        (field.name, field.sha256, field.truncated) for field in source_fields
    )


def build_evidence_packet(
    record: ReportLearningRecord,
    session: AnalysisSession,
    revision: int,
) -> ReportEvidencePacket:
    """Rebuild the bounded evidence packet for one immutable revision."""
    if session.status != "completed" or session.result is None:
        raise ValueError("session is not completed")
    identity = (
        session.session_id,
        session.request.symbol,
        session.request.trade_date,
        extract_paper_action(session),
    )
    if identity != (record.session_id, record.symbol, record.trade_date, record.action):
        raise ReportLearningConflict("report learning identity changed")
    source_values = _source_values(session)
    source_digest = _source_digest(source_values)
    if source_digest != record.source_digest:
        raise ReportLearningConflict("report learning source changed")
    if revision < 1 or revision > len(record.revisions):
        raise ReportLearningConflict("report learning revision is unavailable")
    snapshot = record.revisions[revision - 1]
    outcomes = record.outcomes[:revision]
    if snapshot.revision != revision or snapshot.outcome_review_ids != [
        outcome.review_id for outcome in outcomes
    ]:
        raise ReportLearningConflict("report learning revision outcomes changed")
    packet = _packet_from_parts(
        record.session_id,
        record.symbol,
        record.trade_date,
        record.action,
        revision,
        source_digest,
        source_values,
        outcomes,
    )
    if _source_field_identity(snapshot.source_fields) != _source_field_identity(
        _packet_source_metadata(packet)
    ):
        raise ReportLearningConflict("report learning source metadata changed")
    return packet


_CERTAINTY_PATTERN = re.compile(
    r"\b(?:guarantee|guaranteed|prove|proved|proven|cause|caused)\b",
    re.IGNORECASE,
)
_REAL_ORDER_PATTERNS = (
    re.compile(r"\b(?:buy|sell)\s+now\b", re.IGNORECASE),
    re.compile(
        r"\bplace\s+(?:a\s+)?(?:real\s+)?order(?:\s+immediately)?\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bexecute\s+(?:a\s+)?(?:real\s+)?(?:order|trade)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(?:立即|马上|现在).{0,4}(?:买入|卖出|下单|开仓|平仓)"),
    re.compile(r"(?:买入|卖出|开仓|平仓).{0,12}(?:实盘|真实仓位)"),
)
_UNTRUSTED_INSTRUCTION_PATTERN = re.compile(
    r"\b(?:ignore|disregard|override|forget)\s+(?:all\s+)?"
    r"(?:previous|prior|earlier|above|below|current|system|developer)\s+"
    r"(?:instructions?|prompts?|messages?)\b",
    re.IGNORECASE,
)
_CREDENTIAL_PATTERNS = (
    re.compile(
        r"\b(?:api[_ -]?key|api[_ -]?token|access[_ -]?token|password|"
        r"private[_ -]?key)\s*"
        r"(?:is\s+|[:=]\s*)\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:[A-Z0-9]+_)+(?:API_KEY|SECRET(?:_ACCESS)?_KEY|SECRET|"
        r"ACCESS_TOKEN|TOKEN|PASSWORD|PRIVATE_KEY)\s*(?:is\s+|[:=]\s*)\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bSECRET\s*=\s*\S+", re.IGNORECASE),
    re.compile(
        r"\bauthorization\s*:\s*bearer\s+\S+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{8,}\b",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
)
_UNSUPPORTED_SOURCE_PATTERNS = (
    re.compile(
        r"\b(?:later|subsequent|post[- ]decision|live|real[- ]time)\s+"
        r"(?:external\s+)?(?:news|data|sources?|information|prices?|markets?|"
        r"search|lookup|web(?:site)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bexternal\s+(?:news|data|sources?|search|lookup|web(?:site)?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:search(?:ed|ing)?|brows(?:e|ed|ing))\s+(?:the\s+)?web\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:google|bing|duckduckgo|web|internet)\s+"
        r"(?:search|brows(?:e|ed|ing)|lookup)\b.{0,48}"
        r"\b(?:after\s+(?:the\s+)?decision|later|subsequent|post[- ]decision)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:after\s+(?:the\s+)?decision|later|subsequent|post[- ]decision)\b"
        r".{0,48}\b(?:google|bing|duckduckgo|web|internet)\s+"
        r"(?:search|brows(?:e|ed|ing)|lookup)\b",
        re.IGNORECASE,
    ),
)
_HERMES_ENTRY_DELIMITER_PATTERN = re.compile(r"(?:\n§\n|\r\n§\r\n)")


def _reflection_text_values(value) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _reflection_text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _reflection_text_values(item)


def _is_unsafe_reflection_text(text: str) -> bool:
    return (
        _HERMES_ENTRY_DELIMITER_PATTERN.search(text) is not None
        or _UNTRUSTED_INSTRUCTION_PATTERN.search(text) is not None
        or any(pattern.search(text) is not None for pattern in _CREDENTIAL_PATTERNS)
        or any(
            pattern.search(text) is not None
            for pattern in _UNSUPPORTED_SOURCE_PATTERNS
        )
    )


def _validated_reflection(
    reflection_data,
    packet: ReportEvidencePacket,
    outcomes: list[ReportLearningOutcome],
) -> ReportReflection:
    try:
        reflection = ReportReflection.model_validate(reflection_data)
    except (TypeError, ValueError, ValidationError) as error:
        raise ReportReflectionRejected("REFLECTION_SCHEMA_INVALID") from error

    evidence_names = {field.name for field in packet.fields}
    if any(
        reference not in evidence_names
        for hypothesis in reflection.causal_hypotheses
        for reference in hypothesis.evidence
    ):
        raise ReportReflectionRejected("REFLECTION_EVIDENCE_INVALID")

    if any(
        _MEMORY_MARKER_PREFIX in text
        for text in _reflection_text_values(reflection.model_dump(mode="python"))
    ):
        raise ReportReflectionRejected("REFLECTION_UNSAFE_CONTENT")

    expected_horizons = {outcome.horizon_days for outcome in outcomes}
    assessment_horizons = {
        assessment.horizon_days for assessment in reflection.outcome_assessments
    }
    if assessment_horizons != expected_horizons:
        raise ReportReflectionRejected("REFLECTION_OUTCOMES_INVALID")

    verdicts = {outcome.verdict for outcome in outcomes}
    if (
        "correct" in verdicts
        and not reflection.reasoning_strengths
        or "incorrect" in verdicts
        and not reflection.mistakes_or_missed_opportunities
        or "flat" in verdicts
        and not reflection.reasoning_strengths
        or "not_scored" in verdicts
        and not reflection.mistakes_or_missed_opportunities
    ):
        raise ReportReflectionRejected("REFLECTION_VERDICT_SECTIONS_INVALID")

    text_values = reflection.model_dump(mode="python")
    if any(
        _CERTAINTY_PATTERN.search(text) is not None
        or any(pattern.search(text) is not None for pattern in _REAL_ORDER_PATTERNS)
        or _is_unsafe_reflection_text(text)
        for text in _reflection_text_values(text_values)
    ):
        raise ReportReflectionRejected("REFLECTION_UNSAFE_CONTENT")
    return reflection


def _join_items(items: list[str]) -> str:
    return " | ".join(items) if items else "None recorded."


def _clip_text(value: str, max_chars: int, max_bytes: int | None = None) -> str:
    max_bytes = max_chars if max_bytes is None else max_bytes
    if len(value) <= max_chars and len(value.encode("utf-8")) <= max_bytes:
        return value
    if max_chars <= 3 or max_bytes <= 3:
        return value.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")[:max_chars]
    prefix_chars = min(max_chars - 3, len(value))
    prefix_bytes = max_bytes - 3
    prefix = value[:prefix_chars]
    while len(prefix.encode("utf-8")) > prefix_bytes:
        prefix = prefix[:-1]
    return prefix.rstrip() + "..."


def _bounded_lesson(
    required_lines: list[str], optional_lines: list[str]
) -> str:
    disclaimer = (
        "Disclaimer: retrospective hypotheses from paper trading are uncertain; "
        "they are not proof of causation or instructions for real orders."
    )
    available = REPORT_LESSON_MAX_CHARS - len(disclaimer) - 2
    available_bytes = REPORT_LESSON_MAX_CHARS - len(disclaimer.encode("utf-8")) - 2
    body_lines = list(required_lines)
    body = "\n".join(body_lines)
    if len(body) > available or len(body.encode("utf-8")) > available_bytes:
        raise ReportLearningError("required report lesson sections exceed limit")
    for optional in optional_lines:
        candidate = "\n".join([*body_lines, optional])
        if (
            len(candidate) <= available
            and len(candidate.encode("utf-8")) <= available_bytes
        ):
            body_lines.append(optional)
            body = candidate
            continue
        remaining = available - len(body) - 1
        remaining_bytes = available_bytes - len(body.encode("utf-8")) - 1
        if remaining > 3 and remaining_bytes > 3:
            clipped = _clip_text(optional, remaining, remaining_bytes)
            if (
                len(clipped) <= remaining
                and len(clipped.encode("utf-8")) <= remaining_bytes
            ):
                body_lines.append(clipped)
                body = "\n".join(body_lines)
    lesson = f"{body}\n\n{disclaimer}"
    if len(lesson) > REPORT_LESSON_MAX_CHARS or len(lesson.encode("utf-8")) > REPORT_LESSON_MAX_CHARS:
        raise ReportLearningError("report lesson exceeds byte limit")
    return lesson


def _compact_memory_entry(
    record: ReportLearningRecord,
    revision: int,
    outcomes: list[ReportLearningOutcome],
    reflection: ReportReflection,
    maturity_horizon: int,
    market_context: str,
) -> str:
    """Render the small deterministic derivative stored in Hermes memory.

    The project lesson remains the complete bounded artifact. This payload has
    fixed required labels and aggressively clipped values so the marker and all
    outcome identities survive the byte and character limits together.
    """
    marker = REPORT_MEMORY_MARKER.format(session_id=record.session_id)
    outcome_tokens = []
    for outcome in outcomes:
        token = (
            f"T+{outcome.horizon_days} {outcome.verdict} "
            f"{format(outcome.raw_return_pct, '.6g')}%"
        )
        outcome_tokens.append(_clip_text(token, 24, 72))
    outcome_summary = "; ".join(outcome_tokens)

    verdicts = {outcome.verdict for outcome in outcomes}
    if verdicts.intersection({"incorrect", "not_scored"}) and reflection.mistakes_or_missed_opportunities:
        quality_label = "Mistake"
        quality_value = reflection.mistakes_or_missed_opportunities[0]
    elif reflection.reasoning_strengths:
        quality_label = "Strength"
        quality_value = reflection.reasoning_strengths[0]
    elif reflection.mistakes_or_missed_opportunities:
        quality_label = "Mistake"
        quality_value = reflection.mistakes_or_missed_opportunities[0]
    else:
        quality_label = "Strength"
        quality_value = "None recorded."

    hypothesis = reflection.causal_hypotheses[0]
    hypothesis_statement = _clip_text(hypothesis.statement, 14, 42)
    hypothesis_evidence = _clip_text(hypothesis.evidence[0], 18, 54)
    hypothesis_value = (
        f"{hypothesis_statement} [evidence: {hypothesis_evidence}; "
        f"confidence: {hypothesis.confidence}]"
    )
    next_check = reflection.next_decision_checks[0]
    lines = [
        marker,
        (
            f"{record.symbol} {record.trade_date.isoformat()} "
            f"revision {revision} action={record.action}"
        ),
        f"Maturity: T+{maturity_horizon}",
        f"Outcomes: {outcome_summary}",
        f"Decision-time market context: {_clip_text(market_context, 16, 48)}",
        f"{quality_label}: {_clip_text(quality_value, 12, 36)}",
        f"Causal hypothesis: {hypothesis_value}",
        f"Next paper-decision check: {_clip_text(next_check, 14, 42)}",
        (
            "Disclaimer: paper trading only; hypotheses are uncertain; "
            "no real-order instruction."
        ),
    ]

    # Required labels are intentionally retained. The value clips above leave
    # enough headroom for the longest valid session marker and UTF-8 expansion.
    entry = "\n".join(lines)
    if (
        len(entry) > HERMES_REPORT_MEMORY_MAX_CHARS
        or len(entry.encode("utf-8")) > HERMES_REPORT_MEMORY_MAX_BYTES
    ):
        # This is a defensive final pass for future label or schema growth. It
        # clips only variable lines and never touches the marker or labels.
        variable_positions = (4, 5, 6, 7)
        for position in variable_positions:
            line = lines[position]
            label, separator, value = line.partition(" ")
            if not separator:
                continue
            lines[position] = f"{label} {_clip_text(value, 4, 12)}"
            entry = "\n".join(lines)
            if (
                len(entry) <= HERMES_REPORT_MEMORY_MAX_CHARS
                and len(entry.encode("utf-8")) <= HERMES_REPORT_MEMORY_MAX_BYTES
            ):
                break
    if (
        len(entry) > HERMES_REPORT_MEMORY_MAX_CHARS
        or len(entry.encode("utf-8")) > HERMES_REPORT_MEMORY_MAX_BYTES
    ):
        raise ReportLearningError("Hermes report memory entry exceeds limit")
    return entry


def _render_reflection(
    record: ReportLearningRecord,
    revision: int,
    reflection: ReportReflection,
) -> RenderedReportLesson:
    snapshot = record.revisions[revision - 1]
    outcomes_by_id = {outcome.review_id: outcome for outcome in record.outcomes}
    try:
        outcomes = [
            outcomes_by_id[review_id] for review_id in snapshot.outcome_review_ids
        ]
    except KeyError as error:
        raise ReportLearningConflict(
            "report learning revision outcomes changed"
        ) from error
    assessments = {
        item.horizon_days: item.assessment
        for item in reflection.outcome_assessments
    }
    outcome_summary = "; ".join(
        f"T+{outcome.horizon_days} {outcome.verdict} "
        f"({format(outcome.raw_return_pct, '.10g')}%)"
        for outcome in outcomes
    )
    maturity_horizon = max(outcome.horizon_days for outcome in outcomes)
    source_names = {field.name for field in snapshot.source_fields}
    market_source = next(
        (
            name
            for name in ("report.market", "market_report")
            if name in source_names
        ),
        "report.market",
    )
    required_lines = [
        (
            f"{record.symbol} paper report lesson, {record.trade_date.isoformat()}, "
            f"revision {revision}, action {record.action}"
        ),
        f"Maturity: T+{maturity_horizon}",
        f"Outcomes: {outcome_summary}",
        f"Archived market context: {market_source} evidence captured at decision time.",
    ]
    required_lines.append(
        "Outcome assessments: "
        + " | ".join(
            f"T+{outcome.horizon_days}: "
            f"{_clip_text(assessments[outcome.horizon_days], 120, 120)}"
            for outcome in outcomes
        )
    )
    required_lines.append(
        "Causal hypotheses: "
        + " | ".join(
            f"{_clip_text(item.statement, 100, 100)} "
            f"[evidence: {', '.join(item.evidence)}; confidence: {item.confidence}]"
            for item in reflection.causal_hypotheses
        )
    )
    required_lines.append(
        "Next paper-decision checks: "
        + " | ".join(
            _clip_text(item, 120, 100)
            for item in reflection.next_decision_checks
        )
    )
    contexts = (
        ("Decision thesis", reflection.decision_thesis),
        ("Overall assessment", reflection.overall_assessment),
        ("Technical context", reflection.technical_context),
        ("Sentiment context", reflection.sentiment_context),
        ("News context", reflection.news_context),
        ("Fundamental context", reflection.fundamental_context),
        ("Reasoning strengths", _join_items(reflection.reasoning_strengths)),
        (
            "Mistakes or missed opportunities",
            _join_items(reflection.mistakes_or_missed_opportunities),
        ),
    )
    optional_lines = [
        f"{label}: {_clip_text(value, 280, 500)}"
        for label, value in contexts
        if value is not None
    ]
    market_context = reflection.technical_context or (
        "report.market archived at decision time; no optional technical context."
    )
    required_lines.insert(
        4,
        f"Decision-time market context: {_clip_text(market_context, 220, 300)}",
    )
    lesson = _bounded_lesson(required_lines, optional_lines)
    lesson = lesson.replace(_MEMORY_MARKER_PREFIX, "[report marker omitted]")
    memory_entry = _compact_memory_entry(
        record,
        revision,
        outcomes,
        reflection,
        maturity_horizon,
        market_context,
    )
    return RenderedReportLesson(lesson=lesson, hermes_memory_entry=memory_entry)


def render_report_lesson(
    record: ReportLearningRecord, revision: int
) -> RenderedReportLesson:
    """Render stable project and Hermes payload strings without calling memory."""
    if revision < 1 or revision > len(record.revisions):
        raise ReportLearningConflict("report learning revision is unavailable")
    snapshot = record.revisions[revision - 1]
    if snapshot.revision != revision or snapshot.reflection is None:
        raise ReportLearningConflict("report learning reflection is unavailable")
    return _render_reflection(record, revision, snapshot.reflection)


def _replace_revision(
    record: ReportLearningRecord,
    revision: int,
    snapshot: ReportLearningRevision,
    *,
    reflected_revision: int | None = None,
) -> ReportLearningRecord:
    revisions = [item.model_copy(deep=True) for item in record.revisions]
    revisions[revision - 1] = snapshot
    return ReportLearningRecord.model_validate(
        {
            **record.model_dump(mode="python"),
            "reflected_revision": (
                record.reflected_revision
                if reflected_revision is None
                else reflected_revision
            ),
            "revisions": revisions,
            "updated_at": snapshot.updated_at,
        }
    )


def _save_reflection_rejection(
    report_store: ReportLearningStore,
    record: ReportLearningRecord,
    revision: int,
    snapshot: ReportLearningRevision,
    error: ReportReflectionRejected,
    attempt_date: date,
) -> None:
    now = utc_now()
    attempts = min(
        snapshot.reflection_attempt_count + 1, MAX_REFLECTION_ATTEMPTS
    )
    rejected_snapshot = snapshot.model_copy(
        update={
            "reflection_state": (
                "attention_required"
                if attempts >= MAX_REFLECTION_ATTEMPTS
                else "pending"
            ),
            "reflection_attempt_count": attempts,
            "last_reflection_attempt_date": attempt_date,
            "last_error_code": error.error_code,
            "updated_at": now,
        }
    )
    report_store._save_unlocked(
        _replace_revision(record, revision, rejected_snapshot)
    )


def submit_report_reflection(
    report_store: ReportLearningStore,
    learning_store: LearningStore,
    session: AnalysisSession,
    expected_revision: int,
    reflection_data,
    *,
    attempt_date: date | None = None,
) -> ReportLearningRecord:
    """Validate and persist one reflection, then index its deterministic lesson."""
    selected_attempt_date = (
        datetime.now(timezone.utc).date()
        if attempt_date is None
        else attempt_date
    )
    if type(selected_attempt_date) is not date:
        raise ValueError("invalid report reflection attempt date")
    with report_store.locked():
        current = report_store.load(session.session_id)
        if current is None:
            raise ReportLearningConflict("report learning record is unavailable")
        if expected_revision < 1 or expected_revision > len(current.revisions):
            raise ReportLearningConflict("report learning revision is unavailable")

        snapshot = current.revisions[expected_revision - 1]
        if expected_revision <= current.reflected_revision:
            build_evidence_packet(current, session, expected_revision)
            try:
                repeated = ReportReflection.model_validate(reflection_data)
            except (TypeError, ValueError, ValidationError) as error:
                raise ReportLearningConflict(
                    "reflected report learning revision differs"
                ) from error
            if repeated != snapshot.reflection:
                raise ReportLearningConflict(
                    "reflected report learning revision differs"
                )
            updated = current
        else:
            if (
                expected_revision != current.reflected_revision + 1
                or snapshot.revision != expected_revision
            ):
                raise ReportLearningConflict(
                    "report learning revision is not the next pending snapshot"
                )
            if snapshot.reflection_state != "pending":
                raise ReportLearningConflict(
                    "report learning revision is not the next pending snapshot"
                )
            if (
                snapshot.last_reflection_attempt_date is not None
                and selected_attempt_date
                <= snapshot.last_reflection_attempt_date
            ):
                raise ReportReflectionRetryDeferred(
                    "report reflection retry deferred until a later UTC date"
                )
            try:
                ReportReflection.model_validate(reflection_data)
            except (TypeError, ValueError, ValidationError) as validation_error:
                rejected = ReportReflectionRejected("REFLECTION_SCHEMA_INVALID")
                _save_reflection_rejection(
                    report_store,
                    current,
                    expected_revision,
                    snapshot,
                    rejected,
                    selected_attempt_date,
                )
                raise rejected from validation_error
            packet = build_evidence_packet(current, session, expected_revision)
            outcomes_by_id = {
                outcome.review_id: outcome for outcome in current.outcomes
            }
            try:
                outcomes = [
                    outcomes_by_id[review_id]
                    for review_id in snapshot.outcome_review_ids
                ]
            except KeyError as error:
                raise ReportLearningConflict(
                    "report learning revision outcomes changed"
                ) from error
            try:
                reflection = _validated_reflection(
                    reflection_data, packet, outcomes
                )
            except ReportReflectionRejected as error:
                _save_reflection_rejection(
                    report_store,
                    current,
                    expected_revision,
                    snapshot,
                    error,
                    selected_attempt_date,
                )
                raise

            rendered = _render_reflection(current, expected_revision, reflection)
            now = utc_now()
            ready_snapshot = snapshot.model_copy(
                update={
                    "reflection_state": "ready",
                    "memory_state": (
                        "add_pending"
                        if expected_revision == 1
                        else "replace_pending"
                    ),
                    "last_error_code": None,
                    "reflection": reflection,
                    "lesson": rendered.lesson,
                    "hermes_memory_entry": rendered.hermes_memory_entry,
                    "updated_at": now,
                }
            )
            updated = _replace_revision(
                current,
                expected_revision,
                ready_snapshot,
                reflected_revision=expected_revision,
            )
            report_store._save_unlocked(updated)

    learning_store.upsert_report(updated)
    return updated


def _validate_review_identity(
    session: AnalysisSession, review: PaperDecisionReview
) -> None:
    if session.status != "completed" or session.result is None:
        raise ValueError("session is not completed")
    expected_identity = (
        session.session_id,
        session.request.symbol,
        session.request.trade_date,
        extract_paper_action(session),
    )
    review_identity = (
        review.session_id,
        review.symbol,
        review.trade_date,
        review.action,
    )
    if review_identity != expected_identity:
        raise ReportLearningConflict("review identity conflicts with report")
    if review.horizon_days not in _ALLOWED_HORIZONS:
        raise ValueError("review horizon is not supported")


def _validate_review_dates(review: PaperDecisionReview) -> None:
    expected_review_date = review.trade_date + timedelta(days=review.horizon_days)
    if (
        review.review_date != expected_review_date
        or review.entry_price.date != review.trade_date
        or review.review_price.date != review.review_date
    ):
        raise ValueError("review date does not match its horizon")


def _is_pristine_pending_revision(revision: ReportLearningRevision) -> bool:
    return (
        revision.reflection_state == "pending"
        and revision.memory_state == "blocked"
        and revision.reflection_attempt_count == 0
        and revision.last_error_code is None
        and revision.reflection is None
        and revision.lesson is None
        and revision.hermes_memory_entry is None
        and revision.verified_at is None
    )


def record_review_fact(
    store: ReportLearningStore,
    session: AnalysisSession,
    review: PaperDecisionReview,
) -> ReportLearningRecord:
    """Append one immutable paper-review fact to its report-level record."""
    _validate_review_identity(session, review)
    source_values = _source_values(session)
    source_digest = _source_digest(source_values)
    action = extract_paper_action(session)
    outcome = ReportLearningOutcome(
        review_id=review.review_id,
        horizon_days=review.horizon_days,
        review_date=review.review_date,
        raw_return_pct=review.raw_return_pct,
        verdict=review.verdict,
    )

    def aggregate(current: ReportLearningRecord | None) -> ReportLearningRecord:
        if current is not None:
            persisted_identity = (
                current.session_id,
                current.symbol,
                current.trade_date,
                current.action,
            )
            incoming_identity = (
                session.session_id,
                session.request.symbol,
                session.request.trade_date,
                action,
            )
            if persisted_identity != incoming_identity:
                raise ReportLearningConflict("report learning identity changed")
            if current.source_digest != source_digest:
                raise ReportLearningConflict("report learning source changed")
            for snapshot in current.revisions:
                packet = _packet_from_parts(
                    current.session_id,
                    current.symbol,
                    current.trade_date,
                    current.action,
                    snapshot.revision,
                    source_digest,
                    source_values,
                    current.outcomes[: snapshot.revision],
                )
                if _source_field_identity(
                    snapshot.source_fields
                ) != _source_field_identity(_packet_source_metadata(packet)):
                    raise ReportLearningConflict(
                        "report learning source metadata changed"
                    )
            existing_outcome = next(
                (
                    item
                    for item in current.outcomes
                    if item.review_id == review.review_id
                ),
                None,
            )
            if existing_outcome is not None:
                if existing_outcome != outcome:
                    raise ReportLearningConflict(
                        "report learning review outcome changed"
                    )
                _validate_review_dates(review)
                return current
            if len(current.outcomes) >= MAX_REPORT_REVISIONS:
                raise ReportLearningConflict("report learning outcome limit reached")
            if any(
                outcome.horizon_days == review.horizon_days
                for outcome in current.outcomes
            ):
                raise ReportLearningConflict("report learning horizon already recorded")

        _validate_review_dates(review)
        outcomes = sorted(
            [*(current.outcomes if current is not None else []), outcome],
            key=lambda item: item.horizon_days,
        )
        now = utc_now()
        existing_revisions = current.revisions if current is not None else []
        revisions: list[ReportLearningRevision] = []
        for position in range(1, len(outcomes) + 1):
            outcome_review_ids = [
                item.review_id for item in outcomes[:position]
            ]
            existing = (
                existing_revisions[position - 1]
                if position <= len(existing_revisions)
                else None
            )
            if (
                existing is not None
                and existing.outcome_review_ids == outcome_review_ids
            ):
                revisions.append(existing)
                continue
            if existing is not None and not _is_pristine_pending_revision(existing):
                raise ReportLearningConflict(
                    "processed report learning revision cannot be rebuilt"
                )
            revisions.append(
                ReportLearningRevision(
                    revision=position,
                    outcome_review_ids=outcome_review_ids,
                    reflection_state="pending",
                    memory_state="blocked",
                    source_fields=_packet_source_metadata(
                        _packet_from_parts(
                            session.session_id,
                            session.request.symbol,
                            session.request.trade_date,
                            action,
                            position,
                            source_digest,
                            source_values,
                            outcomes[:position],
                        )
                    ),
                    created_at=now,
                    updated_at=now,
                )
            )

        revision_number = len(outcomes)
        return ReportLearningRecord(
            session_id=session.session_id,
            symbol=session.request.symbol,
            trade_date=session.request.trade_date,
            action=action,
            source_digest=source_digest,
            desired_revision=revision_number,
            reflected_revision=(current.reflected_revision if current else 0),
            confirmed_revision=(current.confirmed_revision if current else 0),
            outcomes=outcomes,
            revisions=revisions,
            created_at=current.created_at if current is not None else now,
            updated_at=now,
        )

    return store.update(session.session_id, aggregate)
