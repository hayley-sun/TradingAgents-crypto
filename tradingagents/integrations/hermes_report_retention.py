"""Durable, per-symbol retirement history for bounded Hermes report memory."""

import json
import os
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import fcntl
from pydantic import ValidationError

from tradingagents.integrations.schemas import (
    ReportLearningRecord,
    ReportMemoryRetirement,
    ReportMemoryRetirementJournal,
    utc_now,
)


REPORT_MEMORY_MARKER = "[TradingAgents paper report: {session_id}]"


class ReportMemoryRetirementError(RuntimeError):
    """Raised when the retirement journal cannot be read or written safely."""


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


class ReportMemoryRetirementStore:
    """Filesystem-backed, append-only retirement journals keyed by symbol."""

    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        try:
            return ReportMemoryRetirementJournal(symbol=symbol).symbol
        except ValidationError as error:
            raise ValueError("invalid retirement journal symbol") from error

    def path_for(self, symbol: str) -> Path:
        return self.root / f"{self._normalize_symbol(symbol)}.json"

    def lock_path_for(self, symbol: str) -> Path:
        return Path(f"{self.path_for(symbol)}.lock")

    def load(self, symbol: str) -> ReportMemoryRetirementJournal | None:
        normalized_symbol = self._normalize_symbol(symbol)
        path = self.path_for(normalized_symbol)
        if not path.exists():
            return None
        try:
            with path.open(encoding="ascii") as journal_file:
                journal = ReportMemoryRetirementJournal.model_validate(
                    json.load(journal_file)
                )
        except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
            raise ReportMemoryRetirementError(
                "report memory retirement journal unavailable"
            ) from error
        if journal.symbol != normalized_symbol:
            raise ReportMemoryRetirementError(
                "report memory retirement journal symbol mismatch"
            )
        return journal

    def journals(self) -> list[ReportMemoryRetirementJournal]:
        if not self.root.exists():
            return []
        try:
            symbols = sorted(path.stem for path in self.root.glob("*.json") if path.is_file())
            return [
                journal
                for symbol in symbols
                if (journal := self.load(symbol)) is not None
            ]
        except (OSError, ValueError, ReportMemoryRetirementError) as error:
            raise ReportMemoryRetirementError(
                "report memory retirement journals unavailable"
            ) from error

    def _save_unlocked(self, journal: ReportMemoryRetirementJournal) -> None:
        try:
            _atomic_json_write(
                self.path_for(journal.symbol), journal.model_dump(mode="json")
            )
        except OSError as error:
            raise ReportMemoryRetirementError(
                "report memory retirement journal unavailable"
            ) from error

    def save(self, journal: ReportMemoryRetirementJournal) -> None:
        with self.locked(journal.symbol):
            self._save_unlocked(journal)

    @contextmanager
    def locked(self, symbol: str) -> Iterator[None]:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self.lock_path_for(symbol).open(
                "a", encoding="ascii"
            ) as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as error:
            raise ReportMemoryRetirementError(
                "report memory retirement store unavailable"
            ) from error

    def sync_symbol(
        self,
        symbol: str,
        records: Iterable[ReportLearningRecord],
    ) -> list[ReportMemoryRetirement]:
        """Append candidates older than the five newest confirmed T+15 reports."""
        normalized_symbol = self._normalize_symbol(symbol)
        completed = sorted(
            (
                record
                for record in records
                if record.symbol == normalized_symbol
                and record.confirmed_revision == 3
                and len(record.revisions) >= 3
                and record.revisions[2].memory_state == "confirmed"
            ),
            key=lambda record: (record.trade_date, record.session_id),
            reverse=True,
        )
        older_completed = reversed(completed[5:])

        with self.locked(normalized_symbol):
            journal = self.load(normalized_symbol)
            existing_items = [] if journal is None else journal.items
            existing_session_ids = {item.session_id for item in existing_items}
            additions = [
                ReportMemoryRetirement(
                    session_id=record.session_id,
                    symbol=normalized_symbol,
                    trade_date=record.trade_date,
                    marker=REPORT_MEMORY_MARKER.format(session_id=record.session_id),
                    state="pending",
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                for record in older_completed
                if record.session_id not in existing_session_ids
            ]
            if not additions:
                return list(existing_items)
            updated = ReportMemoryRetirementJournal(
                symbol=normalized_symbol,
                items=[*existing_items, *additions],
            )
            self._save_unlocked(updated)
            return list(updated.items)
