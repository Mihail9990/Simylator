"""Repository separation, local update preservation and no-real-execution contract."""
import ast
from contextlib import redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import independent_install as app
import install_tradfi as installer
from tradfi import managed
from tradfi.paper_runtime import _run_paper
from tradfi.runtime import main
from tradfi.web_control import WebTransport, panel_html
from tradfi.control import OwnerMailbox

ROOT=Path(__file__).resolve().parents[1]


def release_zip(sha):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob('*')):
            rel=path.relative_to(ROOT)
            if not path.is_file() or any(p in {'.git','__pycache__'} for p in rel.parts):continue
            archive.writestr('Simylator-'+sha+'/'+rel.as_posix(),path.read_bytes())
    return out.getvalue()


class InstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sha='a'*40;cls.sha2='b'*40
        cls.archive=release_zip(cls.sha);cls.archive2=release_zip(cls.sha2)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'AI_IMPULS_PAPER';self.urls=[];self.current=self.sha
        def fetch(url,limit=installer.MAX_DOWNLOAD):
            self.urls.append(url)
            self.assertIn('Mihail9990/Simylator',url)
            if '/git/ref/' in url:
                self.assertTrue(url.endswith('/heads/main'))
                return json.dumps(dict(ref='refs/heads/main',object=dict(type='commit',sha=self.current))).encode()
            self.assertTrue(url.endswith('/archive/'+self.current+'.zip'))
            return self.archive if self.current==self.sha else self.archive2
        self.manager=app.IndependentManager(self.root,'paper',fetch=fetch,report=lambda text:None)
        self.manager.prepare_python=lambda release:None

    def test_fresh_install_update_and_rollback_use_only_own_root(self):
        with patch('subprocess.Popen') as launch:
            self.manager.install()
            launch.assert_not_called()
        self.assertEqual(self.manager.pointer(),dict(current=self.sha,previous=None))
        config=self.manager.config();self.assertFalse(config['enable_trading']);self.assertEqual(config['web']['port'],8766)
        self.assertEqual(self.root/'data'/'state.sqlite3',Path(config['state_file']))
        self.assertNotIn('live_state_file',config)
        self.assertTrue((self.root/'START_PAPER.bat').is_file());self.assertFalse((self.root/'START_LIVE.bat').exists())
        self.current=self.sha2;self.manager.install()
        self.assertEqual(self.manager.pointer(),dict(current=self.sha2,previous=self.sha))
        self.manager.rollback()
        self.assertEqual(self.manager.pointer()['current'],self.sha)
        self.assertEqual(self.manager.config(),config)

    def test_existing_independent_paper_keeps_identity_password_database_and_https(self):
        from tradfi.paper import PaperSession
        from test_tradfi_paper import Reader
        self.root.mkdir();old='c'*40
        installer.atomic_json(self.root/'application.json',dict(format=1,role='paper'))
        installer.atomic_json(self.root/'managed-install.json',dict(format=2,role='paper',layout='independent_roots_v1'))
        installer.atomic_json(self.root/'current.json',dict(current=old,previous=None))
        cfg=dict(backend='paper_mt5_quotes_v1',enable_trading=False,state_file=str(self.root/'data/state.sqlite3'),log_file=str(self.root/'data/runtime.log'),web=dict(host='127.0.0.1',port=8766,enabled=True,password='existing-secret'*3,public_origin='https://paper.example.com'))
        installer.atomic_json(self.root/'data/service.json',cfg)
        reader=Reader();session=PaperSession(cfg['state_file'],reader,clock=lambda:reader.now,monotonic=lambda:reader.now)
        session.step();identity=session.exchange.account;session.close()
        binding={'path':cfg['state_file']};installer.atomic_json(self.root/'data/paper-state-binding.json',binding)
        https=self.root/'paper-https'/'keep.json';installer.atomic_json(https,{'sentinel':'existing-https'})
        code=self.root/'releases'/old/'code';code.mkdir(parents=True);(code/'retained.txt').write_text('old code')
        before=(self.root/'data/service.json').read_bytes()
        self.manager.install()
        self.assertEqual((self.root/'data/service.json').read_bytes(),before)
        self.assertEqual(installer.read_json(https),{'sentinel':'existing-https'})
        self.assertEqual(installer.read_json(self.root/'data/paper-state-binding.json'),binding)
        self.assertTrue((code/'retained.txt').is_file())
        session=PaperSession(cfg['state_file'],reader,clock=lambda:reader.now,monotonic=lambda:reader.now)
        self.assertEqual(session.exchange.account,identity);session.close()
        backups=list((self.root/'backups').glob('*/state.sqlite3'));self.assertEqual(len(backups),1)
        with sqlite3.connect(backups[0]) as db:
            self.assertEqual(json.loads(db.execute('SELECT payload FROM paper_exchange').fetchone()[0])['identity'],identity)
        # Mixed-repository old releases cannot be selected by a new PAPER-only rollback.
        with self.assertRaises(installer.SetupError):self.manager.rollback()
        self.assertEqual(self.manager.pointer()['current'],self.sha)

    def test_wrong_role_contract_and_archive_refused(self):
        with self.assertRaises(installer.SetupError):app.IndependentManager(self.root,'live')
        code=Path(self.tmp.name)/'code';code.mkdir()
        with self.assertRaises(installer.SetupError):installer.extract_archive(release_zip(self.sha),code,self.sha2)
        self.manager.install();code=self.manager.current_release()/'code'
        installer.atomic_json(code/'independent-install-contract.json',dict(format=1,roles=['live','paper'],layout='independent_roots_v1'))
        with self.assertRaises(installer.SetupError):app.validate_code(code)

    def test_active_process_lock_blocks_update_and_external_state_is_refused(self):
        self.manager.install();before=self.manager.pointer()
        with installer.lock(self.root/'instance-paper.lock'):
            with self.assertRaises(installer.SetupError):self.manager.install()
        self.assertEqual(self.manager.pointer(),before)
        cfg=self.manager.config();cfg['state_file']=str(Path(self.tmp.name)/'foreign.db')
        installer.atomic_json(self.manager.config_path,cfg)
        with self.assertRaises(installer.SetupError):self.manager.config()

    def test_launch_cannot_enable_execution_and_ignores_live_password(self):
        self.manager.install();cfg=self.manager.config();cfg['web']['password']='own-pass';installer.atomic_json(self.manager.config_path,cfg)
        with patch('subprocess.Popen') as process,patch.dict(os.environ,{'TRADFI_WEB_PASSWORD':'foreign'}):
            with self.assertRaises(installer.SetupError):self.manager.launch(True)
            process.assert_not_called()
            self.manager.launch()
        args,kwargs=process.call_args
        self.assertIn('paper',args[0]);self.assertNotIn('--execute',args[0]);self.assertNotIn('mt5',args[0])
        self.assertNotIn('TRADFI_WEB_PASSWORD',kwargs['env'])
        self.assertEqual(kwargs['env']['TRADFI_LAUNCH_INSTANCE'],'paper')


