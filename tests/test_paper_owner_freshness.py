"""PAPER-only current execution evidence, reviewed context and finite queue TTL."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tradfi.paper import PaperSession
from test_tradfi_paper import Reader


class PaperOwnerFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.reader=Reader();self.now=100.
        self.paper=PaperSession(Path(self.tmp.name)/'paper.db',self.reader,
                                clock=lambda:self.reader.now,monotonic=lambda:self.now)
        self.addCleanup(self.paper.close)
        self.paper.step()

    def request(self, suffix='1', action=None):
        return dict(id='owner-freshness-'+suffix, context_token=self.paper.mailbox.view()['command_context_token'],
                    revision=self.paper.controller.state.get('control_revision',0),
                    action=action or dict(type='paper_costs',commission_per_lot_side='2'))

    def delayed(self, seconds=2.1):
        self.now+=seconds
        self.reader.tick('4000','4000.1',seconds=seconds)

    def status(self, request):
        return self.paper.controller.state['control_receipts'][request['id']]['status']

    def test_current_owner_data_recovers_expired_publication_within_queue_ttl(self):
        request=self.request();self.paper.mailbox.submit(request);self.delayed();self.paper.step()
        self.assertEqual(self.status(request),'APPLIED')
        self.assertEqual(self.paper.exchange.p['commission_per_lot_side'],'2')
        self.assertFalse(self.paper.mailbox.requests.qsize())

    def test_three_second_queue_boundary_rejects_even_with_new_fresh_quote(self):
        for seconds in (3, 3.1):
            request=self.request(str(seconds).replace('.','-'));self.paper.mailbox.submit(request)
            self.delayed(seconds);self.paper.step()
            self.assertEqual(self.status(request),'REJECTED')
            self.assertEqual(self.paper.exchange.p['commission_per_lot_side'],'0')
            completed=[r for r in self.paper.diagnostics.drain() if r['event']=='action_completed'][-1]
            self.assertEqual(completed['error_code'],'QUEUE_EXPIRED')
            self.assertEqual(completed['guard_stage'],'execution_queue')
            self.assertAlmostEqual(completed['queue_age_seconds'],seconds)

    def test_stale_quote_does_not_block_explicit_local_cost_command(self):
        request=self.request();self.paper.mailbox.submit(request)
        self.now+=2.1;self.reader.now+=6
        self.paper.step()
        self.assertEqual(self.status(request),'APPLIED')
        self.assertEqual(self.paper.exchange.p['commission_per_lot_side'],'2')
        self.assertTrue(self.paper.controller.state['control_receipts'][request['id']]['paper_quote']['stale'])

    def test_slow_current_read_is_not_made_fresh(self):
        request=self.request();self.paper.mailbox.submit(request)
        read=self.reader.read
        def slow():
            self.now+=2.1
            return read()
        with patch.object(self.reader,'read',slow):self.paper.step()
        self.assertEqual(self.status(request),'REJECTED')
        completed=[r for r in self.paper.diagnostics.drain() if r['event']=='action_completed'][-1]
        self.assertEqual(completed['error_code'],'SNAPSHOT_STALE')
        self.assertAlmostEqual(completed['snapshot_age_seconds'],2.1)

    def test_queue_deadline_is_rechecked_after_current_view_work(self):
        request=self.request();self.paper.mailbox.submit(request);self.delayed(2.5)
        current=self.paper.mailbox.current_execution_view
        def slow():
            self.now+=.6
            return current()
        self.paper.mailbox.current_execution_view=slow
        self.paper.step()
        self.assertEqual(self.status(request),'REJECTED')
        self.assertEqual(self.paper.exchange.p['commission_per_lot_side'],'0')
        completed=[r for r in self.paper.diagnostics.drain() if r['event']=='action_completed'][-1]
        self.assertEqual(completed['error_code'],'QUEUE_EXPIRED')
        self.assertAlmostEqual(completed['queue_age_seconds'],3.1)

    def test_changed_context_between_client_get_and_post_refuses_token(self):
        from tradfi.control import SnapshotStale
        request=self.request()
        self.paper.controller.state['stop']=not self.paper.controller.state['stop']
        self.paper.step()
        with self.assertRaises(SnapshotStale) as caught:self.paper.mailbox.submit(request)
        self.assertEqual(caught.exception.code,'CONTEXT_CHANGED')
        self.assertFalse(self.paper.mailbox.requests.qsize())

    def test_emergency_stop_keeps_existing_context_token_exemption(self):
        request=self.request(action=dict(type='command',text='/stop'))
        request['context_token']='f'*64
        self.paper.mailbox.submit(request)
        self.now+=10;self.reader.reason='offline'
        self.paper.step()
        self.assertEqual(self.status(request),'APPLIED')
        self.assertTrue(self.paper.controller.state['stop'])

    def test_exact_time_recheck_accepts_stale_token_without_widening_exemption(self):
        from tradfi.control import SnapshotStale
        request=self.request(action=dict(type='command',text='/resume_time'))
        request['context_token']='f'*64
        calls=[];self.paper.gateway.execute=lambda action:calls.append(action)
        self.paper.mailbox.submit(request)
        self.now+=10;self.reader.reason='offline';self.paper.step()
        self.assertEqual(self.status(request),'APPLIED')
        self.assertEqual(calls,[dict(type='command',text='/resume_time')])
        other=self.request('other',dict(type='command',text='/start'))
        other['context_token']='f'*64
        with self.assertRaises(SnapshotStale):self.paper.mailbox.submit(other)

    def test_legacy_request_keeps_original_preview_deadline(self):
        request=self.request();request.pop('context_token')
        self.paper.mailbox.submit(request);self.delayed();self.paper.step()
        self.assertEqual(self.status(request),'REJECTED')

    def test_projection_time_does_not_make_old_quote_fresh(self):
        request=self.request();self.paper.mailbox.submit(request)
        self.reader.data['timestamp']=self.reader.now-4.8
        view=self.paper.view
        def slow():
            result=view()
            self.now+=.4;self.reader.now+=.4
            return result
        with patch.object(self.paper,'view',slow):self.paper.step()
        self.assertEqual(self.status(request),'APPLIED')
        self.assertEqual(self.paper.exchange.p['commission_per_lot_side'],'2')
        quote=self.paper.controller.state['control_receipts'][request['id']]['paper_quote']
        self.assertTrue(quote['stale']);self.assertGreater(quote['age_seconds'],5)

    def test_changed_reviewed_context_refuses_without_rebase(self):
        request=self.request();self.paper.mailbox.submit(request);self.delayed()
        self.paper.controller.state['stop']=not self.paper.controller.state['stop']
        self.paper.step()
        self.assertEqual(self.status(request),'REJECTED')
        self.assertEqual(self.paper.exchange.p['commission_per_lot_side'],'0')
        completed=[r for r in self.paper.diagnostics.drain() if r['event']=='action_completed'][-1]
        self.assertEqual(completed['error_code'],'CONTEXT_CHANGED')
        self.assertEqual(completed['guard_stage'],'execution_context')

    def test_revision_change_and_unknown_never_rebase_or_repeat(self):
        first=self.request();second=self.request('second')
        self.paper.mailbox.submit(first);self.paper.mailbox.submit(second)
        calls=[]
        def unknown(action):calls.append(action);raise TimeoutError('do not repeat')
        self.paper.gateway.execute=unknown
        self.delayed();self.paper.step()
        self.assertEqual(self.status(first),'UNKNOWN')
        self.assertEqual(self.paper.mailbox.view()['action_results'][second['id']]['status'],'CONFLICT')
        self.assertEqual(len(calls),1)
        self.paper.mailbox.submit(first);self.paper.step()
        self.assertEqual(len(calls),1);self.assertEqual(self.status(first),'UNKNOWN')

    def test_duplicate_queued_request_executes_once(self):
        request=self.request();self.paper.mailbox.submit(request);self.paper.mailbox.submit(request)
        calls=[];execute=self.paper.gateway.execute
        def counted(action):calls.append(action);return execute(action)
        self.paper.gateway.execute=counted
        self.delayed();self.paper.step()
        self.assertEqual(self.status(request),'APPLIED');self.assertEqual(len(calls),1)
        self.paper.mailbox.submit(request);self.paper.step();self.assertEqual(len(calls),1)

    def test_reservation_delay_rechecks_current_freshness_and_is_logged(self):
        request=self.request();self.paper.mailbox.submit(request)
        save=self.paper.controller.save
        def slow(event):
            result=save(event)
            if event=='control_action_reserved':self.now+=2.1
            return result
        with patch.object(self.paper.controller,'save',slow):self.paper.step()
        self.assertEqual(self.status(request),'REJECTED')
        completed=[r for r in self.paper.diagnostics.drain() if r['event']=='action_completed'][-1]
        self.assertEqual(completed['guard_stage'],'execution_current')
        self.assertIn('reservation_ms',completed)
        self.assertAlmostEqual(completed['snapshot_age_seconds'],2.1)


class ManualOrderFreshnessTest(unittest.TestCase):
    def test_manual_market_delay_expiry_and_replay(self):
        import test_paper_interrupt as fixture
        for delay, expected in ((2.1,'APPLIED'),(3.001,'REJECTED')):
            with self.subTest(delay=delay):
                f=fixture.PaperInterruptTest();f.setUp()
                try:
                    f.initial();f.reader.tick('3998','3998.1');f.step(4)
                    p=f.session
                    self.assertEqual(p.controller.state['phase'],'V3_WAIT_ENTRY')
                    request=dict(id='manual-expiry-001',context_token=p.mailbox.view()['command_context_token'],revision=p.controller.state.get('control_revision',0),
                                 action=dict(type='v3_entry',cycle_id=p.controller.state['cycle']['id'],
                                             order_type='MARKET',timing='now',side='SELL'))
                    p.mailbox.submit(request);f.reader.tick('3998','3998.1',seconds=delay);f.step()
                    self.assertEqual(p.mailbox.view()['action_results'][request['id']]['status'],expected)
                    f.step(5);before=deepcopy(p.exchange.p)
                    self.assertEqual(p.mailbox.submit(request)['status'],expected)
                    f.step();self.assertEqual(p.exchange.p,before)
                    if expected=='REJECTED':
                        self.assertFalse(p.exchange.p['positions'])
                        retry=dict(request,id='manual-retry-002',context_token=p.mailbox.view()['command_context_token'],revision=p.controller.state['control_revision'])
                        p.mailbox.submit(retry);f.step()
                        self.assertEqual(p.mailbox.view()['action_results'][retry['id']]['status'],'APPLIED')
                        f.step(5)
                    self.assertEqual(len(p.exchange.p['positions']),6)
                finally:
                    f.tearDown();f.doCleanups()
