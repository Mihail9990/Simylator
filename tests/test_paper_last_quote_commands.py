"""Explicit command last-price policy never enables autonomous stale execution."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tradfi.paper import PaperSession
from tradfi.control import ActionRejected, SnapshotStale, OwnerMailbox
from tradfi.storage import DurabilityError
from test_tradfi_paper import Reader

class LastQuoteCommands(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.reader=Reader();self.now=100.;self.path=Path(self.tmp.name)/'paper.db'
        self.session=PaperSession(self.path,self.reader,clock=lambda:self.reader.now,monotonic=lambda:self.now)
        self.addCleanup(lambda:self.session.close());self.session.step()
        self.session.controller.state['basket_settings']['INITIAL_SIZE']='.01'
        self.session.controller.save('fixture')
    @property
    def p(self):return self.session.exchange.p
    @property
    def bot(self):return self.session.controller
    def start(self):
        self.session.action({'type':'command','text':'/start'})
        self.reader.tick('4000','4000.1',seconds=30,bar=('3999','4000'));self.session.step();self.session.step()
        self.assertEqual(len(self.p['orders']),1)
    def initial(self):
        self.start();self.reader.tick('4000.9','4001');self.session.step();self.session.step()
        self.assertEqual(len(self.p['positions']),1)
    def stale(self):
        self.reader.now+=60;self.reader.reason='STALE_MT5';self.now+=1;self.session.step()
    def request(self,action,rid='last-price-request'):
        return dict(id=rid,revision=self.bot.state.get('control_revision',0),context_token=self.session.mailbox.view()['command_context_token'],action=action)
    def submit(self,action):
        req=self.request(action);self.session.mailbox.submit(req);self.session.step()
        return req,self.bot.state['control_receipts'][req['id']]
    def test_cancel_after_drag_and_stale_is_immediate_once(self):
        self.start();c=self.bot.state['cycle'];old=self.p['orders'][0]['id']
        self.session.action(dict(type='basket_levels',cycle_id=c['id'],levels={'BUY':'4002'},expected=self.bot.levels(),expected_orders=[old]))
        new=self.p['orders'][0]['id'];self.assertNotEqual(old,new);self.stale()
        req,r=self.submit(dict(type='cancel_initial',cycle_id=c['id'],expected_orders=[new]))
        self.assertEqual(r['status'],'APPLIED');self.assertFalse(self.p['orders']);self.assertEqual(self.bot.state['phase'],'INITIAL_WAIT')
        self.assertTrue(r['paper_quote']['stale']);before=deepcopy(self.p)
        self.assertEqual(self.session.mailbox.submit(req)['status'],'APPLIED');self.session.step();self.assertEqual(before,self.p)
    def test_stale_market_uses_bid_ask_without_automatic_ticks(self):
        self.initial();self.reader.tick('3998','3998.1');self.session.step();self.session.step()
        self.assertEqual(self.bot.state['phase'],'V3_WAIT_ENTRY');self.stale()
        with patch.object(self.bot,'tick',side_effect=AssertionError('autonomous tick')),patch.object(self.bot,'_follow',side_effect=AssertionError('follow')):
            req,r=self.submit(dict(type='v3_entry',cycle_id=self.bot.state['cycle']['id'],order_type='MARKET',timing='now',side='BUY'))
        self.assertEqual(r['status'],'APPLIED');self.assertEqual({D(p['entry']) for p in self.p['positions']},{D('3998.1')})
        self.assertTrue(all(d['paper_quote']['stale'] for d in self.p['deals'] if d['entry']=='IN' and d['price']=='3998.1'))
        before=deepcopy(self.p);self.session.step();self.assertEqual(before,self.p)
    def test_flatten_on_stale_bid_not_ask_and_no_replay(self):
        self.initial();self.stale();_,r=self.submit(dict(type='paper_flatten',confirmed=True))
        self.assertEqual(r['status'],'APPLIED');self.assertFalse(self.p['positions'])
        d=self.p['deals'][-1];self.assertEqual(d['price'],'4000.9');self.assertTrue(d['paper_quote']['stale'])
    def test_stale_quote_persists_restart_but_gap_not_resumed(self):
        self.initial();self.stale();self.session.close()
        self.session=PaperSession(self.path,self.reader,clock=lambda:self.reader.now,monotonic=lambda:self.now)
        self.session.step();self.assertTrue(self.p['paused_gap'])
        self.assertTrue(self.session.view()['paper']['command_quote']['available'])
        self.session.action(dict(type='paper_flatten',confirmed=True));self.assertFalse(self.p['positions']);self.assertTrue(self.p['paused_gap'])
    def test_no_quote_cancellation_and_no_invented_price(self):
        self.start();c=self.bot.state['cycle'];oid=self.p['orders'][0]['id'];self.p.pop('last_verified_market');self.session.exchange.persist();self.stale()
        self.session.action(dict(type='cancel_initial',cycle_id=c['id'],expected_orders=[oid]));self.assertFalse(self.p['orders'])
        with self.assertRaises(ActionRejected):self.session.action(dict(type='command',text='/start'))
    def test_unknown_blocks_new_effects_but_stop_still_cancels(self):
        self.start();self.bot.state.setdefault('control_receipts',{})['other']={'status':'UNKNOWN','revision':0,'digest':'x'};self.bot.save('fixture_unknown');self.stale()
        with self.assertRaises(ActionRejected):self.session.action(dict(type='paper_flatten',confirmed=True))
        self.session.action(dict(type='command',text='/stop'));self.assertFalse(self.p['orders']);self.assertEqual(self.bot.state['control_receipts']['other']['status'],'UNKNOWN')
    def test_failed_or_slow_owner_is_not_a_fresh_local_publication(self):
        self.start();self.stale();req=self.request(dict(type='paper_costs',commission_per_lot_side='2'));self.now+=3
        with self.assertRaises(SnapshotStale):self.session.mailbox.submit(req)
    def test_queue_expiry_and_generic_live_mailbox_stay_strict(self):
        self.start();self.stale();req=self.request(dict(type='paper_costs',commission_per_lot_side='2'));self.session.mailbox.submit(req);self.now+=3;self.session.step()
        self.assertEqual(self.bot.state['control_receipts'][req['id']]['status'],'REJECTED')
        with self.assertRaises(SnapshotStale):OwnerMailbox._require_fresh({'type':'cancel_initial'},dict(snapshot_freshness={'fresh':True},feed={'status':'WAITING'}))
    def test_old_unrelated_ready_intent_not_dispatched(self):
        self.initial();self.reader.tick('3998','3998.1');self.session.step();self.session.step();self.stale()
        cid=self.bot._intent('MARKET',side='BUY',price='3998.1',sl='3997',tp='3999',size='.01')
        self.session.action(dict(type='paper_costs',commission_per_lot_side='2'))
        self.assertEqual(self.bot.state['commands'][cid]['status'],'READY');self.assertFalse(self.p['positions'])
    def test_unrelated_command_cannot_finish_previous_drag(self):
        self.start();self.bot.action(dict(type='basket_levels',cycle_id=self.bot.state['cycle']['id'],levels={'BUY':'4002'},expected=self.bot.levels(),expected_orders=[self.p['orders'][0]['id']]))
        before=deepcopy(self.p);self.stale()
        with self.assertRaises(ActionRejected):self.session.action(dict(type='v3_settings',cycle_id=self.bot.state['cycle']['id'],entry_distance='1.2'))
        self.assertEqual(before,self.p);self.assertIsNotNone(self.bot.state['cycle'].get('edit'))
    def test_queued_cancel_loses_to_actual_fill_before_drain(self):
        self.start();req=self.request(dict(type='cancel_initial',cycle_id=self.bot.state['cycle']['id'],expected_orders=[self.p['orders'][0]['id']]))
        self.session.mailbox.submit(req);self.reader.tick('4000.9','4001');self.session.step()
        self.assertEqual(self.bot.state['control_receipts'][req['id']]['status'],'REJECTED')
        self.assertEqual(len(self.p['positions']),1);self.assertFalse(self.p['orders'])
    def test_atomic_cancel_fault_before_commit_restart_never_replays_unknown(self):
        self.start();self.stale();req=self.request(dict(type='cancel_initial',cycle_id=self.bot.state['cycle']['id'],expected_orders=[self.p['orders'][0]['id']]))
        self.session.mailbox.submit(req)
        def fault(event):
            if event=='paper_local_cancel_before_commit':raise RuntimeError('injected')
        self.session.store.fault=fault
        with self.assertRaises(DurabilityError):self.session.step()
        self.assertEqual(len(self.p['orders']),1);self.session.store.fault=None;self.session.close()
        self.session=PaperSession(self.path,self.reader,clock=lambda:self.reader.now,monotonic=lambda:self.now);self.session.step()
        self.assertEqual(self.session.mailbox.submit(req)['status'],'UNKNOWN');self.assertEqual(len(self.p['orders']),1)
        self.session.action(dict(type='command',text='/stop'));self.assertFalse(self.p['orders'])
        self.assertEqual(self.bot.state['control_receipts'][req['id']]['status'],'UNKNOWN')
    def test_atomic_cancel_fault_after_commit_replay_returns_applied(self):
        self.start();self.stale();req=self.request(dict(type='cancel_initial',cycle_id=self.bot.state['cycle']['id'],expected_orders=[self.p['orders'][0]['id']]))
        self.session.mailbox.submit(req)
        def fault(event):
            if event=='paper_local_cancel_after_commit':raise RuntimeError('injected')
        self.session.store.fault=fault
        with self.assertRaises(DurabilityError):self.session.step()
        self.session.store.fault=None;self.session.close()
        self.session=PaperSession(self.path,self.reader,clock=lambda:self.reader.now,monotonic=lambda:self.now);self.session.step()
        self.assertEqual(self.session.mailbox.submit(req)['status'],'APPLIED');self.assertFalse(self.p['orders'])
    def test_pending_stop_does_not_fill_from_last_quote_on_unrelated_command(self):
        self.start();self.stale();before=deepcopy(self.p['orders'])
        self.session.action(dict(type='paper_costs',commission_per_lot_side='2'))
        self.assertEqual(self.p['orders'],before);self.assertFalse(self.p['positions'])
    def test_invalid_or_unverified_new_sample_does_not_replace_last_quote(self):
        old=deepcopy(self.p['last_verified_market'])
        for mutation in ({'timestamp_semantics':'UNVERIFIED'},{'quote':{'bid':'-1','ask':'0'}},{'timestamp':float('nan')}):
            market=dict(deepcopy(old),**mutation)
            with self.assertRaises(ValueError):self.session.exchange.observe(market)
            self.assertEqual(self.p['last_verified_market'],old)
