"""Production PAPER wiring, virtual execution only (no MT5 object)."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from tradfi.paper import PaperSession
from tradfi.sl_overlap_recovery import SLOverlapRecoveryController
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
    def test_actual_session_uses_overlap_and_same_tick_reverse(self):
        self.assertIsInstance(self.bot,SLOverlapRecoveryController)
        self.start()
        orders=self.session.exchange.p['orders'];self.assertEqual(len(orders),3)
        original=orders[0]['price']
        self.reader.tick(original,str(D(original)+D('.1')));self.step()
        positions=self.session.exchange.p['positions']
        self.assertEqual(len(positions),3);self.assertEqual({p['side'] for p in positions},{'SELL'})
        self.assertEqual(len(self.session.exchange.p['orders']),4)
        self.assertGreater(D(self.bot.state['cycle']['recovery']),0)
    def test_close_all_on_last_quote_executes_without_future_tick(self):
        self.start();self.reader.reason='NO_NEW_TICK';self.reader.now+=60;self.step()
        self.session.action(dict(type='candle_close_all',cycle_id=self.bot.state['cycle']['id']))
        self.assertFalse(self.session.exchange.p['positions'])
        self.assertFalse(self.session.exchange.p['orders'])
        self.assertTrue(self.bot.state['stop'])
        self.assertTrue(all(t['closed'] for t in self.bot.state['cycle']['tickets'].values()))
    def test_real_quote_support_pair_buy_fill_cancels_sibling_and_reanchors(self):
        self.start()
        exchange=self.session.exchange
        position=exchange.p['positions'][0]
        entry=D(position['entry']); original=D(position['sl'])
        self.reader.tick(str(entry+D('.25')),str(entry+D('.35')));self.step(8)
        support=D(exchange.p['positions'][0]['sl'])
        self.assertEqual(support,entry-D('.25'))
        self.reader.tick(str(support),str(support+D('.1')));self.step(10)
        self.assertFalse(exchange.p['positions'])
        buys=[o for o in exchange.p['orders'] if o['side']=='BUY']
        sells=[o for o in exchange.p['orders'] if o['side']=='SELL']
        self.assertEqual((len(buys),len(sells)),(3,3))
        self.assertEqual({D(o['price']) for o in buys},{support+D('.8')})
        self.assertEqual({D(o['price']) for o in sells},{original})
        old_sell_ids={o['id'] for o in sells}
        trigger=D(buys[0]['price'])
        self.reader.tick(str(trigger-D('.1')),str(trigger));self.step(12)
        self.assertEqual(len(exchange.p['positions']),3)
        self.assertEqual({p['side'] for p in exchange.p['positions']},{'BUY'})
        self.assertFalse(old_sell_ids & {o['id'] for o in exchange.p['orders']})
        self.assertEqual({o['side'] for o in exchange.p['orders']},{'SELL'})
        self.assertEqual({D(o['price']) for o in exchange.p['orders']},
                         {D(p['sl']) for p in exchange.p['positions']})
    def test_profile_and_sl_settings_in_published_view(self):
        self.session.action(dict(type='candle_recovery_settings',cycle_id=None,trigger_1='.3',trigger_2='.5',offset_1='.2'))
        view=self.session.view()
        self.assertEqual(view['candle_recovery']['settings']['trigger_2'],'0.5')
        self.assertEqual(len(view['recovery_profiles']['profiles']),8)
