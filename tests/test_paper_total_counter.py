from copy import deepcopy
from pathlib import Path
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch
from tradfi.paper import PaperSession
from tradfi.control import ActionRejected
from tradfi.storage import DurabilityError
from test_tradfi_paper import Reader


class PaperTotalCounterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'paper.db';self.reader=Reader()
        self.open()
        self.session.step()
        self.append_closed('past','-4')
    def open(self):
        self.session=PaperSession(self.path,self.reader,clock=lambda:self.reader.now,monotonic=lambda:self.reader.now)
        self.addCleanup(self.session.close)
    def append_closed(self,pid,profit):
        for ent in ('IN','OUT'):
            self.session.exchange.p['deals'].append(dict(id=ent+pid,position_id=pid,order_id=ent+pid,command_id='',entry=ent,side='BUY',volume='.01',price='4000',time=self.reader.now-20 if ent=='IN' else self.reader.now-10,reason='EXPERT' if ent=='IN' else 'SL',profit='0' if ent=='IN' else profit,commission='-.1',swap='0',fee='0'))
        self.session.exchange.persist()
    def metrics(self):return self.session.view()['metrics']
    def request(self,id='reset-total-000001'):
        return dict(id=id,revision=self.session.controller.state.get('control_revision',0),action=dict(type='paper_reset_total',confirmed=True))
    def reopen(self):
        self.session.close();self.open()
    def test_negative_total_zero_then_new_realized_and_restart(self):
        before=deepcopy(self.session.exchange.p)
        self.assertEqual(Decimal(self.metrics()['all_closed_net']),Decimal('-4.2'))
        req=self.request();self.assertEqual(self.session.gateway.apply(req)['status'],'APPLIED')
        self.assertEqual(Decimal(self.metrics()['all_closed_net']),0)
        self.assertEqual(self.session.exchange.p,before)
        self.append_closed('later','2');self.assertEqual(Decimal(self.metrics()['all_closed_net']),Decimal('1.8'))
        baseline=deepcopy(self.session.controller.state['paper_total_baseline'])
        self.assertEqual(self.session.gateway.apply(req)['status'],'APPLIED')
        self.assertEqual(self.session.controller.state['paper_total_baseline'],baseline)
        self.reopen();self.assertEqual(Decimal(self.metrics()['all_closed_net']),Decimal('1.8'))
        self.assertEqual(Decimal(self.metrics()['lifetime_closed_net']),Decimal('-2.4'))
        self.session.gateway.apply(self.request('reset-total-000002'))
        self.assertEqual(Decimal(self.metrics()['all_closed_net']),0)
    def test_active_recovery_orders_history_and_commissions_unchanged(self):
        self.session.action(dict(type='command',text='/start'))
        self.reader.tick('4000','4000.1',seconds=30,bar=('3999','4000'))
        for _ in range(3):self.session.step()
        self.reader.tick('4000.9','4001')
        for _ in range(3):self.session.step()
        self.assertTrue(self.session.exchange.p['positions']);self.assertTrue(self.session.exchange.p['orders'])
        state=deepcopy(self.session.controller.state);exchange=deepcopy(self.session.exchange.p)
        with patch.object(self.session.controller,'tick',side_effect=AssertionError('strategy advanced')),patch.object(self.session.exchange,'submit',side_effect=AssertionError('trade effect')):
            self.session.action(dict(type='paper_reset_total',confirmed=True))
        after=deepcopy(self.session.controller.state);after.pop('paper_total_baseline')
        self.assertEqual(after,state);self.assertEqual(self.session.exchange.p,exchange)
        self.assertEqual(self.session.view()['position_history'],self.session._history_cache.view(exchange,state['currency'],bool(state.get('completed')),state['commands'])['position_history'])
    def test_no_quote_needed_and_confirmation_required(self):
        self.session.exchange.available=False
        with patch.object(self.reader,'read',side_effect=AssertionError('quote read')):
            for confirmed in (False,1,None):
                with self.assertRaises(ActionRejected):self.session.action(dict(type='paper_reset_total',confirmed=confirmed))
            self.session.action(dict(type='paper_reset_total',confirmed=True))
        self.assertEqual(Decimal(self.metrics()['all_closed_net']),0)
    def test_incomplete_history_and_bad_baseline_reject(self):
        self.session.exchange.p['deals'][1]['profit']='NaN'
        before=deepcopy(self.session.controller.state)
        with self.assertRaises(ActionRejected):self.session.action(dict(type='paper_reset_total',confirmed=True))
        self.assertEqual(self.session.controller.state,before)
        self.assertFalse(self.metrics()['total_reset_available'])
        self.session.exchange.p['deals'][1]['profit']='-4'
        self.session.controller.state['paper_total_baseline']=dict(amount='3',currency='USD',reset_at=self.reader.now)
        with self.assertRaises(ActionRejected):self.session.action(dict(type='paper_reset_total',confirmed=True))
        self.assertIsNone(self.metrics()['all_closed_net'])
    def test_failed_baseline_commit_not_successful_and_restart_recovers_reservation(self):
        req=self.request()
        def fail(event):
            if event=='paper_total_counter_reset':raise RuntimeError('disk failure')
        self.session.store.fault=fail
        with self.assertRaises(DurabilityError):self.session.gateway.apply(req)
        self.session.store.fault=None
        self.assertNotIn('paper_total_baseline',self.session.controller.state)
        self.reopen()
        self.assertEqual(self.session.controller.state['control_receipts'][req['id']]['status'],'REJECTED')
        self.assertEqual(Decimal(self.metrics()['all_closed_net']),Decimal('-4.2'))
    def test_crash_after_atomic_commit_receipt_is_applied(self):
        req=self.request();save=self.session.controller.save
        def crash(event):
            if event=='control_action_completed':raise SystemExit('simulated crash')
            return save(event)
        with patch.object(self.session.controller,'save',side_effect=crash):
            with self.assertRaises(SystemExit):self.session.gateway.apply(req)
        self.reopen()
        self.assertEqual(self.session.controller.state['control_receipts'][req['id']]['status'],'APPLIED')
        self.assertEqual(Decimal(self.metrics()['all_closed_net']),0)
        self.append_closed('later','2')
        self.session.gateway.apply(req)
        self.assertEqual(Decimal(self.metrics()['all_closed_net']),Decimal('1.8'))
    def test_crash_before_effect_preserves_baseline(self):
        self.session.gateway.apply(self.request())
        baseline=deepcopy(self.session.controller.state['paper_total_baseline'])
        req=self.request('reset-total-000002')
        with self.assertRaises(SystemExit):self.session.gateway.apply(req,pre_execute=lambda _:(_ for _ in ()).throw(SystemExit()))
        self.reopen()
        self.assertEqual(self.session.controller.state['paper_total_baseline'],baseline)
        self.assertEqual(self.session.controller.state['control_receipts'][req['id']]['status'],'REJECTED')

    def test_other_variant_metric_unchanged_and_reset_unavailable(self):
        from tradfi.paper_total_counter import counter_metrics
        metrics=dict(all_closed_net='4.2',history_complete=True)
        result=counter_metrics(metrics,{'currency':'UST','simulator_variant':'old'})
        self.assertEqual(result['all_closed_net'],'4.2')
        self.assertFalse(result['total_reset_available'])
    def test_open_positions_full_net_counts_after_later_close(self):
        self.session.action(dict(type='command',text='/start'))
        self.reader.tick('4000','4000.1',seconds=30,bar=('3999','4000'))
        for _ in range(3):self.session.step()
        self.reader.tick('4000.9','4001')
        for _ in range(3):self.session.step()
        self.session.action(dict(type='paper_reset_total',confirmed=True))
        position=self.session.exchange.p['positions'][0]
        pid=position['id']
        self.session.exchange._close(position,'MANUAL','4002')
        self.session.exchange.persist()
        net=sum((Decimal(d[key]) for d in self.session.exchange.p['deals'] if d['position_id']==pid for key in ('profit','commission','swap','fee')),Decimal(0))
        self.assertEqual(Decimal(self.metrics()['all_closed_net']),net)
