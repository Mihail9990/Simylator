"""Runtime logs are opt-in safe records, bounded and independent of execution."""
import io
import json
import logging
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from tradfi.log_export import build_document
from tradfi.runtime_logging import RuntimeLog


class RuntimeLoggingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'runtime.log'
        self.logger = logging.getLogger('tradfi.engine')

    def test_events_exported_without_secrets_and_handler_removed(self):
        level, handlers = self.logger.level, list(self.logger.handlers)
        with RuntimeLog(self.path) as capture:
            self.logger.info('%s phase=%s', 'basket_start', 'WAIT_CANDLE')
            self.logger.error('Unknown gateway outcome %s: %s', 'secret-ID', 'secret-password')
            self.logger.error('unrecognized secret-token', exc_info=True)
        self.assertFalse(capture.worker.is_alive())
        self.assertEqual(self.logger.handlers, handlers)
        self.assertEqual(self.logger.level, level)
        rows = [json.loads(x) for x in self.path.read_text().splitlines()]
        self.assertEqual([x['event'] for x in rows], ['service_started', 'basket_start', 'unknown_gateway_outcome', 'service_stopped'])
        exported = build_document(self.path)
        self.assertIn(b'exported_records=4', exported)
        self.assertNotIn(b'secret-ID', exported)
        self.assertNotIn(b'secret-password', exported)
        self.assertNotIn(b'secret-token', exported)
        self.assertNotIn('secret', self.path.read_text())

    def test_v3_wait_phases_survive_capture_and_export(self):
        with RuntimeLog(self.path):
            self.logger.info('%s phase=%s', 'v3_entry_prepared', 'V3_WAIT_ENTRY')
            self.logger.info('%s phase=%s', 'v3_schedule_created', 'V3_WAIT_CANDLE')
        rows = [json.loads(x) for x in self.path.read_text().splitlines()]
        self.assertIn(('v3_entry_prepared', 'V3_WAIT_ENTRY'),
                      [(x['event'], x.get('phase')) for x in rows])
        self.assertIn(('v3_schedule_created', 'V3_WAIT_CANDLE'),
                      [(x['event'], x.get('phase')) for x in rows])
        exported = build_document(self.path)
        self.assertIn(b'V3_WAIT_ENTRY', exported)
        self.assertIn(b'V3_WAIT_CANDLE', exported)
        self.assertIn(b'exported_records=4', exported)

    def test_failure_event_has_no_exception_text(self):
        with self.assertRaisesRegex(ValueError, 'private'):
            with RuntimeLog(self.path):
                raise ValueError('private credential')
        self.assertIn('service_failed', self.path.read_text())
        self.assertNotIn('private', self.path.read_text())

    def test_queue_is_bounded_and_drops_without_blocking(self):
        capture = RuntimeLog(self.path, capacity=2)
        for _ in range(100):
            capture.event('service_started')
        self.assertEqual(capture.queue.qsize(), 2)
        self.assertEqual(capture.dropped, 98)
        self.assertFalse(self.path.exists())

    def test_bad_path_is_best_effort(self):
        self.path.mkdir()
        with patch('sys.stderr', new_callable=io.StringIO) as stderr:
            with RuntimeLog(self.path) as capture:
                capture.event('service_started')
            self.assertTrue(capture.failed)
            self.assertEqual(stderr.getvalue(), '')

    def test_rotation_is_bounded(self):
        with patch('tradfi.runtime_logging.ROTATION_BYTES', 350), patch('tradfi.runtime_logging.BACKUPS', 2):
            with RuntimeLog(self.path) as capture:
                for _ in range(30):
                    capture.event('service_started')
        self.assertLessEqual(len(list(self.path.parent.glob('runtime.log*'))), 3)
        self.assertTrue(Path(str(self.path) + '.1').exists())
        self.assertTrue(all(p.stat().st_size < 350 for p in self.path.parent.glob('runtime.log*')))

    def test_slow_disk_does_not_block_producer_or_shutdown(self):
        entered, release = threading.Event(), threading.Event()
        from tradfi.runtime_logging import QuietRotatingHandler
        original = QuietRotatingHandler.emit
        def slow(sink, record):
            entered.set()
            release.wait(5)
            original(sink, record)
        with patch.object(QuietRotatingHandler, 'emit', slow):
            capture = RuntimeLog(self.path, capacity=2)
            try:
                capture.__enter__()
                self.assertTrue(entered.wait(2))
                for _ in range(100):
                    capture.event('service_started')
                capture.__exit__(None, None, None)
                self.assertTrue(capture.worker.is_alive(), 'shutdown returns while sink is stalled')
                self.assertGreater(capture.dropped, 0)
                self.assertNotIn(capture, self.logger.handlers)
            finally:
                release.set()
                capture.worker.join(3)
        self.assertFalse(capture.worker.is_alive())

    def test_stalled_sink_does_not_block_interpreter_exit(self):
        script = '''
import sys
from threading import Event
from tradfi.runtime_logging import RuntimeLog, QuietRotatingHandler
entered = Event()
def stalled(self, record):
    entered.set()
    Event().wait()
QuietRotatingHandler.emit = stalled
with RuntimeLog(sys.argv[1]):
    assert entered.wait(2)
print('context exit finished', flush=True)
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.path)],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('context exit finished', result.stdout)

    def test_missing_and_empty_exports_are_explained(self):
        self.assertIn(b'No runtime log files are readable', build_document(self.path))
        self.path.touch()
        self.assertIn(b'No allowlisted runtime events', build_document(self.path))
