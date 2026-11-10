from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_tradfi_basket as fixtures
from tradfi.basket_latest_close import LatestCloseBasketController
from tradfi.basket_math import last_closed_sl_price

class LatestClose(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.BasketTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.bot=LatestCloseBasketController(self.f.store,self.f.b)
        self.f.bot.state['tp_distances']=['.20']+[None]*19
    def first_sl(self,side='BUY'):
        f=self.f;f.start(side);f.b.fill(side);f.tick(2)
        self.assertFalse(f.b.data['orders'])
        self.entry=f.b.data['positions'][0]['entry']
        self.exit='3997.8' if side=='BUY' else '4002.2'
        f.b.close('SL',price=self.exit);f.tick(4)
    def fill_basket(self,side='SELL'):
        f=self.f
        for _ in range(6):f.b.fill(side)
        entry=D(f.b.data['positions'][0]['entry']);f.b.quote(entry-D('.05'),entry+D('.05'));f.tick(3)
        self.assertEqual(len(f.b.data['positions']),6);self.assertFalse(f.b.data['orders'])
    def test_first_buy_actual_exit_and_oco(self):
        self.first_sl();f=self.f
        self.assertEqual(f.bot.state['cycle']['expanded'],{'BUY':self.entry,'SELL':'3996.80'})
        self.assertEqual(len(f.b.data['orders']),12);self.fill_basket()
    def test_first_sell_mirror(self):
        self.first_sl('SELL');f=self.f
        self.assertEqual(f.bot.state['cycle']['expanded'],{'BUY':'4003.20','SELL':self.entry})
        self.fill_basket('BUY')
    def test_latest_exit_not_mean_and_restart(self):
        self.first_sl();self.fill_basket();f=self.f
        for i,p in enumerate(list(f.b.data['positions'])):
            f.b.data['now']+=1;f.b.close('SL',position_id=p['id'],price=str(D('3998.1')+D(i)/10))
        f.tick(4);self.assertEqual(f.bot.state['cycle']['expanded'],{'BUY':'3999.60','SELL':'3997.60'})
        orders=deepcopy(f.b.data['orders']);f.bot=LatestCloseBasketController(f.store,f.b);f.tick(3)
        self.assertEqual(f.b.data['orders'],orders)
    def test_partial_tp_then_remaining_sl(self):
        self.first_sl();self.fill_basket();f=self.f
        for i,p in enumerate(list(f.b.data['positions'])):
            f.b.data['now']+=1
            f.b.close('TP' if i<2 else 'SL',position_id=p['id'],price='3996.7' if i<2 else str(D('3998.1')+D(i)/10))
        f.tick(4);self.assertEqual(f.bot.state['cycle']['expanded'],{'BUY':'3999.60','SELL':'3997.60'})
        self.assertEqual(f.bot.state['cycle']['range_anchor_exit'],'3998.6')
    def test_ambiguous_same_timestamp_halts(self):
        self.first_sl();self.fill_basket();f=self.f
        for i,p in enumerate(list(f.b.data['positions'])):f.b.close('SL',position_id=p['id'],price=str(D('3998')+D(i)/10))
        f.tick(4);self.assertTrue(f.bot.state['cycle'].get('manual'));self.assertFalse(f.b.data['orders'])
    def test_both_sides_fill_halts_preserving_positions(self):
        self.first_sl();f=self.f;f.b.fill('BUY');f.b.fill('SELL');f.tick(4)
        self.assertTrue(f.bot.state['cycle'].get('manual'));self.assertEqual(len(f.b.data['positions']),2)
    def test_initial_tp_no_recovery(self):
        f=self.f;f.start();f.b.fill('BUY');f.tick(2);f.b.close('TP');f.tick(5)
        self.assertIsNone(f.bot.state['cycle']);self.assertFalse(f.b.data['orders'])
    def test_old_active_cycle_not_reinterpreted(self):
        f=self.f;f.bot=fixtures.BasketController(f.store,f.b);f.start();before=deepcopy(f.bot.state['cycle'])
        f.bot=LatestCloseBasketController(f.store,f.b);f.tick(2)
        self.assertEqual(f.bot.state['phase'],'MIGRATION_BLOCK');self.assertEqual(f.bot.state['cycle'],before)
    def test_ordering_uses_time_not_list_or_id(self):
        t=dict(closed=True,size='.02',exit_facts=[dict(id='zzz',price='20',volume='.01',time=1,reason='SL'),dict(id='aaa',price='21',volume='.01',time=2,reason='SL')])
        self.assertEqual(last_closed_sl_price([t]),'21');t['exit_facts'].reverse();self.assertEqual(last_closed_sl_price([t]),'21')
    def test_missing_facts_and_unclosed_refused(self):
        for t in [dict(closed=False,size='.01'),dict(closed=True,size='.01',exit_facts=[])]:
            with self.assertRaises(ValueError):last_closed_sl_price([t])

    def test_every_recovery_sl_replaces_previous_range(self):
        self.first_sl();self.fill_basket();f=self.f
        for p in list(f.b.data['positions']):f.b.close('SL',position_id=p['id'],price='3998.60')
        f.tick(4);self.fill_basket()
        for p in list(f.b.data['positions']):f.b.close('SL',position_id=p['id'],price='3999.10')
        f.tick(4);self.assertEqual(f.bot.state['cycle']['expanded'],{'BUY':'4000.10','SELL':'3998.10'})
    def test_unknown_oco_cancel_blocks_reentry(self):
        self.first_sl();f=self.f
        for _ in range(6):f.b.fill('SELL')
        f.b.fail_before.add('CANCEL');f.tick(4)
        before=len([c for c in f.b.commands if c['kind']=='STOP'])
        for p in list(f.b.data['positions']):f.b.close('SL',position_id=p['id'],price='3998.60')
        f.tick(5)
        self.assertEqual(len([c for c in f.b.commands if c['kind']=='STOP']),before)
    def test_readonly_cannot_start(self):
        self.f.b.read_only=True
        with self.assertRaises(ValueError):self.f.bot.command('/start')
        self.assertFalse(self.f.b.commands)

    def test_initial_plain_tp_and_automatic_recovery_ladder(self):
        f=self.f;f.start()
        initial=f.b.data['orders'][0]
        self.assertEqual(D(initial['tp'])-D(initial['price']),D('.25'))
        f.b.fill('BUY');f.tick(2);self.assertFalse(f.b.data['orders'])
        f.b.close('SL',price='3997.8');f.tick(4)
        for side in ('BUY','SELL'):
            prices=[D(o['tp']) for o in f.b.data['orders'] if o['side']==side]
            self.assertEqual(len(prices),6);self.assertEqual(len(set(prices)),6)

    def test_unknown_future_cost_is_not_written_off_after_ladder_tp(self):
        self.first_sl();self.fill_basket();f=self.f
        for p in list(f.b.data['positions']):f.b.close('TP',position_id=p['id'],commission='-1')
        f.tick(4);self.assertGreater(D(f.bot.state['cycle']['recovery']),0)
        self.assertTrue(f.bot.state['cycle'].get('manual'));self.assertFalse(f.b.data['orders'])

