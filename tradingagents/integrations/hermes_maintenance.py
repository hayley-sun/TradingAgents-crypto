"""Scheduled maintenance for persisted Hermes analysis workers and logs."""

import argparse
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from tradingagents.integrations.hermes_mcp import (
    PROJECT_ROOT,
    SessionStore,
    reconcile_session_worker,
)
from tradingagents.integrations.schemas import is_valid_session_id


DEFAULT_LOG_RETENTION_DAYS = 14


class MaintenanceError(RuntimeError):
    """Raised when persisted worker state cannot be maintained safely."""


@dataclass(frozen=True)
class MaintenanceReport:
    """Safe summary of one worker and log maintenance pass."""

    scanned_session_count: int
    repaired_session_ids: list[str]
    untracked_session_ids: list[str]
    pruned_log_count: int
    dry_run: bool


def _session_paths(store: SessionStore) -> list[Path]:
    if not store.root.exists():
        return []
    return sorted(
        path
        for path in store.root.glob("*.json")
        if is_valid_session_id(path.stem)
    )


def run_maintenance(
    store: SessionStore,
    logs_root: Path,
    worker_is_alive: Callable[[int], bool] | None = None,
    now: datetime | None = None,
    log_retention_days: int = DEFAULT_LOG_RETENTION_DAYS,
    dry_run: bool = False,
) -> MaintenanceReport:
    """Repair tracked dead workers and prune only expired worker log files."""
    if log_retention_days < 1:
        raise ValueError("log retention days must be positive")

    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None:
        raise ValueError("maintenance time must include a timezone")
    cutoff = current_time - timedelta(days=log_retention_days)
    repaired_session_ids: list[str] = []
    untracked_session_ids: list[str] = []
    session_paths = _session_paths(store)

    try:
        for path in session_paths:
            session = store.load(path.stem)
            if session is None or session.status not in {"queued", "running"}:
                continue
            if session.worker_pid is None:
                untracked_session_ids.append(session.session_id)
                continue
            _, repaired = reconcile_session_worker(
                session,
                store,
                worker_is_alive=worker_is_alive,
                persist=not dry_run,
            )
            if repaired:
                repaired_session_ids.append(session.session_id)
    except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
        raise MaintenanceError("Hermes maintenance could not inspect session state") from error

    pruned_log_count = 0
    logs_directory = Path(logs_root).expanduser().resolve()
    try:
        if logs_directory.exists():
            for log_path in logs_directory.glob("*.log"):
                if not log_path.is_file() or log_path.stat().st_mtime >= cutoff.timestamp():
                    continue
                pruned_log_count += 1
                if not dry_run:
                    log_path.unlink()
    except OSError as error:
        raise MaintenanceError("Hermes maintenance could not prune worker logs") from error

    return MaintenanceReport(
        scanned_session_count=len(session_paths),
        repaired_session_ids=repaired_session_ids,
        untracked_session_ids=untracked_session_ids,
        pruned_log_count=pruned_log_count,
        dry_run=dry_run,
    )


def _default_results_dir() -> Path:
    configured = os.getenv("TRADINGAGENTS_RESULTS_DIR")
    return Path(configured) if configured else PROJECT_ROOT / "results"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Maintain Hermes worker sessions and worker logs."
    )
    parser.add_argument("--results-dir", type=Path, default=_default_results_dir())
    parser.add_argument(
        "--log-retention-days", type=int, default=DEFAULT_LOG_RETENTION_DAYS
    )
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)
    results_dir = arguments.results_dir.expanduser().resolve()

    try:
        report = run_maintenance(
            SessionStore(results_dir / "hermes" / "sessions"),
            results_dir / "hermes" / "logs",
            log_retention_days=arguments.log_retention_days,
            dry_run=arguments.dry_run,
        )
    except (MaintenanceError, ValueError):
        print(json.dumps({"ok": False}))
        return 1

    print(
        json.dumps(
            {
                "ok": True,
                "scanned_session_count": report.scanned_session_count,
                "repaired_session_ids": report.repaired_session_ids,
                "untracked_session_ids": report.untracked_session_ids,
                "pruned_log_count": report.pruned_log_count,
                "dry_run": report.dry_run,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
