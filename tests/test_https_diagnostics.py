"""Offline regressions for the PAPER apply -> worker -> GUI diagnostic boundary."""
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

import https_common as h
import paper_https_setup as p
from test_paper_https_setup import build_setup, plan

SECRET = 'DO_NOT_DISCLOSE_password_config_command'


class Diagnostics(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.setup = build_setup(Path(self.temp.name))
        patcher = patch.object(h, 'validate_archive', return_value=b'validated-test-binary')
        patcher.start()
        self.addCleanup(patcher.stop)

    def worker(self, fn):
        job = h.BackgroundJob()
        self.assertTrue(job.start(fn))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = job.poll()
            if result is not None:
                self.assertFalse(result[0])
                return result[1]
            time.sleep(.001)
        self.fail('worker did not complete')

    def assert_safe(self, exc, code, stage):
        row = h.safe_failure(exc, 'config')  # The former GUI reclassification boundary.
        self.assertEqual((row['code'], row['stage']), (code, stage))
        self.assertNotIn(SECRET, str(exc) + h.failure_text(exc) + json.dumps(row))
        return row

    def test_original_double_wrap_reproduces_collapse(self):
        original = subprocess.TimeoutExpired(SECRET, 1, output=SECRET, stderr=SECRET)
        row = h.safe_failure(original, 'download')
        old_wrapper = h.HttpsError(row['reason'])
        self.assertEqual(h.safe_failure(old_wrapper, 'config')['code'], 'GUARD_REJECTED')
        fixed = h.FailureError(row)
        self.assert_safe(fixed, 'TIMEOUT', 'download')

    def test_download_timeout_survives_apply_worker_gui(self):
        with patch.object(self.setup.adapter, 'fetch', side_effect=subprocess.TimeoutExpired(SECRET, 1)):
            exc = self.worker(lambda: self.setup.apply(plan(self.setup), lambda _: True))
        self.assert_safe(exc, 'TIMEOUT', 'download')
        self.assertEqual(self.setup.diagnose()['failure']['code'], 'TIMEOUT')
        self.assertFalse(self.setup.home.exists())

    def test_command_exit_and_adapt_stage_survive(self):
        with patch.object(self.setup.adapter, 'run', side_effect=h.CommandError('caddy.exe', 23)):
            exc = self.worker(lambda: self.setup.apply(plan(self.setup), lambda _: True))
        row = self.assert_safe(exc, 'COMMAND_FAILED', 'adapt')
        self.assertEqual((row['command'], row['exit_code']), ('caddy.exe', 23))
        self.assertEqual(h.read(self.setup.journal)['failure'], row)
        reopened = p.Setup(self.setup.root, self.setup.adapter)
        self.assertEqual(reopened.diagnose()['failure'], row)
        self.assertIn('exit_code=23', h.failure_text(exc))

    def test_permissions_keep_acl_stage(self):
        with patch.object(self.setup.adapter, 'protect', side_effect=PermissionError(SECRET)):
            exc = self.worker(lambda: self.setup.apply(plan(self.setup), lambda _: True))
        self.assert_safe(exc, 'FILE_PERMISSION', 'acl')

    def test_preparation_root_password_conflict_and_ports(self):
        exc = self.worker(lambda: self.setup.prepare('8.8.8.8', password='short'))
        self.assert_safe(exc, 'PASSWORD_REQUIRED', 'prepare')
        self.setup.adapter.snapshot['services'] = [{'Name': SECRET}]
        exc = self.worker(lambda: plan(self.setup))
        self.assert_safe(exc, 'HTTPS_CONFLICT', 'prepare')
        self.setup.adapter.snapshot['services'] = []
        self.setup.adapter.snapshot['listeners'] = [{'port': 443}, {'port': 8766}]
        exc = self.worker(lambda: plan(self.setup))
        row = self.assert_safe(exc, 'PORT_BUSY', 'prepare')
        self.assertEqual(row['ports'], [443, 8766])
        self.assertIn('443, 8766', h.failure_text(exc))
        h.save(self.setup.root / 'application.json', {'role': 'live'})
        exc = self.worker(lambda: plan(self.setup))
        self.assert_safe(exc, 'ROOT_INVALID', 'root')

    def test_unknown_errors_and_untrusted_guard_text_are_redacted(self):
        for exc, code in [(RuntimeError(SECRET), 'UNKNOWN'), (h.HttpsError(SECRET), 'GUARD_REJECTED')]:
            with self.subTest(code=code):
                self.assert_safe(self.worker(lambda: (_ for _ in ()).throw(exc)), code, 'begin')
        # A copied hardcoded message is not proof of a trusted local guard.
        self.assertEqual(h.safe_failure(h.HttpsError(h.MESSAGES['PASSWORD_REQUIRED']), 'prepare')['code'], 'GUARD_REJECTED')
        forged = dict(code='TIMEOUT', stage=SECRET, reason=SECRET, password=SECRET, command=SECRET)
        self.assertNotIn(SECRET, json.dumps(h.sanitize_failure(forged)))

    def test_journal_save_failure_does_not_mask_unknown_command_outcome(self):
        self.setup.adapter.fail = 'create'
        save = h.save
        def save_or_fail(path, value):
            if path == self.setup.journal and 'failure' in value:
                raise PermissionError(SECRET)
            save(path, value)
        with patch.object(h, 'save', side_effect=save_or_fail):
            exc = self.worker(lambda: self.setup.apply(plan(self.setup), lambda _: True))
        row = self.assert_safe(exc, 'TIMEOUT', 'service_create')
        self.assertTrue(row['journal_save_failed'])
        self.assertIn('Не удалось сохранить', h.failure_text(exc))
        self.assertIsNotNone(h.read(self.setup.journal)['pending'])
        self.assertEqual(self.setup.diagnose()['failure'], row)
        for action in (lambda: plan(self.setup), lambda: self.setup.rollback(lambda _: True), self.setup.recover):
            with self.assertRaises(h.HttpsError):
                action()
        self.assertEqual(sum(c[0] == 'create' for c in self.setup.adapter.calls), 1)
        self.assertFalse(any(c[0] in ('start', 'delete', 'stop') for c in self.setup.adapter.calls))

    def test_maintenance_failure_before_apply_body_is_classified(self):
        import independent_install
        with patch.object(independent_install.IndependentManager, 'maintenance_guard',
                          side_effect=independent_install.legacy.SetupError(SECRET)):
            exc = self.worker(lambda: self.setup.apply(plan(self.setup), lambda _: True))
        self.assert_safe(exc, 'MAINTENANCE_REJECTED', 'maintenance')
        self.assertFalse(self.setup.adapter.calls)
        self.assertFalse(self.setup.home.exists())

    def test_hostile_structured_payload_is_rebuilt_and_bounded(self):
        payload = dict(code='COMMAND_FAILED', stage='adapt', reason=SECRET,
                       command=SECRET, exit_code=2 ** 200, password=SECRET)
        exc = h.FailureError(payload)
        exc.failure['reason'] = SECRET  # Even mutation after construction is ignored.
        row = self.assert_safe(exc, 'COMMAND_FAILED', 'adapt')
        self.assertEqual(row['command'], 'command')
        self.assertIsNone(row['exit_code'])
        self.assertNotIn('http_status', h.sanitize_failure(dict(code='HTTP_ERROR', http_status=999999)))

    def test_successful_apply_clears_prior_download_failure(self):
        with patch.object(self.setup.adapter, 'fetch', side_effect=TimeoutError(SECRET)):
            self.worker(lambda: self.setup.apply(plan(self.setup), lambda _: True))
        self.assertEqual(self.setup.diagnose()['failure']['code'], 'TIMEOUT')
        self.setup.apply(plan(self.setup), lambda _: True)
        result = self.setup.diagnose()
        self.assertEqual(result['status'], 'INSTALLED_UNVERIFIED')
        self.assertIsNone(result['failure'])

    def test_successful_verify_clears_prior_gui_verify_failure(self):
        self.setup.apply(plan(self.setup), lambda _: True)
        self.setup.adapter.snapshot['listeners'] = [
            dict(port=443, pid=5, address='0.0.0.0'),
            dict(port=8766, pid=9, address='127.0.0.1')]
        self.setup.adapter.fail = 'tls'
        try:
            self.setup.verify()
        except p.ssl.SSLCertVerificationError as exc:
            # The GUI records the failure without changing verify's public contract.
            self.setup.last_failure = h.safe_failure(exc, 'verify')
        self.assertEqual(self.setup.diagnose()['failure']['code'], 'TLS_VERIFY')
        self.setup.adapter.fail = None
        self.setup.verify()
        self.assertIsNone(self.setup.diagnose()['failure'])

    def test_successful_recovery_clears_failure_and_keeps_archived_journal(self):
        with patch.object(self.setup.adapter, 'run', side_effect=h.CommandError('caddy.exe', 23)):
            self.worker(lambda: self.setup.apply(plan(self.setup), lambda _: True))
        self.setup.recover()
        self.assertIsNone(self.setup.diagnose()['failure'])
        archived = list(self.setup.root.glob('paper-https-recovery-*/transaction.json'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(h.read(archived[0])['failure']['code'], 'COMMAND_FAILED')

    def test_successful_rollback_clears_display_but_keeps_historical_failure(self):
        self.setup.apply(plan(self.setup), lambda _: True)
        tx = h.read(self.setup.journal)
        tx['failure'] = h.safe_failure(TimeoutError(SECRET), 'adapt')
        h.save(self.setup.journal, tx)
        self.setup.last_failure = tx['failure']
        self.setup.rollback(lambda _: True)
        self.assertIsNone(self.setup.diagnose()['failure'])
        self.assertEqual(h.read(self.setup.journal)['failure']['code'], 'TIMEOUT')
        self.assertIsNone(p.Setup(self.setup.root, self.setup.adapter).diagnose()['failure'])

    def test_persisted_failure_reason_is_rebuilt_not_echoed(self):
        self.setup.adapter.fail = 'create'
        self.worker(lambda: self.setup.apply(plan(self.setup), lambda _: True))
        tx = h.read(self.setup.journal)
        tx['failure'].update(reason=SECRET, password=SECRET)
        h.save(self.setup.journal, tx)
        reopened = p.Setup(self.setup.root, self.setup.adapter)
        self.assertNotIn(SECRET, json.dumps(reopened.diagnose()['failure']))


if __name__ == '__main__':
    unittest.main()
