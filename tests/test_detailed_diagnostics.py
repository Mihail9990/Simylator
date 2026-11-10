import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tradfi.detailed_diagnostics import DetailedDiagnostics, sanitize
from tradfi.web_logs import DiagnosticLogs, LIMIT
from tradfi.storage import Store
from tradfi.engine import Controller
from tradfi.simulator import Simulator
from tradfi.control import ActionGateway, ActionRejected, Conflict


class DetailedTests(unittest.TestCase):
    def test_allowlist_and_bound(self):
        trace = DetailedDiagnostics(lambda: 123)
        for i in range(250):
            trace.record('owner_step', status='OK', duration_ms=2, password='secret', error_type='SECRET_TOKEN')
        rows = trace.drain()
        self.assertEqual(len(rows), 200)
        self.assertEqual(rows[-1]['dropped_records'], 50)
        self.assertNotIn('secret', json.dumps(rows).lower())
        self.assertEqual(trace.drain(), [])
        self.assertEqual(sanitize({'event':'SECRET'}), {})
        self.assertNotIn('duration_ms', sanitize({'event':'owner_step','duration_ms':float('nan')}))

    def test_malformed_and_uuid_are_safe(self):
        trace=DetailedDiagnostics(lambda:123)
        self.assertNotIn('duration_ms',sanitize({'event':'owner_step','duration_ms':10**1000}))
        from tradfi.context_diagnostics import project
        self.assertNotIn('SECRET_',json.dumps(project({'feed':{'reason':'SECRET_PASSWORD_ABC','source':'SECRET_TOKEN_XYZ'},'file':'SECRET_FILE'})))
        for kind in ([],{},None):
            trace.action('action_reserved', {'action':{'type':kind}})
        for event in ([],{},None):
            self.assertEqual(sanitize({'event':event}),{})
        self.assertTrue(project({'id':'\ud800'})['id'].startswith('hash-'))
        trace.action('action_reserved', {'id':'12345678-1234-1234-1234-123456789abc','action':{'type':'command','text':'/start'}})
        self.assertEqual(trace.drain()[-1]['receipt_id'],'12345678-1234-1234-1234-123456789abc')

    def test_guard_evidence_is_strictly_allowlisted(self):
        row=sanitize(dict(event='action_completed', error_code='QUEUE_EXPIRED',
                          guard_stage='execution_queue', queue_age_seconds=3.1,
                          snapshot_age_seconds=2.2, quote_age_seconds=.2, reservation_ms=12,
                          password='secret', raw_exception='secret'))
        self.assertEqual(row['guard_stage'],'execution_queue')
        self.assertEqual(row['error_code'],'QUEUE_EXPIRED')
        self.assertEqual(row['queue_age_seconds'],3.1)
        self.assertNotIn('secret',json.dumps(row))
        rejected=sanitize(dict(event='action_completed', guard_stage='secret-stage',
                               error_code='secret-code', queue_age_seconds=float('inf')))
        self.assertNotIn('guard_stage',rejected);self.assertNotIn('error_code',rejected)
        self.assertNotIn('queue_age_seconds',rejected)

    def test_clock_failure_cannot_break_recording(self):
        def bad():raise OSError('secret')
        trace=DetailedDiagnostics(bad)
        trace.record('owner_step')
        self.assertEqual(trace.drain(), [])

    def test_actions_status_replay_conflict_and_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.db');self.addCleanup(store.close)
            bot=Controller(store,Simulator());calls=[]
            def execute(action):
                calls.append(action)
                raise TimeoutError('password=NEVER_EXPORT')
            gate=ActionGateway(bot,execute);gate.diagnostics=DetailedDiagnostics(lambda: 123)
            request={'id':'a'*32,'revision':0,'action':{'type':'command','text':'/start'}}
            self.assertEqual(gate.apply(request)['status'],'UNKNOWN')
            self.assertEqual(gate.apply(request)['status'],'UNKNOWN')
            self.assertEqual(len(calls),1)
            with self.assertRaises(Conflict):gate.apply(dict(request,id='b'*32))
            rows=gate.diagnostics.drain()
            self.assertEqual([r['event'] for r in rows],['action_reserved','action_completed','action_replayed','action_conflict'])
            self.assertEqual(rows[1]['error_type'],'TimeoutError')
            self.assertGreaterEqual(rows[1]['duration_ms'],0)
            self.assertNotIn('NEVER_EXPORT',json.dumps(rows))
            def refuse(action):raise ActionRejected('private rejection')
            gate.execute=refuse
            self.assertEqual(gate.apply(dict(request,id='c'*32,revision=1))['status'],'REJECTED')
            self.assertEqual(gate.diagnostics.drain()[-1]['error_type'],'ActionRejected')

    def test_feed_clock_changes_do_not_create_tick_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.db');self.addCleanup(store.close)
            bot=Controller(store,Simulator());logs=DiagnosticLogs()
            for i in range(10):
                logs.capture('paper',store,bot.state,123+i,{'feed':{'status':'FRESH','age_seconds':i},'metrics':{'profit':i},'paper':{'cursor':i}})
            self.assertEqual(len(logs.recent['paper']),1)
            logs.capture('paper',store,bot.state,140,{'feed':{'status':'STALE'}})
            self.assertEqual(len(logs.recent['paper']),2)

    def test_transport_rejection_is_sanitized(self):
        from tradfi.web_control import WebTransport
        from tradfi.control import OwnerMailbox
        web=WebTransport(OwnerMailbox(),password='test-password-long')
        web.diagnostics=DetailedDiagnostics(lambda:123)
        code,_,_=web.request('GET','/api/paper/state',{'Host':'wrong','Authorization':'SECRET'})
        self.assertEqual(code,403)
        rows=web.diagnostics.drain()
        self.assertEqual(rows[0]['error_code'],'ORIGIN_DENIED')
        self.assertNotIn('SECRET',json.dumps(rows))

    def test_capture_archive_download_and_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(Path(tmp)/'state.db');self.addCleanup(store.close)
            bot=Controller(store,Simulator());path=Path(tmp)/'runtime.log'
            logs=DiagnosticLogs(path);self.addCleanup(logs.close)
            trace=DetailedDiagnostics(lambda:123);trace.record('owner_step',status='OK',duration_ms=4,password='NEVER_EXPORT')
            logs.capture('paper',store,bot.state,123,details=trace.drain())
            text=logs.document('paper')
            self.assertIn('owner_step',text);self.assertIn('duration_ms',text);self.assertNotIn('NEVER_EXPORT',text)
            self.assertTrue(Path(str(path)+'.paper.diagnostic.jsonl').is_file())
            logs.close()
            fresh=DiagnosticLogs(path);self.addCleanup(fresh.close)
            self.assertIn('owner_step',fresh.document('paper'))
            with patch.object(fresh,'document',return_value='a'+'я'*LIMIT):
                raw=fresh.response('paper',download=True)
                self.assertLessEqual(len(raw),LIMIT);raw.decode('utf-8')

    def test_archive_reprojects_injected_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'runtime.log'
            from tradfi.context_diagnostics import VERSION
            Path(str(path)+'.paper.diagnostic.jsonl').write_text(json.dumps({'version':VERSION,'scope':'paper','context':{},'diagnostics':[{'event':'owner_step','status':'OK','password':'NEVER_EXPORT','error_type':'NEVER_EXPORT'}]})+'\n')
            with Path(str(path)+'.paper.diagnostic.jsonl').open('a') as f:
                f.write(json.dumps({'version':[],'scope':'paper'})+'\n')
                f.write(json.dumps({'version':VERSION,'scope':'paper','context':{'id':'\ud800'}})+'\n')
            logs=DiagnosticLogs(path)
            self.assertNotIn('NEVER_EXPORT',logs.document('paper'))
