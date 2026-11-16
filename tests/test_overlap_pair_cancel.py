"""PAPER shortcut cancellation must honor overlap logical ladder ownership."""
from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_candle_paper_session as fixtures
from tradfi.storage import DurabilityError

class PairCancelTests(unittest.TestCase):
    setUp=fixtures.CandlePaperSession.setUp
    step=fixtures.CandlePaperSession.step
    start=fixtures.CandlePaperSession.start
    def pair(self):
        self.start()
        p=self.session.exchange.p['positions'][0];entry=D(p['entry'])
        self.reader.tick(str(entry+D('.25')),str(entry+D('.35')));self.step(8)
        support=self.session.exchange.p['positions'][0]['sl']
        self.reader.tick(support,str(D(support)+D('.1')));self.step(10)
        self.assertEqual(len(self.session.exchange.p['orders']),6)
        self.assertFalse(self.session.exchange.p['positions'])
    def cancel(self):
        self.session.action(dict(type='v3_cancel_entry',cycle_id=self.bot.state['cycle']['id']))
    def test_pair_cancels_both_sides_and_does_not_recreate_on_restart(self):
        self.pair();debt=self.bot.state['cycle']['recovery'];deals=deepcopy(self.session.exchange.p['deals'])
        ids={o['id'] for o in self.session.exchange.p['orders']}
        self.cancel()
        self.assertFalse(self.session.exchange.p['orders'])
        self.assertTrue(all(link.get('disabled') for link in self.bot.state['cycle']['overlap_ladders'].values()))
        self.assertEqual(self.bot.state['cycle']['recovery'],debt)
        self.assertEqual(self.session.exchange.p['deals'],deals)
        self.assertEqual(ids,{o['id'] for o in self.session.exchange.p['order_history'] if o['id'] in ids and o['status']=='CANCELLED'})
        cls=type(self.bot)
        self.session.controller=cls(self.session.store,self.session.exchange,clock=lambda:self.reader.now)
        self.bot=self.session.controller
        for side in ('BUY','SELL','BUY'):
            self.reader.tick('4000','4000.1',seconds=60,bar=('3999','4000') if side=='BUY' else ('4001','4000'))
            self.step(8)
        self.assertFalse(self.session.exchange.p['orders'])
        self.assertEqual(self.bot.state['cycle']['recovery'],debt)
    def test_existing_live_position_is_untouched(self):
        self.start();positions=deepcopy(self.session.exchange.p['positions']);deals=deepcopy(self.session.exchange.p['deals'])
        self.cancel()
        self.assertEqual(self.session.exchange.p['positions'],positions)
        self.assertEqual(self.session.exchange.p['deals'],deals)
        self.assertFalse(self.session.exchange.p['orders'])
        self.step(8)
        self.assertFalse(self.session.exchange.p['orders'])
    def test_cancellation_rollback_keeps_logical_and_physical_pair(self):
        self.pair();state=deepcopy(self.bot.state);exchange=deepcopy(self.session.exchange.p)
        def fail(event):
            if event=='paper_local_cancel_before_commit':raise RuntimeError('disk failure')
        self.session.store.fault=fail
        with self.assertRaises(DurabilityError):self.cancel()
        self.session.store.fault=None
        self.assertEqual(self.bot.state,state);self.assertEqual(self.session.exchange.p,exchange)
        self.assertEqual(self.session.store.load(),state)
