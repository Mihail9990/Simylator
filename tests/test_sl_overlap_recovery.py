from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from tradfi.sl_overlap_recovery import SLOverlapRecoveryController, volume_for_debt, PROFILES
from tradfi.interactive_simulator import InteractiveSimulator
from tradfi.simulator import ARTIFICIAL_SPEC
from tradfi.storage import Store, DurabilityError

class OverlapTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'paper.db');self.addCleanup(self.store.close)
        self.b=InteractiveSimulator(dict(ARTIFICIAL_SPEC,volume_min='.01',volume_step='.01',contract_size='100'))
        self.b.data.update(now=90,candle=dict(id=0,open='3999',close='4000'))
        self.bot=SLOverlapRecoveryController(self.store,self.b)
    def tick(self,n=5):
        for _ in range(n):self.bot.tick()
    def start(self):
        self.bot.command('/start');self.b.data.update(now=120,candle=dict(id=60,open='3999',close='4000'));self.tick()
        self.assertEqual(len(self.b.data['orders']),1)
        self.b.fill('BUY');p=self.b.data['positions'][0];e=D(p['entry']);self.b.quote(e-D('.05'),e+D('.05'));self.tick()
        return p
    def test_defaults_and_boundaries(self):
        cfg=self.bot.state['basket_settings'];self.assertEqual(cfg['INITIAL_ENTRY_DISTANCE'],'0.80');self.assertEqual(cfg['INITIAL_SL_DISTANCE'],'1.00')
        for debt,expected in [('0','.03'),('3.499','.03'),('3.5','.04'),('6.5','.05'),('10','.06'),('15','.07'),('21','.08'),('28','.09'),('36','.10'),('50','.10')]:self.assertEqual(volume_for_debt(debt),D(expected))
        self.assertIsNone(volume_for_debt('50.0001'))
        self.assertEqual(PROFILES[8],('.70','1.30','2','3','3.70','4.70','5.70','5.90'))
    def test_automatic_original_sl_and_restart_no_duplicates(self):
        p=self.start();orders=deepcopy(self.b.data['orders'])
        self.assertEqual(len(orders),3);self.assertEqual({D(o['price']) for o in orders},{D(p['sl'])})
        self.bot=SLOverlapRecoveryController(self.store,self.b);self.tick();self.assertEqual(self.b.data['orders'],orders)
    def test_fixed_orders_after_support_sl_and_source_close(self):
        p=self.start();orders=deepcopy(self.b.data['orders']);e=D(p['entry'])
        p['tp']=str(e+2);self.b.quote(e+D('.4'),e+D('.5'));self.tick();self.assertEqual(D(p['sl']),e)
        self.assertEqual(self.b.data['orders'],orders)
        self.b.close('SL',profit='-.50');self.tick();self.assertEqual([o for o in self.b.data['orders'] if o['side']=='SELL'],orders)
        self.assertEqual(len([o for o in self.b.data['orders'] if o['side']=='BUY']),3)
        self.assertEqual(self.bot.state['phase'],'RECOVERY_PENDING')
    def test_loss_and_reverse_fill_arms_projected_four(self):
        p=self.start();self.b.close('SL')
        for _ in range(3):self.b.fill('SELL')
        p=self.b.data['positions'][0];e=D(p['entry']);self.b.quote(e-D('.05'),e+D('.05'));self.tick()
        self.assertEqual(len(self.b.data['orders']),4)
        self.assertEqual({o['side'] for o in self.b.data['orders']},{'BUY'})
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('2'))
        self.assertEqual(D(self.bot.state['cycle']['groups'][-1]['projected_recovery_at_placement']),D('5'))
    def test_tp_cancels_reverse_before_finish(self):
        self.start();self.b.close('TP');self.tick(10)
        self.assertFalse(self.b.data['orders']);self.assertIsNone(self.bot.state['cycle'])
    def test_foreign_variant_database_rejected(self):
        self.bot.state.pop('simulator_variant');self.bot.save('foreign')
        with self.assertRaises(DurabilityError):SLOverlapRecoveryController(self.store,self.b)
    def test_closed_gap_group_no_market_replacement(self):
        self.start();self.b.close('SL')
        for _ in range(3):self.b.fill('SELL')
        for p in list(self.b.data['positions']):self.b.close('SL',position_id=p['id'])
        self.tick();self.assertFalse(self.b.data['orders']);self.assertTrue(self.bot.state['cycle']['manual_required'])
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('5'))
    def test_threshold_edits_reprofile_pending_without_moving_price(self):
        p=self.start();before=deepcopy(self.b.data['orders']);c=self.bot.state['cycle']
        self.bot.action(dict(type='overlap_thresholds',cycle_id=c['id'],thresholds=[dict(upper='50',profile_id='volume_8')]))
        self.tick(10);self.assertEqual(len(self.b.data['orders']),8)
        self.assertEqual({o['price'] for o in self.b.data['orders']},{before[0]['price']})
        self.bot=SLOverlapRecoveryController(self.store,self.b);self.tick()
        self.assertEqual(len(self.b.data['orders']),8)
        self.b.close('SL')
        for _ in range(8):self.b.fill('SELL')
        e=D(self.b.data['positions'][0]['entry']);self.b.quote(e-D('.05'),e+D('.05'));self.tick()
        self.bot.action(dict(type='overlap_thresholds',cycle_id=c['id'],thresholds=[dict(upper='50',profile_id='volume_3')]))
        self.tick(10);self.assertEqual(len(self.b.data['positions']),8);self.assertEqual(len(self.b.data['orders']),3)
    def test_invalid_threshold_edit_is_atomic(self):
        from tradfi.control import ActionRejected
        before=deepcopy(self.bot.state['overlap_thresholds'])
        for rows in ([],[dict(upper='0',profile_id='volume_3')],[dict(upper='50',profile_id='nope')],[dict(upper='5',profile_id='volume_3'),dict(upper='4',profile_id='volume_4')]):
            with self.assertRaises(ActionRejected):self.bot.action(dict(type='overlap_thresholds',cycle_id=None,thresholds=rows))
            self.assertEqual(self.bot.state['overlap_thresholds'],before)
    def test_projected_high_debt_blocks_next_but_keeps_protection(self):
        self.bot.state['basket_carry']='49';self.bot.save('debt')
        p=self.start();self.assertFalse(self.b.data['orders'])
        self.b.close('SL');self.tick();self.assertTrue(self.bot.state['cycle']['manual_required'])
        self.assertEqual(self.bot.state['phase'],'V3_WAIT_ENTRY')
    def test_cancelled_auto_link_not_recreated(self):
        self.start();c=self.bot.state['cycle'];self.bot.action(dict(type='v3_cancel_link',cycle_id=c['id']))
        self.tick(10);self.assertFalse(self.b.data['orders'])
        self.b.close('SL');self.tick();self.assertTrue(c['manual_required']);self.assertFalse(self.b.data['orders'])
    def test_profile_delete_cannot_orphan_thresholds(self):
        from tradfi.control import ActionRejected
        settings=deepcopy(self.bot.state['recovery_profiles']);settings['profiles']=settings['profiles'][1:]
        with self.assertRaises(ActionRejected):self.bot.action(dict(type='recovery_profiles',cycle_id=None,**settings))
        self.assertEqual(len(self.bot.state['recovery_profiles']['profiles']),8)
    def test_manual_continuation_same_direction_and_permission_consumed(self):
        self.start();c=self.bot.state['cycle']
        self.bot.action(dict(type='v3_cancel_link',cycle_id=c['id']));self.tick()
        self.b.close('SL');self.tick()
        self.bot.action(dict(type='candle_recovery_resume',cycle_id=c['id'],volume='.03',profile_id='volume_3'))
        self.b.quote('4000','4000.1');self.b.data.update(now=240,candle=dict(id=180,open='3999',close='4000'));self.tick()
        self.assertEqual(len(self.b.data['orders']),3)
        self.assertTrue(c['groups'][0]['retired'])
        for _ in range(3):self.b.fill('BUY')
        e=D(self.b.data['positions'][0]['entry']);self.b.quote(e-D('.05'),e+D('.05'));self.tick()
        self.assertNotIn('recovery_follow',c)
        self.bot.action(dict(type='v3_cancel_link',cycle_id=c['id']));self.tick()
        for p in list(self.b.data['positions']):self.b.close('SL',position_id=p['id'])
        self.tick();self.b.quote('4000','4000.1');self.b.data.update(now=300,candle=dict(id=240,open='3999',close='4000'));self.tick()
        self.assertFalse(self.b.data['orders']);self.assertTrue(c['manual_required'])
    def test_recovered_partial_basket_cancels_reverse_and_closes_remainder(self):
        self.start();self.b.close('SL')
        for _ in range(3):self.b.fill('SELL')
        e=D(self.b.data['positions'][0]['entry']);self.b.quote(e-D('.05'),e+D('.05'));self.tick()
        self.assertEqual(len(self.b.data['orders']),4)
        first=self.b.data['positions'][0]
        self.b.close('TP',position_id=first['id'],profit='3')
        self.tick(12)
        self.assertFalse(self.b.data['orders']);self.assertFalse(self.b.data['positions'])
        self.assertIsNone(self.bot.state['cycle'])
        self.assertEqual(len([cmd for cmd in self.b.commands if cmd['kind']=='CLOSE']),2)
    def test_protected_source_loss_and_partial_tp_recovery_cleanup(self):
        p=self.start();e=D(p['entry']);p['tp']=str(e+2)
        self.b.quote(e+D('.30'),e+D('.40'));self.tick()
        self.assertEqual(D(p['sl']),e-D('.25'))
        self.b.close('SL');self.tick()
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('.50'))
        for _ in range(3):self.b.fill('SELL')
        e=D(self.b.data['positions'][0]['entry']);self.b.quote(e-D('.05'),e+D('.05'));self.tick()
        p=self.b.data['positions'][0];self.b.close('TP',position_id=p['id']);self.tick()
        p=self.b.data['positions'][0];self.b.close('TP',position_id=p['id']);self.tick(12)
        self.assertFalse(self.b.data['positions']);self.assertFalse(self.b.data['orders']);self.assertIsNone(self.bot.state['cycle'])
    def test_support_sl_reprofiles_projected_debt_but_not_entry(self):
        self.bot.state['basket_carry']='5';self.bot.save('debt');p=self.start();e=D(p['entry']);p['tp']=str(e+2)
        self.assertEqual(len(self.b.data['orders']),5);price=self.b.data['orders'][0]['price']
        self.b.quote(e+D('.25'),e+D('.35'));self.tick(12)
        self.assertEqual(D(p['sl']),e-D('.25'));self.assertEqual(len(self.b.data['orders']),4)
        self.assertEqual({o['price'] for o in self.b.data['orders']},{price})
        old=deepcopy(self.b.data['orders']);self.tick(10);self.assertEqual(self.b.data['orders'],old)
    def test_fill_during_pending_replacement_freezes_old_volume(self):
        self.start();c=self.bot.state['cycle']
        self.bot.action(dict(type='overlap_thresholds',cycle_id=c['id'],thresholds=[dict(upper='50',profile_id='volume_8')]))
        from unittest.mock import patch
        with patch.object(self.bot, 'dispatch'):
            self.bot._step(self.bot.snapshot())  # durable cancel intents, not dispatched
        self.b.close('SL')
        for _ in range(3):self.b.fill('SELL')
        e=D(self.b.data['positions'][0]['entry']);self.b.quote(e-D('.05'),e+D('.05'))
        self.bot=SLOverlapRecoveryController(self.store,self.b);self.tick(12)
        self.assertEqual(len(self.b.data['positions']),3)
        self.assertEqual({o['side'] for o in self.b.data['orders']},{'BUY'})
        self.assertEqual(len(self.b.data['orders']),8)
    def test_support_stop_adds_same_side_from_nominal_sl_not_slippage(self):
        p=self.start();e=D(p['entry']);p['tp']=str(e+2);original=p['sl']
        self.bot.state['v3_settings']['entry_distance']='.90';self.bot.save('distance')
        self.b.quote(e+D('.25'),e+D('.35'));self.tick();support=D(p['sl'])
        self.b.close('SL',price=support-D('.10'));self.tick(10)
        buys=[o for o in self.b.data['orders'] if o['side']=='BUY'];sells=[o for o in self.b.data['orders'] if o['side']=='SELL']
        self.assertEqual(len(buys),3);self.assertEqual(len(sells),3)
        self.assertEqual({D(o['price']) for o in buys},{support+D('.90')})
        self.assertEqual({D(o['price']) for o in sells},{D(original)})
        self.bot=SLOverlapRecoveryController(self.store,self.b);self.tick(10)
        self.assertEqual(len(self.b.data['orders']),6)
    def test_sell_support_reentry_is_below_nominal_support(self):
        self.bot.command('/start');self.b.data.update(now=120,candle=dict(id=60,open='4001',close='4000'));self.tick()
        self.b.fill('SELL');p=self.b.data['positions'][0];e=D(p['entry']);p['tp']=str(e-2)
        self.b.quote(e-D('.05'),e+D('.05'));self.tick();original=D(p['sl'])
        self.b.quote(e-D('.35'),e-D('.25'));self.tick();support=D(p['sl']);self.assertEqual(support,e+D('.25'))
        self.b.close('SL',price=support+D('.10'));self.tick(10)
        sells=[o for o in self.b.data['orders'] if o['side']=='SELL'];buys=[o for o in self.b.data['orders'] if o['side']=='BUY']
        self.assertEqual(len(sells),3);self.assertEqual(len(buys),3)
        self.assertEqual({D(o['price']) for o in sells},{support-D('.80')})
        self.assertEqual({D(o['price']) for o in buys},{original})
    def support_pair(self):
        p=self.start();e=D(p['entry']);p['tp']=str(e+2)
        self.b.quote(e+D('.25'),e+D('.35'));self.tick();self.b.close('SL');self.tick(10)
        self.assertEqual(len(self.b.data['orders']),6)
    def test_support_pair_buy_fill_cancels_old_sell_moves_to_new_buy_sl(self):
        self.support_pair();old_sell={o['id'] for o in self.b.data['orders'] if o['side']=='SELL'}
        for _ in range(3):self.b.fill('BUY')
        p=self.b.data['positions'][0];e=D(p['entry']);self.b.quote(e-D('.05'),e+D('.05'));self.tick(12)
        self.assertFalse(old_sell & {o['id'] for o in self.b.data['orders']})
        self.assertEqual({o['side'] for o in self.b.data['orders']},{'SELL'})
        self.assertEqual({D(o['price']) for o in self.b.data['orders']},{D(p['sl'])})
        self.assertEqual(len(self.b.data['positions']),3)
    def test_support_pair_sell_fill_cancels_same_side_buy(self):
        self.support_pair();old_buy={o['id'] for o in self.b.data['orders'] if o['side']=='BUY'}
        for _ in range(3):self.b.fill('SELL')
        p=self.b.data['positions'][0];e=D(p['entry']);self.b.quote(e-D('.05'),e+D('.05'));self.tick(12)
        self.assertFalse(old_buy & {o['id'] for o in self.b.data['orders']})
        self.assertEqual({o['side'] for o in self.b.data['orders']},{'BUY'})
        self.assertEqual({D(o['price']) for o in self.b.data['orders']},{D(p['sl'])})
    def test_oco_both_filled_race_halts_without_extra_exposure(self):
        self.support_pair()
        for side in ('BUY','SELL'):
            for _ in range(3):self.b.fill(side)
        self.tick();self.assertEqual(self.bot.state['phase'],'MANUAL')
        self.assertEqual(len(self.b.data['positions']),6);self.assertFalse(self.b.data['orders'])
    def test_common_cancel_disables_both_logical_ladders_across_restart(self):
        self.support_pair();c=self.bot.state['cycle']
        self.bot.action(dict(type='v3_cancel_entry',cycle_id=c['id']));self.tick(12)
        self.assertFalse(self.b.data['orders'])
        self.bot=SLOverlapRecoveryController(self.store,self.b);self.tick(12)
        self.assertFalse(self.b.data['orders']);self.assertTrue(self.bot.state['cycle']['manual_required'])
    def test_widened_sl_is_not_support_reentry(self):
        p=self.start();p['sl']=str(D(p['sl'])-D('.2'));self.tick()
        self.b.close('SL');self.tick(10)
        self.assertFalse(any(link['kind']=='support' for link in self.bot.state['cycle']['overlap_ladders'].values()))
    def test_pair_common_cancel_is_exposed_in_view(self):
        self.support_pair()
        self.assertTrue(self.bot.v3_view(self.bot.snapshot())['v3_controls']['can_cancel_entry'])
