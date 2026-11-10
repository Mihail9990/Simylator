"""Updater regression: real PAPER history is not a LIVE deal ledger."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import independent_install as app
import install_tradfi as installer
import test_standalone_package as package
import test_tradfi_paper as paper_tests


class PaperUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old = 'a' * 40
        cls.new = 'b' * 40
        cls.archives = {sha: package.release_zip(sha) for sha in (cls.old, cls.new)}

    def setUp(self):
        self.fixture = paper_tests.PaperTest()
        self.fixture.primary_v3 = True
        self.fixture.setUp()
        self.closed = False
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.close_fixture)
        self.path = self.fixture.path
        self.root = self.path.parent / 'installed'
        self.current = self.old
        self.fetches = []
        def fetch(url, limit=installer.MAX_DOWNLOAD):
            self.fetches.append(url)
            if '/git/ref/' in url:
                return json.dumps(dict(ref='refs/heads/main', object=dict(type='commit', sha=self.current))).encode()
            return self.archives[self.current]
        self.manager = app.IndependentManager(self.root, 'paper', fetch=fetch, report=lambda _: None)
        self.manager.prepare_python = lambda _: None

    def close_fixture(self):
        if not self.closed:
            self.fixture.session.close()
            self.closed = True

    def loss(self):
        self.fixture.initial()
        self.fixture.reader.tick('3998', '3998.1')
        self.fixture.step(4)
        self.assertTrue(self.fixture.session.controller.state['seen_deals'])
        self.assertGreater(float(self.fixture.session.controller.state['cycle']['recovery']), 0)
        self.close_fixture()

    def inspect(self):
        installer.inspect_state(self.path.parent, {'state_file': str(self.path)})

    def values(self):
        with closing(sqlite3.connect(self.path)) as db:
            return tuple(json.loads(db.execute('SELECT payload FROM ' + table + ' WHERE id=1').fetchone()[0])
                         for table in ('snapshot', 'paper_exchange'))

    def write_values(self, state, paper):
        with closing(sqlite3.connect(self.path)) as db, db:
            for table, value in (('snapshot', state), ('paper_exchange', paper)):
                db.execute('UPDATE ' + table + ' SET payload=? WHERE id=1', (json.dumps(value),))

    def installed(self):
        self.manager.install()
        destination = Path(self.manager.config()['state_file'])
        app.copy_database(self.path, destination)
        self.path = destination
        installer.atomic_json(installer.binding_path(self.root), {'path': str(destination)})
        config = self.manager.config()
        config['web'].update(password='retained-password', public_origin='https://paper.example.test')
        installer.atomic_json(self.manager.config_path, config)
        installer.atomic_json(self.root / 'paper-https' / 'sentinel.json', {'retained': True})
        self.fetches.clear()
        self.current = self.new

    def assert_read_only(self):
        before = self.path.read_bytes()
        self.inspect()
        self.assertEqual(self.path.read_bytes(), before)

    def test_closed_history_and_debt_are_valid_without_live_ledger(self):
        self.loss()
        state, paper = self.values()
        self.assertNotIn('ledger_contract', state)
        self.assert_read_only()
        with closing(sqlite3.connect(self.path)) as db:
            self.assertNotIn('consumed_deals', {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})

    def test_open_positions_orders_and_paused_gap_are_retained(self):
        self.fixture.initial()
        c = self.fixture.session.controller.state['cycle']
        self.fixture.action(dict(type='v3_link', cycle_id=c['id'], expected_positions=sorted(p['id'] for p in self.fixture.p['positions'])))
        self.fixture.step(3)
        self.assertTrue(self.fixture.p['positions']); self.assertTrue(self.fixture.p['orders'])
        self.fixture.p['paused_gap'] = True
        self.fixture.session.exchange.persist()
        self.close_fixture()
        self.assert_read_only()
        self.installed()
        before = self.values()
        self.manager.install()
        self.assertEqual(self.values(), before)
        self.assertTrue(self.values()[1]['paused_gap'])

    def test_unconsumed_exchange_exit_after_interrupted_tick_is_valid(self):
        self.fixture.initial()
        self.fixture.reader.tick('3998', '3998.1')
        self.fixture.session.exchange.observe(self.fixture.reader.read())
        self.close_fixture()
        state, paper = self.values()
        self.assertFalse(state['seen_deals'])
        self.assertTrue(any(d['entry'] == 'OUT' for d in paper['deals']))
        self.assert_read_only()

    def test_unknown_command_without_durable_result_blocks(self):
        self.loss()
        state, paper = self.values()
        cid = next(iter(state['commands']))
        state['commands'][cid]['status'] = 'UNKNOWN'
        paper['results'].pop(cid)
        self.write_values(state, paper)
        before = self.path.read_bytes()
        with self.assertRaises(installer.SetupError): self.inspect()
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_command_with_durable_result_is_preserved_not_replayed(self):
        self.fixture.initial(); self.close_fixture()
        state, paper = self.values(); cid = next(iter(paper['results']))
        state['commands'][cid]['status'] = 'UNKNOWN'
        self.write_values(state, paper)
        self.assert_read_only()
        self.assertEqual(self.values()[0]['commands'][cid]['status'], 'UNKNOWN')

    def test_malformed_and_incomplete_histories_fail_closed(self):
        self.loss()
        original = self.values()
        mutations = {
            'schema': lambda s, p: s.update(schema=2),
            'boolean_schema': lambda s, p: s.update(schema=True),
            'backend': lambda s, p: s.update(backend='mt5_direct_v1'),
            'future_backend': lambda s, p: s.update(backend='paper_mt5_quotes_v2'),
            'future_ledger': lambda s, p: s.update(ledger_contract='future'),
            'live_ledger': lambda s, p: s.update(ledger_contract='deal_ledger_v1'),
            'wrong_account': lambda s, p: s.update(account='PAPER-other'),
            'missing_account': lambda s, p: s.pop('account'),
            'currency': lambda s, p: s.update(currency='USD'),
            'unknown_phase': lambda s, p: s.update(phase='FUTURE'),
            'unknown_strategy': lambda s, p: s.update(recovery_mode='V4'),
            'blocked_migration': lambda s, p: s.update(migration_block='requires reconciliation'),
            'nonfinite_recovery': lambda s, p: s['cycle'].update(recovery='NaN'),
            'negative_carry': lambda s, p: s.update(basket_carry='-900'),
            'unknown_receipt': lambda s, p: s.update(control_receipts={'request': {'status': 'UNKNOWN'}}),
            'unknown_cycle': lambda s, p: s['cycle'].update(recovery_mode='V4'),
            'wrong_entry_side': lambda s, p: p['deals'][0].update(side='SELL'),
            'wrong_order_size': lambda s, p: p['order_history'][0].update(size='.02'),
            'missing_accepted_order': lambda s, p: next(iter(p['results'].values())).update(order_id='missing'),
            'missing_commands': lambda s, p: s.pop('commands'),
            'unknown_command': lambda s, p: next(iter(s['commands'].values())).update(status='FUTURE'),
            'seen_string': lambda s, p: s.update(seen_deals='out'),
            'seen_duplicate': lambda s, p: s['seen_deals'].append(s['seen_deals'][0]),
            'seen_missing_exit': lambda s, p: s['seen_deals'].append('missing'),
            'seen_entry': lambda s, p: s['seen_deals'].append(p['deals'][0]['id']),
            'missing_identity': lambda s, p: p.pop('identity'),
            'nonboolean_pause': lambda s, p: p.update(paused_gap='false'),
            'missing_results': lambda s, p: p.pop('results'),
            'unknown_result': lambda s, p: next(iter(p['results'].values())).update(status='UNKNOWN'),
            'missing_deals': lambda s, p: p.pop('deals'),
            'missing_entry': lambda s, p: p['deals'].pop(0),
            'duplicate_exit': lambda s, p: p['deals'].append(deepcopy(p['deals'][-1])),
            'missing_order_history': lambda s, p: p.update(order_history=[]),
            'malformed_deal': lambda s, p: p['deals'].append({}),
            'nonfinite_cost': lambda s, p: p.update(commission_per_lot_side='NaN'),
            'missing_cursor': lambda s, p: p.pop('cursor'),
            'partial_close': lambda s, p: p['deals'][-1].update(volume='0.001'),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                state, paper = deepcopy(original); mutate(state, paper); self.write_values(state, paper)
                before = self.path.read_bytes()
                with self.assertRaises(installer.SetupError): self.inspect()
                self.assertEqual(self.path.read_bytes(), before)

    def test_coherent_but_fabricated_open_fill_does_not_override_command(self):
        self.fixture.initial(); self.close_fixture()
        original = self.values()
        for change in ('size', 'side'):
            state, paper = deepcopy(original)
            if change == 'size':
                paper['deals'][0]['volume'] = '.02'; paper['positions'][0]['size'] = '.02'
            else:
                paper['deals'][0]['side'] = 'SELL'; paper['positions'][0]['side'] = 'SELL'
            self.write_values(state, paper)
            with self.assertRaises(installer.SetupError): self.inspect()

    def test_database_corruption_missing_tables_rows_and_ambiguous_json_block(self):
        self.loss(); original = self.path.read_bytes()
        for payload in ('[]', '{', '{"schema":1,"schema":1}', '{"schema":NaN}'):
            with self.subTest(payload=payload):
                self.path.write_bytes(original)
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('UPDATE snapshot SET payload=?', (payload,))
                with self.assertRaises(installer.SetupError): self.inspect()
        for statement in ('DELETE FROM snapshot', 'DROP TABLE snapshot', 'DELETE FROM paper_exchange', 'DROP TABLE paper_exchange'):
            with self.subTest(statement=statement):
                self.path.write_bytes(original)
                with closing(sqlite3.connect(self.path)) as db, db: db.execute(statement)
                with self.assertRaises(installer.SetupError): self.inspect()
        self.path.write_bytes(b'not a sqlite database')
        with self.assertRaises(installer.SetupError): self.inspect()

    def test_unexpected_live_ledger_rows_block_even_without_contract(self):
        self.loss()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE consumed_deals(backend TEXT, account TEXT, ticket TEXT)')
        self.assert_read_only()  # Empty legacy table does not invent a ledger.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('INSERT INTO consumed_deals VALUES(?,?,?)', ('mt5_direct_v1', 'LIVE', '1'))
        with self.assertRaises(installer.SetupError): self.inspect()

    def test_update_backup_repeat_and_rollback_preserve_data_and_settings(self):
        self.loss(); self.installed()
        data = self.path.read_bytes(); values = self.values()
        protected = [self.manager.config_path, installer.binding_path(self.root), self.root / 'paper-https' / 'sentinel.json']
        before = {p: p.read_bytes() for p in protected}
        with patch('subprocess.Popen') as launch:
            self.manager.install(); launch.assert_not_called()
        self.assertEqual(self.manager.pointer(), dict(current=self.new, previous=self.old))
        self.assertEqual(self.path.read_bytes(), data)
        self.assertEqual({p: p.read_bytes() for p in protected}, before)
        backups = list((self.root / 'backups').glob('*/state.sqlite3'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(json.loads(db.execute('SELECT payload FROM snapshot').fetchone()[0]), values[0])
            self.assertEqual(json.loads(db.execute('SELECT payload FROM paper_exchange').fetchone()[0]), values[1])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        self.manager.install()  # Same SHA must not flip previous or rewrite data.
        self.assertEqual(self.manager.pointer(), dict(current=self.new, previous=self.old))
        self.manager.rollback()
        self.assertEqual(self.manager.pointer(), dict(current=self.old, previous=self.new))
        self.assertEqual(self.path.read_bytes(), data)
        self.assertEqual({p: p.read_bytes() for p in protected}, before)

    def test_wal_backup_includes_committed_wal_without_modifying_source(self):
        self.loss(); self.installed()
        with closing(sqlite3.connect(self.path, isolation_level=None)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('CREATE TABLE wal_sentinel(value TEXT)')
            db.execute("INSERT INTO wal_sentinel VALUES('committed-only-in-wal')")
            wal = Path(str(self.path) + '-wal')
            before = (self.path.read_bytes(), wal.read_bytes())
            self.manager.install()
            self.assertEqual((self.path.read_bytes(), wal.read_bytes()), before)
            backup = next((self.root / 'backups').glob('*/state.sqlite3'))
            with closing(sqlite3.connect(backup)) as saved:
                self.assertEqual(saved.execute('SELECT value FROM wal_sentinel').fetchone()[0], 'committed-only-in-wal')
                self.assertEqual(saved.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertFalse(Path(str(backup) + '-wal').exists())

    def test_atomic_pointer_replace_failure_keeps_original_and_retry_works(self):
        self.loss(); self.installed()
        pointer = self.manager.pointer(); data = self.path.read_bytes()
        real_replace = installer.os.replace
        def fail_pointer(src, dst):
            if Path(dst) == self.root / 'current.json': raise OSError('atomic switch interrupted')
            return real_replace(src, dst)
        with patch.object(installer.os, 'replace', side_effect=fail_pointer), self.assertRaises(OSError):
            self.manager.install()
        self.assertEqual(self.manager.pointer(), pointer); self.assertEqual(self.path.read_bytes(), data)
        self.assertFalse(list(self.root.glob('current.json.*.tmp')))
        self.manager.install()
        self.assertEqual(self.manager.pointer()['current'], self.new)

    def test_failed_backup_download_dependency_and_pointer_switch_keep_current(self):
        self.loss(); self.installed()
        original = self.manager.pointer(); data = self.path.read_bytes()
        actions = [
            patch.object(self.manager, 'backup', side_effect=installer.SetupError('backup failed')),
            patch.object(self.manager, 'fetch', side_effect=OSError('download failed')),
            patch.object(self.manager, 'prepare_python', side_effect=installer.SetupError('dependencies failed')),
        ]
        real_atomic = installer.atomic_json
        def fail_pointer(path, value):
            if Path(path) == self.root / 'current.json': raise OSError('pointer failure')
            return real_atomic(path, value)
        actions.append(patch.object(installer, 'atomic_json', side_effect=fail_pointer))
        for action in actions:
            with action, self.assertRaises((installer.SetupError, OSError)): self.manager.install()
            self.assertEqual(self.manager.pointer(), original)
            self.assertEqual(self.path.read_bytes(), data)
        self.manager.install()
        self.assertEqual(self.manager.pointer()['current'], self.new)

    def test_bad_state_blocks_before_download_and_backup(self):
        self.loss(); self.installed()
        state, paper = self.values(); state['seen_deals'].append('missing'); self.write_values(state, paper)
        pointer = self.manager.pointer(); data = self.path.read_bytes()
        with patch.object(self.manager, 'backup') as backup:
            with self.assertRaises(installer.SetupError): self.manager.install()
            backup.assert_not_called()
        self.assertFalse(self.fetches)
        self.assertEqual(self.manager.pointer(), pointer); self.assertEqual(self.path.read_bytes(), data)

    def test_process_and_database_locks_block_before_download(self):
        self.loss(); self.installed()
        for path in (self.root / 'instance-paper.lock', Path(str(self.path) + '.lock')):
            with installer.lock(path), self.assertRaises(installer.SetupError): self.manager.install()
            self.assertFalse(self.fetches)

    def test_incompatible_release_and_incomplete_ready_marker_do_not_switch(self):
        self.loss(); self.installed()
        release = self.manager.version(self.new)
        installer.extract_archive(self.archives[self.new], release / 'code', self.new)
        pointer = self.manager.pointer(); data = self.path.read_bytes()
        with self.assertRaises((installer.SetupError, FileNotFoundError)): self.manager.install()
        self.assertEqual(self.manager.pointer(), pointer)
        installer.atomic_json(release / 'ready.json', {'sha': self.new, 'contract': installer.CONTRACT})
        contract = dict(installer.CONTRACT, state_schema=2)
        installer.atomic_json(release / 'code' / 'managed_release.json', contract)
        with self.assertRaises(installer.SetupError): self.manager.install()
        self.assertEqual(self.manager.pointer(), pointer); self.assertEqual(self.path.read_bytes(), data)

    def test_external_zip_entrypoints_load_new_installer_not_old_release(self):
        for name in ('START_PAPER.bat', 'START_INSTALLER.bat'):
            text = (package.ROOT / name).read_text()
            self.assertIn('"%~dp0independent_install.py"', text)
            self.assertNotIn('launcher.py', text)
        self.assertEqual(installer.REPO, 'Mihail9990/Simylator')
        self.assertEqual(installer.CONTRACT, json.loads((package.ROOT / 'managed_release.json').read_text()))


if __name__ == '__main__': unittest.main()
