"""Production PAPER wiring, virtual execution only (no MT5 object)."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from tradfi.paper import PaperSession
from tradfi.candle_recovery import CandleRecoveryController
from test_tradfi_paper import Reader

class CandlePaperSession(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.reader=Reader()
        self.session=PaperSession(Path(self.tmp.name)/'paper.db',self.reader,clock=lambda:self.reader.now,monotonic=lambda:self.reader.now)
        self.addCleanup(self.session.close)
        self.step()
        self.bot=self.session.controller
        self.bot.state['basket_settings'].update(INITIAL_SL_DISTANCE='1',INITIAL_TP_DISTANCE='2')
        self.bot.save('test_setup')
    def step(self,n=3):
        for _ in range(n):self.session.step()
    def start(self):
        self.session.action(dict(type='command',text='/start'))
        self.reader.tick('4000','4000.1',seconds=30,bar=('3999','4000'));self.step()
        self.assertEqual(len(self.session.exchange.p['orders']),1)
        self.reader.tick('4000.9','4001');self.step()
        self.assertEqual(len(self.session.exchange.p['positions']),1)
    def test_actual_session_uses_candle_controller_and_full_sl_follow(self):
        self.assertIsInstance(self.bot,CandleRecoveryController)
        self.start();self.reader.tick('4000','4000.1');self.step()
        c=self.bot.state['cycle'];self.assertEqual(D(c['follow_volume']),D('.03'))
        self.assertFalse(self.session.exchange.p['orders'])
        self.reader.tick('4000','4000.1',seconds=60,bar=('4001','4000'));self.step()
        orders=self.session.exchange.p['orders']
        self.assertEqual(len(orders),3);self.assertEqual({o['side'] for o in orders},{'SELL'})
        self.assertEqual({D(o['size']) for o in orders},{D('.01')})
    def test_close_all_on_last_quote_executes_without_future_tick(self):
        self.start();self.reader.reason='NO_NEW_TICK';self.reader.now+=60;self.step()
        self.session.action(dict(type='candle_close_all',cycle_id=self.bot.state['cycle']['id']))
        self.assertFalse(self.session.exchange.p['positions'])
        self.assertFalse(self.session.exchange.p['orders'])
        self.assertTrue(self.bot.state['stop'])
        self.assertTrue(all(t['closed'] for t in self.bot.state['cycle']['tickets'].values()))
    def test_profile_and_sl_settings_in_published_view(self):
        self.session.action(dict(type='candle_recovery_settings',cycle_id=None,trigger_1='.3',trigger_2='.5',offset_1='.2'))
        view=self.session.view()
        self.assertEqual(view['candle_recovery']['settings']['trigger_2'],'0.5')
        self.assertEqual(len(view['recovery_profiles']['profiles']),9)
