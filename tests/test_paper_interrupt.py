"""PAPER cycle interruption, accounting and all-or-nothing crash boundaries."""
from copy import deepcopy
from decimal import Decimal as D
import json
import unittest
from unittest.mock import patch

from test_tradfi_paper import PaperTest
from tradfi.control import ActionRejected, Conflict
from tradfi.storage import DurabilityError


class PaperInterruptTest(PaperTest):
    primary_v3 = True
    # Reuse helpers, not the inherited legacy strategy tests.
    def request(self):
        return dict(type='paper_interrupt', confirmed=True, gap_confirmed=bool(self.p['paused_gap']),
                    cycle_id=self.session.controller.state['cycle']['id'],
                    expected_positions=sorted(p['id'] for p in self.p['positions']),
                    expected_orders=sorted(o['id'] for o in self.p['orders']))

    def gateway_request(self, name='interrupt-0001'):
        return dict(id=name, revision=self.session.controller.state.get('control_revision', 0), action=self.request())

    def net(self):
        return sum((sum((D(d[k]) for k in ('profit', 'commission', 'swap', 'fee')), D(0)) for d in self.p['deals']), D(0))

    def assert_settled(self, carry=None):
        s = self.session.controller.state
        self.assertFalse(self.p['orders']); self.assertFalse(self.p['positions'])
        self.assertIsNone(s['cycle']); self.assertTrue(s['stop']); self.assertEqual(s['phase'], 'WAIT_START')
        self.assertEqual(s['completed'][-1]['completion'], 'PAPER_INTERRUPTED_RETAINED')
        if carry is not None: self.assertEqual(D(s['basket_carry']), carry)
        view = self.session.view()
        self.assertFalse(view['orders']); self.assertFalse(view['positions'])
        self.assertEqual(view['paper']['last_interrupt']['recovery_retained'], s['basket_carry'])

    def test_interrupt_buy_bid_costs_carry_and_restart(self):self._interrupt_side('BUY')
    def test_interrupt_sell_ask_costs_carry_and_restart(self):self._interrupt_side('SELL')
    def _interrupt_side(self,side):
        self.action(dict(type='paper_costs', commission_per_lot_side='2'))
        self.initial(side)
        self.session.controller.state['basket_carry'] = '7'
        self.session.controller.state['cycle']['realized'] = str(D(self.session.controller.state['cycle']['realized']) - 7)
        self.session.controller.save('fixture_existing_debt')
        before_ids={d['id'] for d in self.p['deals']}
        quote=deepcopy(self.session.exchange.market['quote'])
        req=self.gateway_request('interrupt-'+side)
        self.assertEqual(self.session.gateway.apply(req)['status'], 'APPLIED')
        new=[d for d in self.p['deals'] if d['id'] not in before_ids]
        self.assertEqual(len(new),1); self.assertEqual(new[0]['price'],quote['bid' if side=='BUY' else 'ask'])
        self.assertEqual(D(new[0]['commission']),D('-.02'))
        carry=D(self.session.controller.state['completed'][-1]['carry_before'])-D(self.session.controller.state['completed'][-1]['cycle_net'])
        self.assert_settled(max(D(0),carry))
        snapshot=deepcopy(self.p)
        self.assertEqual(self.session.gateway.apply(req)['status'],'APPLIED');self.assertEqual(snapshot,self.p)
        self.reader.tick('4000','4000.1',seconds=120,bar=('3999','4000')); self.step(3)
        self.assertFalse(self.p['orders']);self.assertIsNone(self.session.controller.state['cycle'])
        self.session.close();self.create();self.step()
        self.assert_settled(max(D(0),carry))
        self.assertEqual(self.session.gateway.apply(req)['status'],'APPLIED')

    def test_interrupt_cancels_pending_and_ready_commands(self):
        self.start(); req=self.gateway_request()
        original=deepcopy(self.session.controller.state['commands'])
        self.session.gateway.apply(req);self.assert_settled(D(0))
        self.assertEqual(self.p['order_history'][-1]['status'],'CANCELLED')
        for cmd in original.values():self.session.exchange.submit(cmd)
        self.assertFalse(self.p['orders'])
        self.assertFalse(self.p['positions'])

    def test_interrupt_closes_multiple_positions_and_link_orders(self):
        self.initial(); c=self.session.controller.state['cycle']
        self.action(dict(type='v3_link',cycle_id=c['id'],expected_positions=sorted(p['id'] for p in self.p['positions'])))
        self.step(3);self.assertTrue(self.p['orders']);self.assertTrue(self.p['positions'])
        self.action(self.request());self.assert_settled(max(D(0),-self.net()))

    def test_interrupt_full_recovery_basket_books_prior_loss_once(self):
        self.action(dict(type='paper_costs',commission_per_lot_side='2'))
        self.initial();self.reader.tick('3998','3998.1');self.step(4)
        c=self.session.controller.state['cycle'];self.assertEqual(self.session.controller.state['phase'],'V3_WAIT_ENTRY')
        self.action(dict(type='v3_entry',cycle_id=c['id'],order_type='MARKET',timing='now',side='SELL'))
        self.step(5);self.assertGreater(len(self.p['positions']),1)
        self.action(self.request());self.assert_settled(max(D(0),-self.net()))
        self.assertEqual(len(self.session.controller.state['seen_deals']),len([d for d in self.p['deals'] if d['entry']=='OUT']))

    def test_planning_partial_exception_does_not_cancel_or_close_anything(self):
        self.initial();before=deepcopy(self.p)
        with patch.object(type(self.session.exchange),'profit',side_effect=ValueError('invalid spec')):
            with self.assertRaises(ActionRejected):self.action(self.request())
        self.assertEqual(before,self.p)
        self.assertIsNotNone(self.session.controller.state['cycle'])

    def test_interrupt_after_unadopted_fill_reconciles_history(self):
        self.action(dict(type='paper_costs',commission_per_lot_side='2'))
        self.start();self.reader.tick('4000.9','4001');self.session.exchange.observe(self.reader.read())
        self.assertFalse(self.session.controller.state['cycle']['tickets'])
        self.action(self.request());self.assert_settled(max(D(0),-self.net()))

    def test_missing_unadopted_fill_history_never_erases_debt(self):
        self.start();self.reader.tick('4000.9','4001');self.session.exchange.observe(self.reader.read())
        self.assertTrue(self.p['positions']);self.assertFalse(self.session.controller.state['cycle']['tickets'])
        self.p['positions']=[];self.p['deals']=[];self.session.exchange.persist()
        before=deepcopy(self.p)
        with self.assertRaises(ActionRejected):self.action(self.request())
        self.assertEqual(before,self.p);self.assertIsNotNone(self.session.controller.state['cycle'])

    def test_orphan_exit_history_refused(self):
        self.start();self.reader.tick('4003','4003.1');self.session.exchange.observe(self.reader.read())
        self.assertFalse(self.p['positions'])
        self.p['deals']=[d for d in self.p['deals'] if d['entry']=='OUT'];self.session.exchange.persist()
        before=deepcopy(self.p)
        with self.assertRaises(ActionRejected):self.action(self.request())
        self.assertEqual(before,self.p)

    def test_stale_changed_missing_confirmation_unknown_and_partial_refused(self):
        self.initial();a=self.request()
        for change in ({'confirmed':False},{'cycle_id':'old'},{'expected_positions':[]},{'gap_confirmed':True},{'extra':True}):
            before=deepcopy(self.p)
            with self.assertRaises(ActionRejected):self.action(dict(a,**change))
            self.assertEqual(before,self.p)
        self.reader.now+=6
        with self.assertRaises(ActionRejected):self.action(a)
        self.reader.tick('4000.9','4001');self.step()
        with patch.object(self.reader.safety,'check',return_value='CLOCK_STEP'):
            with self.assertRaises(ActionRejected):self.action(self.request())
        position=self.p['positions'][0];position['size']='.005';self.session.exchange.persist()
        before=deepcopy(self.p)
        with self.assertRaises(ActionRejected):self.action(self.request())
        self.assertEqual(before,self.p)

    def test_time_or_quote_becoming_stale_during_planning_is_rejected(self):
        self.initial();before=deepcopy(self.p)
        with patch.object(self.reader.safety,'check',side_effect=[None,'CLOCK_CHANGED']):
            with self.assertRaises(ActionRejected):self.action(self.request())
        self.assertEqual(before,self.p)
        fresh=self.session._fresh;calls=[]
        def delayed():
            calls.append(True)
            if len(calls)==2:self.reader.now+=6
            return fresh()
        with patch.object(self.session,'_fresh',side_effect=delayed):
            with self.assertRaises(ActionRejected):self.action(self.request())
        self.assertEqual(before,self.p);self.assertIsNotNone(self.session.controller.state['cycle'])

    def test_restart_gap_requires_explicit_current_quote_confirmation(self):
        self.initial();self.session.close();self.reader.tick('3980','3980.1',seconds=60);self.create();self.step()
        self.assertTrue(self.p['paused_gap']);self.assertTrue(self.p['positions'])
        with self.assertRaises(ActionRejected):self.action(dict(self.request(),gap_confirmed=False))
        self.action(self.request());self.assert_settled(max(D(0),-self.net()))
        self.assertEqual(self.p['deals'][-1]['price'],'3980')
        self.assertTrue(self.p['paused_gap'],'interrupt must not resume quote gap automatically')

    def test_atomic_faults_leave_no_partial_effects_and_reconcile_receipt_on_restart(self):
        self.initial()
        for stage in ('paper_interrupt_exchange_written','paper_interrupt_snapshot_written','paper_interrupt_before_commit'):
            with self.subTest(stage=stage):
                before=deepcopy(self.p);req=self.gateway_request('fault-'+stage)
                def fault(event):
                    if event==stage:raise OSError('simulated disk failure')
                self.session.store.fault=fault
                with self.assertRaises(DurabilityError):self.session.gateway.apply(req)
                self.session.store.fault=None
                self.assertEqual(before,self.p);self.assertTrue(self.session.controller.state['cycle'])
                self.assertEqual(self.session.gateway.apply(req)['status'],'UNKNOWN')
                self.session.close();self.create();self.step()
                self.assertEqual(self.session.gateway.apply(req)['status'],'REJECTED')
                self.assertTrue(self.p['positions'])
        self.session.gateway.apply(self.gateway_request('new-confirmed-interrupt'));self.assert_settled(max(D(0),-self.net()))

    def test_crash_immediately_after_gateway_reservation_is_safe_to_retry(self):
        self.initial();req=self.gateway_request()
        with patch.object(self.session.gateway,'execute',side_effect=DurabilityError('crash before executor')):
            with self.assertRaises(DurabilityError):self.session.gateway.apply(req)
        self.assertEqual(self.session.controller.state['control_receipts'][req['id']]['status'],'UNKNOWN')
        self.session.close();self.create();self.step()
        self.assertEqual(self.session.gateway.apply(req)['status'],'REJECTED')
        self.assertTrue(self.p['positions'])
        self.session.gateway.apply(self.gateway_request('confirmed-after-restart'));self.assert_settled(max(D(0),-self.net()))

    def test_after_commit_crash_is_applied_once_on_restart(self):
        self.initial();req=self.gateway_request()
        def fault(event):
            if event=='paper_interrupt_after_commit':raise OSError('lost acknowledgement')
        self.session.store.fault=fault
        with self.assertRaises(DurabilityError):self.session.gateway.apply(req)
        self.session.store.fault=None;self.assert_settled(max(D(0),-self.net()))
        before=deepcopy(self.p);self.session.close();self.create();self.step()
        self.assertEqual(self.session.gateway.apply(req)['status'],'APPLIED');self.assertEqual(before,self.p)
        count=self.session.store.db.execute("SELECT count(*) FROM audit WHERE event='paper_cycle_interrupted'").fetchone()[0]
        self.assertEqual(count,1)

    def test_authenticated_paper_route_scope_and_flat_publication(self):
        from tradfi.web_control import WebTransport
        self.initial();self.step()
        transport=WebTransport(self.session.mailbox,role='paper',authority='127.0.0.1:8766',password='x'*32)
        headers={'Host':'127.0.0.1:8766','X-Local-Control':'1','Content-Type':'application/json'}
        _,_,extra=transport.request('POST','/api/login',headers,json.dumps({'password':'x'*32}).encode())
        headers['Cookie']=extra['Set-Cookie'].split(';')[0]
        req=self.gateway_request();body=json.dumps(req).encode()
        headers['X-Controller-Scope']='LIVE'
        self.assertEqual(transport.request('POST','/api/paper/action',headers,body)[0],409)
        headers['X-Controller-Scope']=self.session.scope
        self.assertEqual(transport.request('POST','/api/action',headers,body)[0],404)
        self.assertEqual(transport.request('POST','/api/paper/action',headers,body)[0],202)
        self.step()
        code,view,_=transport.request('GET','/api/paper/state',headers)
        self.assertEqual(code,200);self.assertFalse(view['positions']);self.assertFalse(view['orders'])
        self.assertEqual(view['action_results'][req['id']]['status'],'APPLIED')
        self.assertIsNone(view['cycle_id']);self.assertTrue(view['stopped'])

    def test_new_id_duplicate_and_stale_revision_never_close_new_cycle(self):
        self.initial();req=self.gateway_request();self.session.gateway.apply(req)
        with self.assertRaises(Conflict):self.session.gateway.apply(dict(req,id='stale-revision'))
        req2=dict(req,id='duplicate-new-id',revision=self.session.controller.state['control_revision'])
        self.assertEqual(self.session.gateway.apply(req2)['status'],'REJECTED')
        self.assertEqual(len(self.session.controller.state['completed']),1)

# The imported fixture is also a TestCase; avoid repeating legacy tests here.
for name in list(vars(PaperTest)):
    if name.startswith('test_') and name not in vars(PaperInterruptTest):
        setattr(PaperInterruptTest, name, None)
del PaperTest