class SafetyTests(unittest.TestCase):
    def test_no_live_execution_modules_or_entrypoints(self):
        for file in ('START_LIVE.bat','tradfi/mt5_adapter.py','tradfi/evidence_broker.py','tradfi/gateway.py','mt5/TradFiGateway.mq5'):
            self.assertFalse((ROOT/file).exists(),file)
        for path in [*ROOT.glob('*.py'),*(ROOT/'tradfi').rglob('*.py')]:
            tree=ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.Attribute):self.assertNotIn(node.attr,{'order_send','order_check'},str(path))
        self.assertFalse(hasattr(__import__('tradfi.runtime',fromlist=['_run_mt5']),'_run_mt5'))

    def test_live_cli_and_execute_are_rejected_before_paper_runtime(self):
        for args in (['--mode','mt5'],['--mode','gateway'],['--mode','paper','--execute'],['--execute']):
            with patch('tradfi.paper_runtime.main') as run,redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):main(args)
                run.assert_not_called()

    def test_execution_config_and_auth_fail_before_native_initialization(self):
        cfg=dict(backend='paper_mt5_quotes_v1',terminal_path='terminal.exe',expected_login=123,expected_server='Bybit-Live-6',symbol='XAUUSD.s',enable_trading=True)
        api=Mock()
        with self.assertRaises(ValueError):_run_paper(cfg,api=api)
        api.initialize.assert_not_called()
        cfg['enable_trading']=False;cfg['web']={'password':'short','public_origin':'https://paper.example.com'}
        from tradfi.web_auth import WebAuthError
        with self.assertRaises(WebAuthError):_run_paper(cfg,api=api)
        api.initialize.assert_not_called()

    def test_http_route_and_html_have_only_paper_role(self):
        with self.assertRaises(ValueError):WebTransport(OwnerMailbox(),role='live')
        with self.assertRaises(ValueError):panel_html(b'<body>','live')
        body=panel_html((ROOT/'web/basket-panel/index.html').read_bytes(),'paper')
        self.assertIn(b'data-role="paper"',body);self.assertNotIn(b'<option value="live">',body)
        box=OwnerMailbox();box._view=dict(scope='PAPER-fixture',revision=0)
        transport=WebTransport(box,password='local-password')
        h={'Host':'127.0.0.1:8766','X-Local-Control':'1','Content-Type':'application/json'}
        code,_,headers=transport.request('POST','/api/login',h,json.dumps({'password':'local-password'}).encode())
        self.assertEqual(code,200);h['Cookie']=headers['Set-Cookie'].split(';')[0]
        self.assertEqual(transport.request('GET','/api/state',h)[0],404)
        self.assertEqual(transport.request('GET','/api/paper/state',h)[0],200)

    def test_managed_root_never_accepts_live_or_shared_installation(self):
        with self.assertRaises(ValueError):
            with managed.runtime_guard('/tmp/never-created.db',instance='live'):pass
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);code=root/'releases'/('a'*40)/'code/tradfi/managed.py';code.parent.mkdir(parents=True)
            installer.atomic_json(root/'managed-install.json',{'format':1})
            installer.atomic_json(root/'current.json',dict(current='a'*40,previous=None))
            with patch.object(managed,'__file__',str(code)):
                with self.assertRaises(installer.SetupError):
                    with managed.runtime_guard(root/'data/state.sqlite3'):pass


if __name__=='__main__':unittest.main()
