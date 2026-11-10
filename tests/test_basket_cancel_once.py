"""One-shot candle pending cancellation, with actual controller and fake broker only."""
import unittest
import test_tradfi_basket as fixtures
from tradfi.control import ActionGateway,ActionRejected

class CancelOnce(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.BasketTest();self.f.setUp();self.addCleanup(self.f.doCleanups)
    def action(self):
        return dict(type='cancel_initial',cycle_id=self.f.bot.state['cycle']['id'],expected_orders=[o['id'] for o in self.f.b.data['orders']])
    def test_same_and_opposite_next_candle(self):
        for side in ('BUY','SELL'):
            with self.subTest(side=side):
                f=fixtures.BasketTest();f.setUp()
                try:
                    f.start();f.bot.action(dict(type='cancel_initial',cycle_id=f.bot.state['cycle']['id'],expected_orders=[o['id'] for o in f.b.data['orders']]))
                    f.tick(4);self.assertFalse(f.b.data['orders'])
                    f.bar(cid=120,side=side);f.tick(4)
                    self.assertEqual([o['side'] for o in f.b.data['orders']],[side])
                finally:f.doCleanups()
    def test_sell_initial(self):
        f=self.f;f.start('SELL');f.bot.action(self.action());f.tick(4);self.assertFalse(f.b.data['orders'])
        f.bar(cid=120,side='SELL');f.tick(4);self.assertEqual(len(f.b.data['orders']),1)
    def test_restart_pending_cancel_then_doji(self):
        f=self.f;f.start();f.bot.action(self.action());f.restart();f.tick(4)
        f.b.data.update(now=180,candle=dict(id=120,open='4000',close='4000'));f.tick(4);self.assertFalse(f.b.data['orders'])
        f.bar(cid=180);f.tick(4);self.assertEqual(len(f.b.data['orders']),1)
    def test_fill_before_action_rejected_without_cancelling(self):
        f=self.f;f.start();a=self.action();f.b.fill('BUY')
        with self.assertRaises(ActionRejected):f.bot.action(a)
        f.tick(4);self.assertEqual(len(f.b.data['positions']),1)
    def test_fill_during_cancel_wins(self):
        f=self.f;f.start();f.bot.action(self.action());f.b.cancel_race='BUY';f.tick(5)
        self.assertEqual(len(f.b.data['positions']),1)
        self.assertEqual(f.bot.state['phase'],'INITIAL_POSITION')
        self.assertNotIn('initial_cancel',f.bot.state['cycle'])
    def test_unknown_cancel_never_rearms(self):
        f=self.f;f.start();f.bot.action(self.action());f.b.fail_before.add('CANCEL');f.tick(3)
        f.bar(cid=120,side='SELL');f.tick(3);f.restart();f.tick(3)
        self.assertEqual(len(f.b.data['orders']),1);self.assertEqual(f.b.data['orders'][0]['side'],'BUY')
    def test_stale_cycle_or_orders_reject(self):
        f=self.f;f.start();a=self.action();a['cycle_id']='stale'
        with self.assertRaises(ActionRejected):f.bot.action(a)
        a=self.action();a['expected_orders']=['stale']
        with self.assertRaises(ActionRejected):f.bot.action(a)
        self.assertNotIn('initial_cancel',f.bot.state['cycle'])
    def test_duplicate_receipt(self):
        f=self.f;f.start();g=ActionGateway(f.bot,execute=f.bot.action)
        request=dict(id='cancel-once-001',revision=0,action=self.action())
        r=g.apply(request);self.assertEqual(r['status'],'APPLIED');self.assertEqual(g.apply(request),r)
        f.tick(4);f.bar(cid=120);f.tick(4);self.assertEqual(len(f.b.data['orders']),1)
    def test_readonly(self):
        f=self.f;f.start();f.b.read_only=True
        with self.assertRaises(ActionRejected):f.bot.action(self.action())
        self.assertNotIn('initial_cancel',f.bot.state['cycle'])
    def test_recovery_reject(self):
        f=self.f;f.initial()
        with self.assertRaises(ActionRejected):f.bot.action(self.action())
        self.assertEqual(len(f.b.data['orders']),6)

