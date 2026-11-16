"""Immediate fill/close losses resume candles, never fabricate reverse fills."""
from copy import deepcopy
from decimal import Decimal as D
import unittest
import test_candle_paper_session as fixtures
from tradfi.paper import PaperSession

class GapFollowTests(unittest.TestCase):
    setUp = fixtures.CandlePaperSession.setUp
    step = fixtures.CandlePaperSession.step
    def gap_initial(self, side='SELL', carry='0'):
        self.bot.state['basket_settings']['INITIAL_TP_DISTANCE']='.25'
        self.bot.state['basket_carry']=carry
        self.bot.save('gap_fixture')
        self.session.action(dict(type='command',text='/start'))
        self.reader.tick('4000','4000.1',seconds=30,
                         bar=('4001','4000') if side=='SELL' else ('3999','4000'))
        self.step()
        order=deepcopy(self.session.exchange.p['orders'][0])
        level=D(order['price'])
        if side=='SELL': self.reader.tick(level-D('.74'),level-D('.64'))
        else: self.reader.tick(level+D('.64'),level+D('.74'))
        self.step()
        self.assertFalse(self.session.exchange.p['positions'])
        self.assertFalse(self.session.exchange.p['orders'])
        self.assertEqual(self.session.exchange.p['deals'][-1]['reason'],'TP')
        self.assertEqual(D(self.session.exchange.p['deals'][-1]['profit']),D('-.98'))
        return order

    def new_bar(self, side='BUY'):
        self.reader.tick('4000','4000.1',seconds=60,
                         bar=('3999','4000') if side=='BUY' else ('4001','4000'))
        self.step(8)

    def check_initial_gap(self, side):
        self.gap_initial(side)
        c=self.bot.state['cycle']
        self.assertEqual(D(c['recovery']),D('.98'))
        self.assertTrue(c['recovery_follow']);self.assertFalse(c['manual_required'])
        self.new_bar('BUY')
        orders=self.session.exchange.p['orders']
        self.assertEqual(len(orders),3)
        self.assertEqual({o['price'] for o in orders},{'4000.80'})
        self.assertEqual({o['side'] for o in orders},{'BUY'})

    def test_sell_gap_loss_waits_next_candle_then_profile_from_actual_debt(self):
        self.check_initial_gap('SELL')

    def test_buy_gap_loss_waits_next_candle_then_profile_from_actual_debt(self):
        self.check_initial_gap('BUY')

    def test_existing_persisted_manual_gap_is_recovered_after_update(self):
        self.gap_initial()
        c=self.bot.state['cycle']
        c['groups'][0].pop('gap_follow_started')
        for key in ('recovery_follow','follow_profile','follow_after'): c.pop(key,None)
        c['manual_required']=True
        self.bot.state['phase']='V3_WAIT_ENTRY'
        self.bot.save('old_release_manual_gap')
        cls=type(self.bot)
        self.session.controller=cls(self.session.store,self.session.exchange,clock=lambda:self.reader.now)
        self.bot=self.session.controller
        self.step()
        self.assertTrue(self.bot.state['cycle']['recovery_follow'])
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('.98'))
        self.new_bar()
        self.assertEqual(len(self.session.exchange.p['orders']),3)

    def test_actual_debt_profile_not_initial_volume(self):
        self.gap_initial(carry='3')
        self.new_bar()
        self.assertEqual(len(self.session.exchange.p['orders']),4)

    def test_over_limit_stays_manual(self):
        self.gap_initial(carry='50')
        self.new_bar()
        self.assertFalse(self.session.exchange.p['orders'])
        self.assertEqual(self.bot.state['phase'],'V3_WAIT_ENTRY')

    def test_restart_no_duplicate_and_direction_changes(self):
        self.gap_initial();self.new_bar()
        old={o['id'] for o in self.session.exchange.p['orders']}
        # Controller reload is sufficient to exercise persisted continuation.
        cls=type(self.bot)
        self.session.controller=cls(self.session.store,self.session.exchange,clock=lambda:self.reader.now)
        self.bot=self.session.controller
        self.step(8)
        self.assertEqual({o['id'] for o in self.session.exchange.p['orders']},old)
        self.new_bar('SELL')
        self.assertEqual(len(self.session.exchange.p['orders']),3)
        self.assertEqual({o['side'] for o in self.session.exchange.p['orders']},{'SELL'})

    def test_explicit_cancel_does_not_recreate(self):
        self.gap_initial();self.new_bar()
        self.session.action(dict(type='v3_cancel_entry',cycle_id=self.bot.state['cycle']['id']))
        self.step(8);self.new_bar('SELL');self.new_bar()
        self.assertFalse(self.session.exchange.p['orders'])
        self.assertFalse(self.bot.state['cycle'].get('recovery_follow'))

    def test_explicit_stop_is_preserved(self):
        self.gap_initial()
        self.session.action(dict(type='command',text='/stop'))
        self.new_bar()
        self.assertFalse(self.session.exchange.p['orders'])
        self.assertTrue(self.bot.state['stop'])

    def test_exactly_fifty_allowed_and_custom_distance(self):
        self.bot.state['v3_settings']['entry_distance']='1.20'
        self.gap_initial(carry='49.02')
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('50'))
        self.new_bar()
        self.assertEqual(len(self.session.exchange.p['orders']),10)
        self.assertEqual({D(o['price']) for o in self.session.exchange.p['orders']},{D('4001.20')})

    def test_commission_is_included_when_choosing_profile(self):
        # .02 initial position: .02 fee on each side shifts3.48 to3.52.
        self.session.exchange.p['commission_per_lot_side']='1'
        self.session.exchange.persist()
        self.gap_initial(carry='2.50')
        self.assertEqual(D(self.bot.state['cycle']['recovery']),D('3.52'))
        self.new_bar()
        self.assertEqual(len(self.session.exchange.p['orders']),4)

    def test_previously_disabled_reverse_does_not_follow(self):
        fixtures.CandlePaperSession.start(self)
        c=self.bot.state['cycle']
        self.session.action(dict(type='v3_cancel_link',cycle_id=c['id']))
        self.step()
        c=self.bot.state['cycle']
        self.assertTrue(any(link.get('disabled') for link in c['overlap_ladders'].values()))
        position=self.session.exchange.p['positions'][0]
        sl=D(position['sl'])
        self.reader.tick(str(sl-D('.01')),str(sl+D('.09')))
        self.step()
        self.new_bar()
        self.assertFalse(self.session.exchange.p['orders'])
        self.assertEqual(self.bot.state['phase'],'V3_WAIT_ENTRY')
        self.assertFalse(c.get('recovery_follow'))

    def test_immediate_profitable_tp_finishes_without_fallback(self):
        # BUY fills beyond trigger but below its TP; same sample Bid reachesTP.
        # Zero spread is permitted in synthetic exchange; no loss remains.
        self.bot.state['basket_settings']['INITIAL_TP_DISTANCE']='.25'
        self.bot.save('positive_gap_fixture')
        self.session.action(dict(type='command',text='/start'))
        self.reader.tick('4000','4000.1',seconds=30,bar=('3999','4000'))
        self.step()
        tp=self.session.exchange.p['orders'][0]['tp']
        self.reader.tick(tp,tp)
        self.step(8)
        self.assertIsNone(self.bot.state['cycle'])
        self.assertFalse(self.session.exchange.p['orders'])
