import json
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from stat import S_IMODE, S_ISDIR
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from tradingagents.integrations.hermes_feishu_state import (
    DeliveryRecord,
    MAX_ATTEMPT_COUNT,
    NotificationAlreadyRunning,
    NotificationEvent,
    NotificationState,
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
    def test_delivery_attempt_count_has_strict_inclusive_bound(self):
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        bounded = DeliveryRecord(
            event=report_event(),
            attempt_count=MAX_ATTEMPT_COUNT,
            next_attempt_at=now,
        )

        self.assertEqual(bounded.attempt_count, MAX_ATTEMPT_COUNT)
        for attempt_count in (
            True,
            "1",
            1.0,
            -1,
            MAX_ATTEMPT_COUNT + 1,
        ):
            with self.subTest(attempt_count=attempt_count), self.assertRaises(
                ValidationError
            ):
                DeliveryRecord(
                    event=report_event(),
                    attempt_count=attempt_count,
                    next_attempt_at=now,
                )

    def test_store_rejects_non_strict_or_out_of_range_attempt_counts(self):
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        event = report_event()
        delivery = DeliveryRecord(event=event, next_attempt_at=now)
        state = initialized_state(now, {}, [event.event_id]).model_copy(
            update={"deliveries": {event.event_id: delivery}}
        )
        invalid_values = (True, "1", 1.0, -1, MAX_ATTEMPT_COUNT + 1)

        for attempt_count in invalid_values:
            with self.subTest(attempt_count=attempt_count):
                with TemporaryDirectory() as directory:
                    store = NotificationStateStore(
                        Path(directory) / "feishu_notifications"
                    )
                    store.save(state)
                    payload = json.loads(store.path.read_text(encoding="ascii"))
                    payload["deliveries"][event.event_id][
                        "attempt_count"
                    ] = attempt_count
                    store.path.write_text(
                        json.dumps(payload, ensure_ascii=True),
                        encoding="ascii",
                    )
                    malformed = store.path.read_bytes()

                    for load in (store.load, store.load_optional):
                        with self.subTest(load=load.__name__):
                            with self.assertRaises(
                                NotificationStateError
                            ) as raised:
                                load()
                            self.assertEqual(
                                str(raised.exception),
                                "notification state unavailable",
                            )
                            self.assertEqual(
                                store.path.read_bytes(), malformed
                            )

    def test_store_save_rejects_manually_invalid_attempt_counts(self):
        now = datetime(2026, 8, 18, tzinfo=timezone.utc)
        event = report_event()
        delivery = DeliveryRecord(event=event, next_attempt_at=now)
        state = initialized_state(now, {}, [event.event_id]).model_copy(
            update={"deliveries": {event.event_id: delivery}}
        )

        with TemporaryDirectory() as directory:
            store = NotificationStateStore(
                Path(directory) / "feishu_notifications"
            )
            store.save(state)
            valid_bytes = store.path.read_bytes()
            for attempt_count in (
                True,
                "1",
                1.0,
                -1,
                MAX_ATTEMPT_COUNT + 1,
            ):
                with self.subTest(attempt_count=attempt_count):
                    invalid_delivery = delivery.model_copy(
                        update={"attempt_count": attempt_count}
                    )
                    invalid_state = state.model_copy(
                        update={
                            "deliveries": {
                                event.event_id: invalid_delivery
                            }
                        }
                    )
                    with self.assertRaises(NotificationStateError):
                        store.save(invalid_state)
                    self.assertEqual(store.path.read_bytes(), valid_bytes)

    def test_models_reject_naive_runtime_datetimes(self):
        aware = datetime(2026, 8, 18, tzinfo=timezone.utc)
        naive = aware.replace(tzinfo=None)
        event_values = report_event().model_dump()
        state_values = initialized_state(aware, {}, []).model_dump()
        invalid_models = {
            "created_at": lambda: NotificationEvent.model_validate(
                {**event_values, "created_at": naive}
            ),
            "initialized_at": lambda: NotificationState.model_validate(
                {**state_values, "initialized_at": naive}
            ),
            "next_attempt_at": lambda: DeliveryRecord(
                event=report_event(), next_attempt_at=naive
            ),
            "delivered_at": lambda: DeliveryRecord(
                event=report_event(), next_attempt_at=aware, delivered_at=naive
            ),
        }

        for field_name, build_model in invalid_models.items():
            with self.subTest(field_name=field_name), self.assertRaises(
                ValidationError
            ):
                build_model()

    def test_state_operations_reject_naive_now(self):
        aware = datetime(2026, 8, 18, tzinfo=timezone.utc)
        naive = aware.replace(tzinfo=None)

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            initialized_state(naive, {}, [])

        state = initialized_state(aware, {}, [])
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            prune_delivered(state, naive)

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

    def test_load_optional_rejects_dangling_state_symlink(self):
        with TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "feishu_notifications")
            store.root.mkdir(mode=0o700)
            store.path.symlink_to(store.root / "missing-state.json")

            with self.assertRaises(NotificationStateError):
                store.load_optional()

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

    def test_save_fsyncs_directory_after_atomic_replace(self):
        with TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "feishu_notifications")
            state = initialized_state(
                datetime(2026, 8, 18, tzinfo=timezone.utc), {}, []
            )
            events = []
            real_fsync = os.fsync
            real_replace = os.replace

            def record_fsync(file_descriptor):
                is_directory = S_ISDIR(os.fstat(file_descriptor).st_mode)
                descriptor_kind = (
                    "directory" if is_directory else "file"
                )
                events.append(f"fsync:{descriptor_kind}")
                real_fsync(file_descriptor)

            def record_replace(source, destination):
                events.append("replace")
                real_replace(source, destination)

            with patch("os.fsync", side_effect=record_fsync), patch(
                "os.replace", side_effect=record_replace
            ):
                store.save(state)

            self.assertEqual(events, ["fsync:file", "replace", "fsync:directory"])

    def test_directory_fsync_failure_raises_safe_state_error(self):
        with TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "feishu_notifications")
            state = initialized_state(
                datetime(2026, 8, 18, tzinfo=timezone.utc), {}, []
            )
            real_fsync = os.fsync

            def fail_directory_fsync(file_descriptor):
                if S_ISDIR(os.fstat(file_descriptor).st_mode):
                    raise OSError("directory sync failure")
                real_fsync(file_descriptor)

            with patch("os.fsync", side_effect=fail_directory_fsync):
                with self.assertRaises(NotificationStateError) as raised:
                    store.save(state)

            self.assertEqual(str(raised.exception), "notification state unavailable")

    def test_second_lock_raises_already_running(self):
        with TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "feishu_notifications")

            with store.lock():
                with self.assertRaises(NotificationAlreadyRunning):
                    with store.lock():
                        self.fail("second lock unexpectedly acquired")

    def test_lock_preserves_oserror_raised_by_caller(self):
        with TemporaryDirectory() as directory:
            store = NotificationStateStore(Path(directory) / "feishu_notifications")
            caller_error = OSError("caller failure")

            try:
                with store.lock():
                    raise caller_error
            except Exception as error:
                raised_error = error
            else:
                self.fail("caller error was not raised")

            self.assertIs(raised_error, caller_error)

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
