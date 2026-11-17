"""No real processes are launched or killed: ownership and handle races offline."""
from copy import deepcopy
import ctypes
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import install_tradfi as m
import independent_install as app

ROOT=r'C:\Apps\PAPER'
SHA='a'*40
EXE=ROOT+'\\releases\\'+SHA+r'\venv\Scripts\python.exe'
BASE=r'C:\Python313\python.exe'


def main(pid=10):
    return dict(pid=pid,parent=1,created=123456789,exe=BASE,argv=[EXE,ROOT+'\\releases\\'+SHA+r'\code\tradfi_main.py','--mode','paper','--service','--config',ROOT+r'\data\service.json'])


def worker(pid=11,exe=EXE):
    return dict(pid=pid,parent=10,created=123456799,exe=BASE,argv=[exe,'-c','from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=10, pipe_handle=42)','--multiprocessing-fork'])


class OwnershipTests(unittest.TestCase):
    def test_exact_service_both_redirector_and_base(self):
        p=main();self.assertTrue(m.owned_paper_process(ROOT,p))
        p['exe']=EXE;self.assertTrue(m.owned_paper_process(ROOT,p))
        for mutate in [lambda p:p['argv'].__setitem__(6,r'C:\Other\data\service.json'),lambda p:p['argv'].__setitem__(1,r'C:\Other\tradfi_main.py'),lambda p:p['argv'].append('--execute'),lambda p:p.update(exe=r'C:\MT5\terminal64.exe'),lambda p:p.update(created=0),lambda p:p.update(pid=True),lambda p:p['argv'].__setitem__(0,BASE)]:
            p=main();mutate(p);self.assertFalse(m.owned_paper_process(ROOT,p),p)

    def test_orphan_worker_own_venv_without_parent(self):
        p=worker();p['parent']=999
        self.assertEqual(m.owned_processes(ROOT,[p]),[p])
        p['argv'][0]='C:\\Other\\releases\\'+SHA+r'\venv\Scripts\python.exe'
        self.assertEqual(m.owned_processes(ROOT,[p]),[])

    def test_base_worker_requires_live_parent_or_exact_durable_identity(self):
        p=worker(exe=BASE)
        self.assertEqual(m.owned_processes(ROOT,[p]),[])
        self.assertEqual({v['pid'] for v in m.owned_processes(ROOT,[main(),p])},{10,11})
        record=dict(p,root=ROOT,kind='quote_worker')
        self.assertEqual(m.owned_processes(ROOT,[p],[record]),[p])
        for key,value in [('created',1),('exe',r'C:\Other\python.exe'),('root',r'C:\Other'),('kind','service')]:
            r=dict(record);r[key]=value
            self.assertEqual(m.owned_processes(ROOT,[p],[r]),[])

    def test_no_arbitrary_descendants_mt5_or_modified_code(self):
        for mutate in [lambda p:p['argv'].__setitem__(2,'import os; os.system("anything")'),lambda p:p.update(exe=r'C:\MT5\terminal64.exe'),lambda p:p['argv'].__setitem__(1,'-m'),lambda p:p['argv'].insert(1,'-Xbad')]:
            p=worker(exe=BASE);mutate(p)
            self.assertEqual(m.owned_processes(ROOT,[main(),p]),[main()])

    def test_parent_reuse_older_child_is_not_owned(self):
        p=worker(exe=BASE);p['created']=1
        self.assertEqual(m.owned_processes(ROOT,[main(),p]),[main()])

    def test_held_handle_identity_mismatch_never_terminates(self):
        for created,exe,expect in [(123456789,BASE,True),(123456788,BASE,False),(123456789,r'C:\Other\python.exe',False)]:
            api=Mock();api.OpenProcess.return_value=99
            def times(handle,creation,*unused):
                creation._obj.dwLowDateTime=created;creation._obj.dwHighDateTime=0;return True
            def image(handle,flags,buffer,size):buffer.value=exe;return True
            api.GetProcessTimes.side_effect=times;api.QueryFullProcessImageNameW.side_effect=image
            api.WaitForSingleObject.side_effect=[258,0];api.TerminateProcess.return_value=True
            with patch.object(m.os,'name','nt'),patch.object(ctypes,'WinDLL',return_value=api,create=True):
                self.assertEqual(m.terminate_verified_process(main()),expect)
            self.assertEqual(api.TerminateProcess.called,expect);api.CloseHandle.assert_called_once_with(99)

    def test_stop_registered_orphan_and_sessions_without_touching_foreign(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m.atomic_json(root/'managed-install.json',dict(format=2,role='paper',layout='sl_overlap_roots_v1'));m.atomic_json(root/'application.json',dict(format=1,role='paper'))
            own=worker(exe=BASE);foreign=worker(12,BASE);foreign['created']=777
            record=dict(own,format=1,root=str(root),kind='quote_worker',session='b'*32)
            m.atomic_json(root/'data/processes/owner.json',record)
            current=[own,foreign];terminated=[]
            def terminate(p):terminated.append(p);current.remove(p);return True
            sleeps=[]
            result=m.stop_owned_paper_processes(root,enumerate_processes=lambda:list(current),terminate=terminate,sleep=sleeps.append)
            self.assertEqual(terminated,[own]);self.assertEqual(current,[foreign]);self.assertEqual(sleeps,[3])
            self.assertTrue((root/'data'/('stop-paper-'+'b'*32+'.request')).exists());self.assertEqual(result['forced'],1)

    def test_no_false_success_when_owned_process_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m.atomic_json(root/'managed-install.json',dict(format=2,role='paper',layout='sl_overlap_roots_v1'));m.atomic_json(root/'application.json',dict(format=1,role='paper'))
            own=worker(exe=BASE);m.atomic_json(root/'data/processes/owner.json',dict(own,format=1,root=str(root),kind='quote_worker'))
            with self.assertRaises(m.SetupError):m.stop_owned_paper_processes(root,enumerate_processes=lambda:[own],terminate=lambda _:False,sleep=lambda _:None)

    def test_cim_precision_keeps_native_timestamp_and_rejects_reused_pid(self):
        row=dict(created='123456780',exe=BASE)
        self.assertTrue(m.same_cim_process(row,dict(created=123456789,exe=BASE)))
        self.assertFalse(m.same_cim_process(row,dict(created=123456790,exe=BASE)))
        self.assertFalse(m.same_cim_process(row,dict(created=123456789,exe=EXE)))
        self.assertFalse(m.same_cim_process(row,None))

    def test_discovery_skips_disappeared_unrelated_and_keeps_native_precision(self):
        shell=Mock();kernel=Mock();argv=main()['argv']
        def parse(cmd,count):count._obj.value=len(argv);return argv
        shell.CommandLineToArgvW.side_effect=parse
        rows=[dict(pid=99,parent=1,exe=BASE,cmd='gone',created='123456780'),dict(pid=10,parent=1,exe=BASE,cmd='owned',created='123456780')]
        native=dict(pid=10,exe=BASE,created=123456789)
        path=Path('/fake/windows')
        with patch.object(m.os,'name','nt'),patch.object(m,'Path',return_value=path),patch.object(ctypes,'WinDLL',side_effect=[shell,kernel],create=True),patch.object(m.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(rows))),patch.object(m,'process_identity',side_effect=[m.SetupError('gone'),native]):
            result=m.windows_processes()
        self.assertEqual(result,[dict(native,parent=1,argv=argv)])

    def test_capture_registration_failure_does_not_leak_or_abort_capture(self):
        from test_paper_quote_capture import CONFIG, synthetic_capture
        from tradfi.paper_quote_capture import ProcessQuoteReader
        with tempfile.TemporaryDirectory() as tmp,patch('tradfi.managed.managed_root',return_value=Path(tmp)),patch.object(m,'register_process_ownership',side_effect=m.SetupError('unavailable')) as register:
            reader=ProcessQuoteReader(CONFIG,worker=synthetic_capture)
            try:register.assert_called_once_with(Path(tmp),reader._process.pid,'quote_worker')
            finally:reader.close()
            self.assertFalse(reader._process.is_alive())

    def test_locked_runtime_prevents_false_stop_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m.atomic_json(root/'managed-install.json',dict(format=2,role='paper',layout='sl_overlap_roots_v1'));m.atomic_json(root/'application.json',dict(format=1,role='paper'))
            manager=app.IndependentManager(root,'paper',report=lambda _:None)
            with patch.object(m,'stop_owned_paper_processes',return_value={'remaining':[]}),m.lock(root/'instance-paper.lock'):
                with self.assertRaises(m.SetupError):manager.stop_processes()

    def test_stop_available_without_current_gui_child_and_uses_own_root_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);m.atomic_json(root/'managed-install.json',dict(format=2,role='paper',layout='sl_overlap_roots_v1'));m.atomic_json(root/'application.json',dict(format=1,role='paper'))
            manager=app.IndependentManager(root,'paper',report=lambda _:None)
            with patch.object(m,'stop_owned_paper_processes',return_value={'remaining':[]}) as stop:
                manager.stop_processes();stop.assert_called_once_with(root,sessions=[])
            source=Path(app.__file__).read_text()
            self.assertIn("stop_button.configure(state='disabled' if busy else 'normal')",source)
            self.assertIn('work(lambda:manager.stop_processes(session),allow_running=True)',source)

if __name__=='__main__':unittest.main()
