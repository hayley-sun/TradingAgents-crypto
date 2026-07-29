import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingagents.integrations.hermes_maintenance import run_maintenance
from tradingagents.integrations.hermes_mcp import SessionStore
from tradingagents.integrations.schemas import AnalysisRequest


DEAD_ID = "hermes_0123456789abcdea"
LIVE_ID = "hermes_0123456789abcdef"
UNTRACKED_ID = "hermes_0123456789abcdec"
DEAD_PID = 4101
LIVE_PID = 4102
NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)


def request() -> AnalysisRequest:
    return AnalysisRequest(
        symbol="BTC",
        trade_date="2026-07-28",
        analysts=["market"],
        research_depth=1,
        llm_provider="ollama",
        quick_model="quick",
        deep_model="deep",
    )


def active_session(store: SessionStore, session_id: str, worker_pid: int | None) -> None:
    session = store.create(session_id, request(), status="running")
    store.save(session.model_copy(update={"worker_pid": worker_pid}))


class HermesMaintenanceTests(unittest.TestCase):
    def test_maintenance_marks_dead_pid_but_keeps_live_and_untracked_sessions(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            store = SessionStore(root / "sessions")
            active_session(store, DEAD_ID, DEAD_PID)
            active_session(store, LIVE_ID, LIVE_PID)
            active_session(store, UNTRACKED_ID, None)

            report = run_maintenance(
                store,
                root / "logs",
                worker_is_alive=lambda pid: pid == LIVE_PID,
                now=NOW,
            )

            dead_session = store.load(DEAD_ID)
            live_session = store.load(LIVE_ID)
            untracked_session = store.load(UNTRACKED_ID)

        self.assertEqual(dead_session.status, "failed")
        self.assertEqual(dead_session.error.code, "WORKER_EXITED")
        self.assertEqual(live_session.status, "running")
        self.assertEqual(untracked_session.status, "running")
        self.assertEqual(report.repaired_session_ids, [DEAD_ID])
        self.assertEqual(report.untracked_session_ids, [UNTRACKED_ID])

    def test_maintenance_prunes_only_expired_worker_logs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            store = SessionStore(root / "sessions")
            logs_root = root / "logs"
            logs_root.mkdir(parents=True)
            expired_log = logs_root / f"{DEAD_ID}.log"
            recent_log = logs_root / f"{LIVE_ID}.log"
            expired_log.write_text("expired", encoding="ascii")
            recent_log.write_text("recent", encoding="ascii")
            os.utime(expired_log, ((NOW - timedelta(days=15)).timestamp(),) * 2)
            os.utime(recent_log, ((NOW - timedelta(days=13)).timestamp(),) * 2)

            report = run_maintenance(store, logs_root, now=NOW, log_retention_days=14)

            self.assertFalse(expired_log.exists())
            self.assertTrue(recent_log.exists())

        self.assertEqual(report.pruned_log_count, 1)

    def test_dry_run_leaves_dead_sessions_and_expired_logs_unchanged(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "results" / "hermes"
            store = SessionStore(root / "sessions")
            active_session(store, DEAD_ID, DEAD_PID)
            logs_root = root / "logs"
            logs_root.mkdir(parents=True)
            expired_log = logs_root / f"{DEAD_ID}.log"
            expired_log.write_text("expired", encoding="ascii")
            os.utime(expired_log, ((NOW - timedelta(days=15)).timestamp(),) * 2)

            report = run_maintenance(
                store,
                logs_root,
                worker_is_alive=lambda _pid: False,
                now=NOW,
                dry_run=True,
            )

            session = store.load(DEAD_ID)
            self.assertEqual(session.status, "running")
            self.assertTrue(expired_log.exists())

        self.assertEqual(report.repaired_session_ids, [DEAD_ID])
        self.assertEqual(report.pruned_log_count, 1)


if __name__ == "__main__":
    unittest.main()
