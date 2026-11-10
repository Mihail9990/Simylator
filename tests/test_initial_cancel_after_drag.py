"""Initial-trigger drag/cancel regression: deterministic fake broker, no network."""
import unittest
from decimal import Decimal

import test_recovery_v3 as fixtures
from tradfi.control import ActionRejected
from tradfi.recovery_v3 import ControlledRecoveryController


class InitialCancelAfterDrag(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.V3()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.f.start()
        self.old_id = self.f.b.data['orders'][0]['id']
        self.f.bot.action(self.fixture.a(
            'basket_levels', levels={'BUY': '4002'},
            expected=self.f.bot.levels(), expected_orders=[self.old_id]))
        self.f.tick(5)
        self.new_id = self.f.b.data['orders'][0]['id']
        self.assertNotEqual(self.old_id, self.new_id)
        self.assertEqual(Decimal(self.f.b.data['orders'][0]['price']), Decimal('4002'))

    def cancel(self):
        self.f.bot.action(self.fixture.a('cancel_initial', expected_orders=[self.new_id]))

    def test_cancels_current_ticket_and_waits_for_next_directional_close(self):
        self.cancel()
        self.f.tick(5)
        self.assertFalse(self.f.b.data['orders'])
        self.assertFalse(self.f.b.data['positions'])
        self.assertEqual(self.f.bot.state['phase'], 'INITIAL_WAIT')
        self.assertNotIn('signal_override', self.f.bot.state['cycle'])
        self.f.bar(120, 'SELL')
        self.f.tick(4)
        self.assertEqual([(o['side'], Decimal(o['price'])) for o in self.f.b.data['orders']],
                         [('SELL', Decimal('3999'))])

    def test_stale_ticket_cannot_cancel_replacement(self):
        with self.assertRaises(ActionRejected):
            self.f.bot.action(self.fixture.a('cancel_initial', expected_orders=[self.old_id]))
        self.assertEqual(self.f.b.data['orders'][0]['id'], self.new_id)

    def test_fill_before_cancel_preserves_position(self):
        self.f.b.fill('BUY')
        with self.assertRaises(ActionRejected):
            self.cancel()
        self.f.tick(4)
        self.assertEqual(len(self.f.b.data['positions']), 1)
        self.assertEqual(self.f.bot.state['phase'], 'INITIAL_POSITION')

    def test_fill_during_cancel_wins_without_opposite_order(self):
        self.cancel()
        self.f.b.cancel_race = 'BUY'
        self.f.tick(5)
        self.assertEqual(len(self.f.b.data['positions']), 1)
        self.assertFalse(self.f.b.data['orders'])
        self.assertEqual(self.f.bot.state['phase'], 'INITIAL_POSITION')
        self.assertNotIn('initial_cancel', self.f.bot.state['cycle'])

    def test_unknown_cancel_never_rearms_after_new_bar_and_restart(self):
        self.cancel()
        self.f.b.fail_before.add('CANCEL')
        self.f.tick(3)
        command_count = len(self.f.b.commands)
        self.f.bar(120, 'SELL')
        self.f.tick(3)
        self.f.bot = ControlledRecoveryController(self.f.store, self.f.b)
        self.f.tick(3)
        self.assertEqual(len(self.f.b.commands), command_count)
        self.assertEqual([o['id'] for o in self.f.b.data['orders']], [self.new_id])
        self.assertFalse(self.f.b.data['positions'])

    def test_cancel_rejected_while_another_drag_is_unfinished(self):
        self.f.bot.action(self.fixture.a(
            'basket_levels', levels={'BUY': '4003'},
            expected=self.f.bot.levels(), expected_orders=[self.new_id]))
        with self.assertRaises(ActionRejected):
            self.cancel()


if __name__ == '__main__':
    unittest.main()
