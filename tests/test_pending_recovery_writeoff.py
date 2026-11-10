"""Flat-only waiver preserves orders and plans; injected/fake brokers only."""
from copy import deepcopy
from decimal import Decimal as D
from unittest.mock import patch
import unittest
import test_v3_manual_trigger_cancel as fixtures
from tradfi.control import ActionRejected,ActionGateway
from tradfi.recovery_v3 import ControlledRecoveryController

class PendingWriteoff(unittest.TestCase):
    def setUp(self):
        self.x=fixtures.ManualTriggerCancel();self.x.setUp();self.addCleanup(self.x.doCleanups);self.f=self.x.f
    def action(self):
        c=self.f.bot.state['cycle'];return dict(type='writeoff_recovery',cycle_id=c['id'],expected_debt=c['recovery'],confirmed=True)
    def test_pending_orders_repeated_receipt_restart_and_new_loss(self):
        self.x.pending();f=self.f;c=f.bot.state['cycle'];cid=c['id'];realized=c['realized'];debt=D(c['recovery']);orders=deepcopy(f.b.data['orders']);sends=deepcopy(f.b.commands)
        self.assertTrue(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_writeoff_recovery'])
        gate=ActionGateway(f.bot,execute=f.bot.action);req=dict(id='waive-pending',revision=f.bot.state.get('control_revision',0),action=self.action())
        self.assertEqual(gate.apply(req)['status'],'APPLIED');self.assertEqual(gate.apply(req)['status'],'APPLIED')
        self.assertEqual(f.b.commands,sends);self.assertEqual(f.b.data['orders'],orders);self.assertEqual(c['realized'],realized);self.assertEqual(c['recovery'],'0')
        f.bot=ControlledRecoveryController(f.store,f.b);f.tick(4)
        self.assertEqual(f.bot.state['cycle']['id'],cid);self.assertEqual(f.b.data['orders'],orders)
        self.assertEqual(D(f.bot.state['cycle']['recovery_waived']),debt)
        for _ in range(3):f.b.fill('SELL')
        entry=D(f.b.data['positions'][0]['entry']);f.b.quote(entry-D('.05'),entry+D('.05'));f.tick(4)
        for p in list(f.b.data['positions']):f.b.close('SL',position_id=p['id'])
        f.tick(5);c=f.bot.state['cycle'];self.assertEqual(D(c['recovery']),D('4.50'));self.assertEqual(D(c['realized']),D(realized)-D('4.50'))
    def test_flat_active_wait_can_reset_without_stop(self):
        f=self.f;self.assertFalse(f.bot.state['stop']);sends=deepcopy(f.b.commands)
        self.assertTrue(f.bot.v3_view(f.bot.snapshot())['v3_controls']['can_writeoff_recovery'])
        f.bot.action(self.action());self.assertEqual(f.b.commands,sends);self.assertEqual(f.bot.state['phase'],'WAIT_CANDLE')
        self.assertIsNone(f.bot.state['cycle']);self.assertEqual(f.bot.state['basket_carry'],'0')
    def test_scheduled_candle_preserved_then_executes(self):
        f=self.f;self.x.x.entry('STOP','next',count=3);c=f.bot.state['cycle'];request=deepcopy(c['v3_request']);cid=c['id']
        f.bot.action(self.action());self.assertEqual(c['v3_request'],request);f.tick(2);self.assertEqual(f.bot.state['cycle']['id'],cid)
        target=request['bar_id'];f.b.data.update(now=target+60,candle=dict(id=target,open='4001',close='4000'));f.b.quote('3999.7','3999.8');f.tick(3)
        self.assertEqual(len(f.b.data['orders']),3);self.assertEqual(c['recovery'],'0')
    def test_positions_and_unknown_cancel_reject_without_writeoff(self):
        self.x.pending();f=self.f;f.b.fill('SELL')
        with self.assertRaises(ActionRejected):f.bot.action(self.action())
        self.assertNotIn('recovery_waived',f.bot.state['cycle'])
    def test_fill_between_two_snapshots_rejects(self):
        self.x.pending();f=self.f;snapshot=f.bot.snapshot;count=0
        def race():
            nonlocal count
            count+=1;snap=snapshot()
            if count==1:f.b.fill('SELL')
            return snap
        with patch.object(f.bot,'snapshot',side_effect=race):
            with self.assertRaises(ActionRejected):f.bot.action(self.action())
        self.assertNotIn('recovery_waived',f.bot.state['cycle']);self.assertEqual(len(f.b.data['positions']),1)
    def test_unknown_control_and_readonly_reject(self):
        self.x.pending();f=self.f
        f.bot.state.setdefault('control_receipts',{})['old']={'status':'UNKNOWN'}
        with self.assertRaises(ActionRejected):f.bot.action(self.action())
        f.bot.state['control_receipts'].clear();f.b.read_only=True
        with self.assertRaises(ActionRejected):f.bot.action(self.action())
        self.assertNotIn('recovery_waived',f.bot.state['cycle'])


class PaperWaiverSettlement(unittest.TestCase):
    def setUp(self):
        from test_paper_interrupt import PaperInterruptTest
        self.x=PaperInterruptTest();self.x.setUp();self.addCleanup(self.x.doCleanups);self.addCleanup(self.x.tearDown)
        self.x.initial();self.x.reader.tick('3998','3998.1');self.x.step(4)
        self.session=self.x.session;self.c=self.session.controller.state['cycle']
        self.x.action(dict(type='v3_entry',cycle_id=self.c['id'],order_type='STOP',timing='next',side='SELL'))
    def waive(self):
        self.x.action(dict(type='writeoff_recovery',cycle_id=self.c['id'],expected_debt=self.c['recovery'],confirmed=True))
    def test_real_paper_waiver_then_interrupt_does_not_restore_old_debt(self):
        before=deepcopy(self.session.view()['metrics']);deals=deepcopy(self.x.p['deals']);request=deepcopy(self.c['v3_request'])
        self.waive();self.assertEqual(self.c['recovery'],'0');self.assertEqual(self.c['v3_request'],request);self.assertEqual(self.x.p['deals'],deals)
        self.assertEqual(self.session.view()['metrics']['all_closed_net'],before['all_closed_net'])
        self.x.action(self.x.request());self.assertEqual(self.session.controller.state['basket_carry'],'0')
        self.assertEqual(self.session.view()['metrics']['all_closed_net'],before['all_closed_net'])
    def test_real_paper_manual_reconciliation_does_not_restore_waiver(self):
        self.waive();self.x.action(dict(type='v3_cancel_request',cycle_id=self.c['id']))
        self.c['manual']=True;self.session.controller.state['phase']='MANUAL';self.session.controller.save('test_manual_boundary')
        self.x.action(dict(type='command',text='/reset_manual'))
        self.assertEqual(self.session.controller.state['basket_carry'],'0')
