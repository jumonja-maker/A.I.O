"""Bounded telemetry priority without reordering, hidden loss or producer I/O."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from app_backend.blackbox_telemetry import FIELDS, TelemetryEmitter, from_environment


class TelemetryPriority(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aio-priority-', dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    @contextmanager
    def blocked_writer(self, *, capacity=32, **options):
        release = threading.Event()
        entered = threading.Event()
        original = TelemetryEmitter._writer
        def waiting(emitter):
            entered.set()
            release.wait(15)
            original(emitter)
        with patch.object(TelemetryEmitter, '_writer', waiting):
            emitter = TelemetryEmitter(self.root / 'events.jsonl', capacity=capacity, **options)
        self.assertTrue(entered.wait(2))
        try:
            yield emitter
        finally:
            release.set()
            emitter.close()
            self.assertFalse(emitter._writer_thread.is_alive())

    @staticmethod
    def read(emitter):
        return [json.loads(row) for row in emitter.path.read_text().splitlines()]

    def assert_complete_accounting(self, emitter, rows):
        self.assertEqual(sum(row['duration_ms'] for row in rows if row['type'] == 'telemetry_dropped'), emitter.dropped)
        admitted = [row for row in rows if row['type'] != 'telemetry_dropped']
        self.assertEqual(len(admitted) + emitter.dropped, emitter._next_seq)
        self.assertEqual([row['seq'] for row in rows], sorted(row['seq'] for row in rows))
        self.assertTrue(all(set(row) <= FIELDS for row in rows))

    def test_routine_flood_leaves_room_for_task_terminal_save_and_cancel(self):
        with self.blocked_writer(capacity=32) as emitter:
            for index in range(100):
                emitter.emit('task_queued', task_id=str(index), stage='queue')
            self.assertEqual(emitter._queue.qsize(), 28)
            self.assertEqual(emitter._critical_reserve, 4)
            emitter.emit('task_failed', task_id='real-task', reason_code='ENGINE_FAILED')
            emitter.emit('save_confirmed', save_status='saved')
            emitter.emit('cancel_requested', status='cancelled')
            self.assertEqual(emitter._queue.qsize(), 31)
        rows = self.read(emitter)
        self.assertTrue({'task_failed', 'save_confirmed', 'cancel_requested'} <= {r['type'] for r in rows})
        self.assert_complete_accounting(emitter, rows)

    def test_actual_guard_registry_event_survives_routine_flood(self):
        with self.blocked_writer(capacity=8) as emitter:
            for _ in range(20):
                emitter.emit('task_queued')
            emitter.emit('native_registry_identified', engine_registry_fingerprint='a' * 64)
        rows = self.read(emitter)
        [identity] = [row for row in rows if row['type'] == 'native_registry_identified']
        self.assertEqual(identity['engine_registry_fingerprint'], 'a' * 64)
        self.assert_complete_accounting(emitter, rows)

    def test_critical_flood_stays_bounded_and_declares_even_terminal_loss(self):
        with self.blocked_writer(capacity=8) as emitter:
            for _ in range(30):
                emitter.emit('task_failed', reason_code='ENGINE_FAILED')
            self.assertEqual(emitter._queue.qsize(), 8)
            self.assertEqual(emitter.dropped, 23)
        rows = self.read(emitter)
        self.assertEqual(len([row for row in rows if row['type'] == 'task_failed']), 7)
        self.assertEqual(rows[-1]['type'], 'telemetry_dropped')
        self.assert_complete_accounting(emitter, rows)

    def test_concurrent_routine_and_terminal_events_keep_one_fifo_sequence(self):
        with self.blocked_writer(capacity=64) as emitter:
            def producer(number):
                for index in range(200):
                    emitter.emit('task_succeeded' if index % 5 == 0 else 'native_phase_measured',
                                 task_id='%s:%s' % (number, index))
            threads = [threading.Thread(target=producer, args=(number,)) for number in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertLessEqual(emitter._queue.qsize(), 64)
        rows = self.read(emitter)
        self.assert_complete_accounting(emitter, rows)
        sequences = [row['seq'] for row in rows]
        self.assertEqual(len(sequences), len(set(sequences)))

    def test_capacity_one_remains_usable_and_does_not_reserve_its_only_slot(self):
        with self.blocked_writer(capacity=1) as emitter:
            self.assertEqual(emitter._critical_reserve, 0)
            self.assertEqual(emitter._routine_capacity, 1)
            emitter.emit('native_phase_measured')
        self.assert_complete_accounting(emitter, self.read(emitter))

    def test_configured_revision_is_captured_once_and_is_not_claimed_verified(self):
        revision = '1' * 40
        with self.blocked_writer(app_revision=revision) as emitter:
            with patch.dict(os.environ, {'AIO_BUILD_REVISION': '2' * 40}):
                emitter.emit('job_started', module_id='guard')
        rows = self.read(emitter)
        self.assertTrue(all(row.get('app_revision') == revision for row in rows))
        self.assertTrue(all(row.get('app_revision_source') == 'configured' for row in rows))
        self.assertTrue(all('engine_registry_fingerprint' not in row for row in rows))

    def test_invalid_identity_metadata_is_omitted_without_disabling_diagnostics(self):
        secret = 'C:/Private Person/secret.txt'
        with self.blocked_writer(app_revision=secret) as emitter:
            emitter.emit('job_started', engine_registry_fingerprint=secret)
        rows = self.read(emitter)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all('app_revision' not in row and 'engine_registry_fingerprint' not in row for row in rows))
        self.assertNotIn(secret, json.dumps(rows))

    def test_explicit_registry_fingerprint_is_a_validated_digest_only(self):
        digest = 'e' * 64
        with self.blocked_writer() as emitter:
            emitter.emit('job_started', engine_registry_fingerprint=digest)
        rows = self.read(emitter)
        self.assertEqual(rows[-1]['engine_registry_fingerprint'], digest)
        self.assertNotIn('engine_registry_fingerprint', rows[0])

    def test_environment_build_declaration_is_optional_and_fixed_at_startup(self):
        destination = self.root / 'configured.jsonl'
        with patch.dict(os.environ, {'AIO_BLACKBOX_EVENTS': str(destination), 'AIO_BUILD_REVISION': 'f' * 40}):
            emitter = from_environment()
        self.assertIsNotNone(emitter)
        emitter.emit('task_started')
        emitter.close()
        rows = self.read(emitter)
        self.assertTrue(all(row.get('app_revision') == 'f' * 40 for row in rows))
