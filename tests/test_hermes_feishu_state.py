import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from tradingagents.integrations.hermes_feishu_state import (
    DeliveryRecord,
    NotificationAlreadyRunning,
    NotificationEvent,
    NotificationStateError,
    NotificationStateStore,
    initialized_state,
    prune_delivered,
    retry_delay,
)


def report_event(event_id="report:2026-08-18:" + "a" * 64):
    return NotificationEvent(
        event_id=event_id,
        kind="report",
        created_at=datetime(2026, 8, 18, 4, 0, tzinfo=timezone.utc),
        trade_date=date(2026, 8, 18),
        report_sha256="a" * 64,
        batch_state="ready",
    )


class FeishuNotificationStateTests(unittest.TestCase):
    def test_state_rejects_event_with_wrong_fields(self):
        with self.assertRaises(ValidationError):
            NotificationEvent(
                event_id="bad",
                kind="execution_failure",
                created_at=datetime.now(timezone.utc),
                trade_date=date(2026, 8, 18),
            )

    def test_store_writes_owner_only_modes_and_round_trips(self):
        with TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "feishu_notifications")
            state = initialized_state(
                datetime(2026, 8, 18, tzinfo=timezone.utc), {"a" * 12: []}, []
            )

            store.save(state)

            self.assertEqual(store.load(), state)
            self.assertEqual(store.load_optional(), state)
            self.assertEqual(S_IMODE(store.root.stat().st_mode), 0o700)
            self.assertEqual(S_IMODE(store.path.stat().st_mode), 0o600)

    def test_load_optional_returns_none_only_for_missing_state(self):
        with TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "feishu_notifications")

            self.assertIsNone(store.load_optional())

    def test_atomic_failure_preserves_valid_bytes(self):
        with TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "feishu_notifications")
            state = initialized_state(
                datetime(2026, 8, 18, tzinfo=timezone.utc), {"a" * 12: []}, []
            )
            store.save(state)
            before = store.path.read_bytes()

            with patch("os.replace", side_effect=OSError("disk failure")):
                with self.assertRaises(NotificationStateError):
                    store.save(
                        state.model_copy(update={"seen_report_event_ids": ["new"]})
                    )

            self.assertEqual(store.path.read_bytes(), before)

    def test_second_lock_raises_already_running(self):
        with TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "feishu_notifications")

            with store.lock():
                with self.assertRaises(NotificationAlreadyRunning):
                    with store.lock():
                        self.fail("second lock unexpectedly acquired")

    def test_malformed_json_is_rejected_without_rewrite(self):
        with TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "feishu_notifications")
            store.root.mkdir(mode=0o700)
            malformed = b'{"schema_version":'
            store.path.write_bytes(malformed)

            with self.assertRaises(NotificationStateError):
                store.load_optional()

            self.assertEqual(store.path.read_bytes(), malformed)

    def test_prune_keeps_compact_ids_and_pending_records(self):
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        old = DeliveryRecord(
            event=report_event("old"),
            attempt_count=1,
            next_attempt_at=now,
            delivered_at=now - timedelta(days=91),
            last_result="delivered",
        )
        pending = DeliveryRecord(event=report_event("pending"), next_attempt_at=now)
        state = initialized_state(now, {"a" * 12: []}, ["old", "pending"])
        state = state.model_copy(
            update={"deliveries": {"old": old, "pending": pending}}
        )

        pruned = prune_delivered(state, now)

        self.assertNotIn("old", pruned.deliveries)
        self.assertIn("old", pruned.seen_report_event_ids)
        self.assertIn("pending", pruned.deliveries)
        self.assertEqual(pruned.execution_cursors, state.execution_cursors)
        self.assertEqual(pruned.seen_execution_ids, state.seen_execution_ids)

    def test_prune_keeps_delivery_at_ninety_day_boundary(self):
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        boundary = DeliveryRecord(
            event=report_event("boundary"),
            attempt_count=1,
            next_attempt_at=now,
            delivered_at=now - timedelta(days=90),
            last_result="delivered",
        )
        state = initialized_state(now, {}, ["boundary"])
        state = state.model_copy(update={"deliveries": {"boundary": boundary}})

        self.assertIn("boundary", prune_delivered(state, now).deliveries)

    def test_retry_delay_uses_capped_schedule(self):
        expected_minutes = (5, 10, 20, 40, 60, 60)

        self.assertEqual(
            [retry_delay(attempt).total_seconds() / 60 for attempt in range(1, 7)],
            list(expected_minutes),
        )

    def test_retry_delay_rejects_non_positive_attempts(self):
        for attempt in (0, -1):
            with self.subTest(attempt=attempt), self.assertRaises(ValueError):
                retry_delay(attempt)


if __name__ == "__main__":
    unittest.main()
