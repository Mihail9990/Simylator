"""Synthetic PAPER candle recovery contracts; no terminal/network/trading."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from tradfi.candle_recovery import CandleRecoveryController, profile_defaults
from tradfi.broker import Spec
from tradfi.interactive_simulator import InteractiveSimulator
from tradfi.simulator import ARTIFICIAL_SPEC
from tradfi.storage import Store

class CandleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'paper.db');self.addCleanup(self.store.close)
        self.b=InteractiveSimulator(dict(ARTIFICIAL_SPEC,volume_min='.01',volume_step='.01',contract_size='100'))
        self.b.data.update(now=90,candle=dict(id=0,open='3999',close='4000'))
        self.bot=CandleRecoveryController(self.store,self.b)
        self.bot.state['basket_settings'].update(INITIAL_SL_DISTANCE='1',INITIAL_TP_DISTANCE='2')
        self.bot.save('fixture')
    def tick(self,n=4):
        for _ in range(n):self.bot.tick()
    def bar(self,n=60,side='BUY',close='4000'):
        self.b.data.update(now=n+60,candle=dict(id=n,open=str(D(close)+(-1 if side=='BUY' else 1)),close=close))
    def start(self,side='BUY'):
        self.bot.command('/start');self.bar(side=side);self.tick()
        self.assertEqual(len(self.b.data['orders']),1)
    def fill(self,side='BUY'):
        for o in list(self.b.data['orders']):
            if o['side']==side:self.b.fill(side)
        p=self.b.data['positions'][0];e=D(p['entry']);self.b.quote(e-D('.05'),e+D('.05'));self.tick()
    def loss(self):
        self.start();self.fill();self.b.close('SL');self.tick()
    def recovery(self):
        self.loss();self.b.quote('4000','4000.1');self.bar(120);self.tick();self.fill()
    def test_default_profiles_and_initial_volume(self):
        self.assertEqual(D(self.bot.state['basket_settings']['INITIAL_SIZE']),D('.02'))
        self.assertEqual([len(p['stages']) for p in profile_defaults()['profiles']],list(range(2,11)))
    def test_full_sl_follows_new_candle_and_increments_once(self):
        self.loss();c=self.bot.state['cycle'];self.assertEqual(D(c['follow_volume']),D('.03'))
        self.assertFalse(self.b.data['orders']);self.tick(8);self.assertEqual(D(c['follow_volume']),D('.03'))
        self.b.quote('4000','4000.1');self.bar(120);self.tick();self.assertEqual(len(self.b.data['orders']),3)
        self.assertEqual({D(o['size']) for o in self.b.data['orders']},{D('.01')})
        self.bar(180,'SELL');self.tick(8);self.assertEqual({o['side'] for o in self.b.data['orders']},{'SELL'})
        self.assertEqual(len(self.b.data['orders']),3)
    def test_support_sl_keeps_volume_even_if_moved_away(self):
        self.start();self.fill();p=self.b.data['positions'][0];p['sl']=str(D(p['sl'])-D('.5'));self.tick()
        self.b.close('SL');self.tick();self.assertEqual(D(self.bot.state['cycle']['follow_volume']),D('.02'))
    def test_support_move_and_close_between_polls(self):
        self.start();self.fill();p=self.b.data['positions'][0];p['sl']=str(D(p['entry'])-D('.25'))
        self.b.close('SL');self.tick();self.assertEqual(D(self.bot.state['cycle']['follow_volume']),D('.02'))
    def test_bid_ask_thresholds_and_no_worsening(self):
        for side in ('BUY','SELL'):
            if side=='SELL':self.tearDown();self.doCleanups();self.setUp()
            self.start(side);self.fill(side);p=self.b.data['positions'][0];e=D(p['entry']);s=1 if side=='BUY' else -1
            # Wrong quote side crossing must not trigger.
            self.b.quote(e+D('.20') if s==1 else e-D('.35'),e+D('.35') if s==1 else e-D('.20'));self.tick()
            self.assertEqual(D(p['sl']),e-s)
            price=e+s*D('.25');self.b.quote(price if s==1 else price-D('.1'),price+D('.1') if s==1 else price);self.tick()
            self.assertEqual(D(p['sl']),e-s*D('.25'))
            price=e+s*D('.40');self.b.quote(price if s==1 else price-D('.1'),price+D('.1') if s==1 else price);self.tick()
            self.assertEqual(D(p['sl']),e)
            p['sl']=str(e+s*D('.1'));self.tick();self.assertEqual(D(p['sl']),e+s*D('.1'))
    def test_mixed_close_keeps_volume(self):
        self.recovery();self.b.close('TP',position_id=self.b.data['positions'][0]['id'])
        for p in list(self.b.data['positions']):self.b.close('SL',position_id=p['id'])
        self.tick();self.assertEqual(D(self.bot.state['cycle']['follow_volume']),D('.03'))
    def test_full_recovery_sl_next_four_and_restart_no_increment(self):
        self.recovery()
        for p in list(self.b.data['positions']):self.b.close('SL',position_id=p['id'])
        self.tick();self.assertEqual(D(self.bot.state['cycle']['follow_volume']),D('.04'))
        self.bot=CandleRecoveryController(self.store,self.b);self.tick();self.assertEqual(D(self.bot.state['cycle']['follow_volume']),D('.04'))
    def test_last_target_subtracts_other_projected_profit(self):
        self.loss();s=Spec.load(self.b.data['spec'])
        tickets=[dict(entry='4000',size='.01',tp='4000.30',index=0),dict(entry='4000',size='.01',tp='4000.70',index=1),dict(entry='4000',size='.01',tp='4001',index=2)]
        self.assertEqual(D(self.bot._last_target(s,'BUY',tickets,D('2'))),D('4001'))
        self.assertEqual(D(self.bot._last_target(s,'BUY',tickets[:2],D('2'))),D('4001.70'))
        self.assertIsNone(self.bot._last_target(s,'BUY',tickets,D('.5')))
    def test_fixed_link_does_not_follow_moved_sl(self):
        self.start();self.fill();p=self.b.data['positions'][0]
        self.bot.action(dict(type='v3_link',cycle_id=self.bot.state['cycle']['id'],expected_positions=[p['id']]))
        self.tick();before=deepcopy(self.b.data['orders']);self.assertTrue(before)
        p['sl']=str(D(p['sl'])+D('.1'));self.tick()
        self.assertEqual(self.b.data['orders'],before)
    def test_manual_market_entry_removed(self):
        from tradfi.control import ActionRejected
        with self.assertRaises(ActionRejected):self.bot.action(dict(type='v3_entry'))

    def test_fixed_link_survives_explicit_sl_control(self):
        self.start();self.fill();p=self.b.data['positions'][0];c=self.bot.state['cycle']
        self.bot.action(dict(type='v3_link',cycle_id=c['id'],expected_positions=[p['id']]))
        self.tick();before=deepcopy(self.b.data['orders'])
        self.bot.action(dict(type='v3_adjust_sl',cycle_id=c['id'],distance='1.10',expected_positions=[p['id']]))
        self.tick(8)
        self.assertEqual(self.b.data['orders'],before)
        self.assertEqual(D(p['sl']),D(p['entry'])-D('1.10'))
    def test_max_volume_full_sl_pauses_and_explicit_profile_resumes(self):
        self.loss();c=self.bot.state['cycle'];c['follow_volume']='.10';self.bot.save('max_fixture')
        self.b.quote('4000','4000.1');self.bar(120);self.tick();self.fill()
        self.assertEqual(len(self.b.data['positions']),10)
        for p in list(self.b.data['positions']):self.b.close('SL',position_id=p['id'])
        self.tick();self.assertTrue(c['manual_required']);self.b.quote('4000','4000.1');self.bar(180);self.tick()
        self.assertFalse(self.b.data['orders'])
        self.bot.action(dict(type='candle_recovery_resume',cycle_id=c['id'],volume='.04',profile_id='volume_4'))
        self.tick();self.assertFalse(self.b.data['orders']);self.bar(240);self.tick()
        self.assertEqual(len(self.b.data['orders']),4)
    def test_last_remaining_rung_adjusts_after_last_index_closed(self):
        self.recovery();c=self.bot.state['cycle'];group=c['groups'][-1]
        tickets=sorted([t for t in c['tickets'].values() if t['group']==group['id']],key=lambda t:t['index'])
        self.b.close('TP',position_id=tickets[-1]['id']);self.tick()
        remaining=sorted(self.b.data['positions'],key=lambda p:c['tickets'][p['id']]['index'])
        projected=sum((self.b.profit(p['side'],D(p['size']),D(p['entry']),D(p['tp'])) for p in remaining),D(0))
        self.assertGreaterEqual(projected,D(c['recovery']))
        self.assertEqual(len(remaining),2)
    def test_cancel_recovery_waits_for_next_candle(self):
        self.loss();self.b.quote('4000','4000.1');self.bar(120);self.tick()
        self.bar(180);self.tick();c=self.bot.state['cycle']
        self.bot.action(dict(type='v3_cancel_entry',cycle_id=c['id']));self.tick(10)
        self.assertFalse(self.b.data['orders'])
        self.bar(240);self.tick();self.assertEqual(len(self.b.data['orders']),3)
    def test_projected_close_fees_not_open_fees_counted(self):
        self.loss();self.b._fee=lambda size:-D(size)*D('10')
        s=Spec.load(self.b.data['spec']);tickets=[dict(entry='4000',size='.01',tp='4000.30'),dict(entry='4000',size='.01',tp='4000.70'),dict(entry='4000',size='.01',tp='4001')]
        self.assertEqual(D(self.bot._last_target(s,'BUY',tickets,D('2'))),D('4001.30'))
    def test_invalid_tp_adjustment_does_not_block_valid_sl(self):
        self.recovery();c=self.bot.state['cycle'];c['recovery']='1.10';c['realized']='-1.10'
        # Existing final TP is distant; revised 0.10 TP is already crossed.
        p=self.b.data['positions'][-1];e=D(p['entry']);self.b.quote(e+D('.25'),e+D('.35'))
        self.bot._manage_positions(self.bot.snapshot());self.bot.dispatch();self.tick()
        self.assertEqual(D(p['sl']),e-D('.25'))
    def test_fixed_link_survives_graph_sl_drag(self):
        self.start();self.fill();p=self.b.data['positions'][0];c=self.bot.state['cycle']
        self.bot.action(dict(type='v3_link',cycle_id=c['id'],expected_positions=[p['id']]))
        self.tick();before=deepcopy(self.b.data['orders'])
        self.bot.edit(protection=dict(position_id=p['id'],old=dict(sl=p['sl'],tp=p['tp']),new=dict(sl=str(D(p['sl'])-D('.1')),tp=p['tp'])))
        self.tick(8);self.assertEqual(self.b.data['orders'],before)
        self.assertEqual(D(p['sl']),D(p['entry'])-D('1.10'))
    def test_close_all_stages_intents_without_next_market_tick(self):
        self.start();self.fill();c=self.bot.state['cycle']
        self.bot.action(dict(type='candle_close_all',cycle_id=c['id']))
        intents=[cmd for cmd in self.bot.state['commands'].values() if cmd['kind']=='CLOSE']
        self.assertEqual(len(intents),1);self.assertTrue(self.bot.state['stop'])
        self.bot.dispatch();self.assertFalse(self.b.data['positions'])
        self.tick();self.assertGreater(D(c['recovery']),0)
    def test_reverse_ladder_uses_confirmed_tp1_and_threshold_sl(self):
        self.recovery();c=self.bot.state['cycle'];g=c['groups'][-1]
        self.assertFalse(self.bot.reverse_controls(self.bot.snapshot())['can_enable'])
        first=min(self.b.data['positions'],key=lambda p:c['tickets'][p['id']]['index'])
        self.b.close('TP',position_id=first['id']);self.tick()
        # TP1 at .30 alone has only the first (below entry) SL support stage.
        self.assertFalse(self.bot.reverse_controls(self.bot.snapshot())['can_enable'])
        e=D(self.b.data['positions'][0]['entry']);self.b.quote(e+D('.40'),e+D('.50'));self.tick()
        self.assertNotIn('entry_sl_once',g)
        self.assertTrue(self.bot.reverse_controls(self.bot.snapshot())['can_enable'])
        expected=sorted(p['id'] for p in self.b.data['positions'])
        self.bot.action(dict(type='reverse_ladder',cycle_id=c['id'],enabled=True,expected_positions=expected));self.tick()
        self.assertTrue(g['reverse_ladder']['enabled']);self.assertTrue(self.bot.reverse_controls(self.bot.snapshot())['can_disable'])
        self.bot.action(dict(type='reverse_ladder',cycle_id=c['id'],enabled=False,expected_positions=expected));self.tick()
        self.assertFalse(g['reverse_ladder']['enabled'])
        self.assertEqual({D(p['sl']) for p in self.b.data['positions']},{e})
    def test_reverse_gate_requires_actual_not_intended_sl(self):
        self.recovery();c=self.bot.state['cycle'];g=c['groups'][-1]
        first=min(self.b.data['positions'],key=lambda p:c['tickets'][p['id']]['index'])
        self.b.close('TP',position_id=first['id']);self.tick()
        for t in c['tickets'].values():
            if not t['closed']:t['sl']=t['entry']
        self.assertFalse(self.bot._tp1_reverse_confirmed(c,g))
    def test_pending_profile_survives_opposite_candle_replacement(self):
        self.loss();self.b.quote('4000','4000.1');self.bar(120);self.tick();c=self.bot.state['cycle']
        frozen=deepcopy(c['groups'][-1]['recovery_profile'])
        updated=deepcopy(self.bot.state['recovery_profiles'])
        next(p for p in updated['profiles'] if p['id']=='volume_3')['stages'][0]['tp_distance']='.50'
        self.bot.action(dict(type='recovery_profiles',cycle_id=c['id'],**updated))
        self.bar(180,'SELL');self.tick(8)
        self.assertEqual(c['groups'][-1]['recovery_profile'],frozen)
        self.assertEqual(c['follow_profile'],frozen)
    def _four_ladder(self):
        self.loss();c=self.bot.state['cycle'];c['follow_volume']='.04';self.bot.save('four_fixture')
        self.b.quote('4000','4000.1');self.bar(120);self.tick();self.fill();return c
    def test_fixed_link_predicts_next_full_sl_volume(self):
        c=self._four_ladder();self.bot.action(dict(type='v3_link',cycle_id=c['id'],expected_positions=sorted(p['id'] for p in self.b.data['positions'])))
        self.tick();self.assertEqual(len(self.b.data['orders']),5)
    def test_fixed_link_support_keeps_volume_after_tp1(self):
        c=self._four_ladder();first=min(self.b.data['positions'],key=lambda p:c['tickets'][p['id']]['index'])
        self.b.close('TP',position_id=first['id']);self.tick();e=D(self.b.data['positions'][0]['entry'])
        self.b.quote(e+D('.55'),e+D('.65'));self.tick()
        self.assertTrue(self.bot.v3_view(self.bot.snapshot())['v3_controls']['can_link'])
        self.bot.action(dict(type='v3_link',cycle_id=c['id'],expected_positions=sorted(p['id'] for p in self.b.data['positions'])))
        self.tick();self.assertEqual(len(self.b.data['orders']),4)
    def test_fixed_link_support_without_tp_keeps_volume(self):
        c=self._four_ladder();e=D(self.b.data['positions'][0]['entry'])
        self.b.quote(e+D('.25'),e+D('.35'));self.tick()
        self.bot.action(dict(type='v3_link',cycle_id=c['id'],expected_positions=sorted(p['id'] for p in self.b.data['positions'])))
        self.tick();self.assertEqual(len(self.b.data['orders']),4)
    def test_fixed_link_rejects_unequal_sl(self):
        from tradfi.control import ActionRejected
        c=self._four_ladder();p=self.b.data['positions'][0];p['sl']=str(D(p['sl'])+D('.1'));self.tick()
        with self.assertRaises(ActionRejected):self.bot.action(dict(type='v3_link',cycle_id=c['id'],expected_positions=sorted(p['id'] for p in self.b.data['positions'])))
        self.assertFalse(self.b.data['orders'])
    def test_manual_last_tp_retained_and_next_remaining_marker_used(self):
        self.recovery();c=self.bot.state['cycle'];positions=sorted(self.b.data['positions'],key=lambda p:c['tickets'][p['id']]['index'])
        peers=deepcopy([p['tp'] for p in positions[:-1]])
        last=positions[-1];new_tp=str(D(last['entry'])+D('3'))
        self.bot.edit(protection=dict(position_id=last['id'],old=dict(sl=last['sl'],tp=last['tp']),new=dict(sl=last['sl'],tp=new_tp)));self.tick(8)
        self.assertEqual(last['tp'],new_tp);self.assertEqual([p['tp'] for p in positions[:-1]],peers)
        prior=positions[-2];prior_tp=str(D(prior['entry'])+D('4'))
        self.bot.edit(protection=dict(position_id=prior['id'],old=dict(sl=prior['sl'],tp=prior['tp']),new=dict(sl=prior['sl'],tp=prior_tp)));self.tick()
        self.b.close('TP',position_id=last['id'],profit='0.10');self.b.quote(D(prior['entry'])-D('.05'),D(prior['entry'])+D('.05'));self.tick(8)
        self.assertEqual(prior['tp'],prior_tp)
