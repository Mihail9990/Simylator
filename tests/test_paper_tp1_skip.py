"""Only PAPER may skip an unsubmitted TP1 own-entry move; protections persist."""
from copy import deepcopy
from decimal import Decimal as D
import unittest
from unittest.mock import patch
import test_paper_interrupt as fixture
import test_tp1_own_entry_sl as live_fixture
from tradfi.storage import DurabilityError


class PaperTP1SkipTest(unittest.TestCase):
    def setUp(self):
        self.f=fixture.PaperInterruptTest();self.f.setUp()
        self.addCleanup(self.f.doCleanups);self.addCleanup(self.f.tearDown)
        self.f.initial();self.f.reader.tick('3998','3998.1');self.f.step(4)
        p=self.f.session;c=p.controller.state['cycle']
        self.f.action(dict(type='v3_entry',cycle_id=c['id'],order_type='MARKET',timing='now',side='SELL'))
        self.f.step(6)
        self.group_id=c['groups'][-1]['id']
        self.old={x['id']:(x['sl'],x['tp']) for x in p.exchange.p['positions']}

    @property
    def p(self):return self.f.session
    @property
    def c(self):return self.p.controller.state['cycle']
    @property
    def g(self):return next(g for g in self.c['groups'] if g['id']==self.group_id)
    def tp1(self, unavailable=True, legacy=False):
        if unavailable:self.f.reader.data['spec']['freeze_level']=10000
        t=next(t for t in self.c['tickets'].values() if t['group']==self.group_id and t['index']==0)
        price=D(t['tp']);self.f.reader.tick(price-D('.1'),price)
        if legacy:
            with patch.object(self.p.controller,'_paper_tp1_facts_safe',return_value=False):self.f.step()
            self.assertEqual(self.c.get('manual_reason'),'TP1_OWN_ENTRY_SL_UNAVAILABLE')
        else:self.f.step(2)
    def assert_retained(self):
        self.assertTrue(all((p['sl'],p['tp'])==self.old[p['id']] for p in self.p.exchange.p['positions']))
        self.assertEqual(len(self.p.exchange.p['positions']),5)
    def modify_ids(self):return {cid for cid,cmd in self.p.controller.state['commands'].items() if cmd['kind']=='MODIFY'}

    def test_skip_retains_sl_tp_and_cycle_keeps_observing_closures(self):
        before=self.modify_ids();self.tp1();self.assert_retained()
        self.assertFalse(self.c.get('manual'));self.assertEqual(self.g['entry_sl_once']['status'],'SKIPPED')
        self.assertEqual(self.modify_ids(),before)
        self.assertIsNone(self.p.view()['manual_reason'])
        price=min(D(p['tp']) for p in self.p.exchange.p['positions'])
        self.f.reader.tick(price-D('.1'),price);self.f.step(5)
        self.assertFalse(self.p.exchange.p['positions'])
        self.assertNotEqual(self.p.controller.state['phase'],'MANUAL')

    def test_remaining_old_sl_still_closes_positions(self):
        self.tp1();price=D(self.p.exchange.p['positions'][0]['sl'])
        self.f.reader.tick(price-D('.1'),price);self.f.step(4)
        self.assertFalse(self.p.exchange.p['positions'])
        self.assertEqual(len([d for d in self.p.exchange.p['deals'] if d['entry']=='OUT' and d['reason']=='SL']),6)
        self.assertNotEqual(self.p.controller.state['phase'],'MANUAL')

    def test_successful_move_is_unchanged(self):
        self.tp1(unavailable=False);self.f.step(5)
        self.assertEqual(self.g['entry_sl_once']['status'],'DONE')
        self.assertTrue(all(D(p['sl'])==D(p['entry']) for p in self.p.exchange.p['positions']))

    def test_no_later_automatic_retry_or_restart_duplicate(self):
        self.tp1();before=self.modify_ids()
        self.f.reader.data['spec']['freeze_level']=0;self.f.step(5)
        self.assertEqual(self.modify_ids(),before);self.assert_retained()
        self.p.close();self.f.create();self.f.step()
        self.assertTrue(self.p.exchange.p['paused_gap'])
        self.f.action(dict(type='paper_resume'));self.f.step(5)
        self.assertEqual(self.g['entry_sl_once']['status'],'SKIPPED')
        self.assertEqual(self.modify_ids(),before);self.assert_retained()

    def test_existing_pause_recovers_on_verified_tick(self):
        self.tp1(legacy=True);before=self.modify_ids();self.f.step(2)
        self.assertFalse(self.c.get('manual'));self.assertNotIn('manual_reason',self.c)
        self.assertEqual(self.g['entry_sl_once']['status'],'SKIPPED')
        self.assertEqual(self.modify_ids(),before);self.assert_retained()
        self.assertNotIn('Защитная пауза',self.p.view()['journal'][-1])

    def test_existing_pause_restart_does_not_remove_independent_gap_pause(self):
        self.tp1(legacy=True);self.p.close();self.f.create();self.f.step()
        self.assertFalse(self.c.get('manual'));self.assertTrue(self.p.exchange.p['paused_gap'])
        self.assertEqual(self.g['entry_sl_once']['status'],'SKIPPED');self.assert_retained()
        self.f.action(dict(type='paper_resume'));self.f.step()
        self.assertFalse(self.p.exchange.p['paused_gap']);self.assert_retained()

    def test_unknown_receipt_or_unfinished_modify_prevents_recovery(self):
        self.tp1(legacy=True);original=deepcopy(self.p.controller.state)
        for case in ('unknown','pending','partial'):
            self.p.controller.state=deepcopy(original)
            if case=='unknown':self.c['fixture']='unknown';self.p.controller.state.setdefault('control_receipts',{})['fixture']={'status':'UNKNOWN'}
            elif case=='pending':self.g['entry_sl_once']={'status':'PENDING','targets':[]}
            else:self.g['entry_sl_once']={'status':'FAILED','targets':[]}
            self.assertFalse(self.p.controller._restore_paper_tp1_pause(self.p.controller.snapshot()))
            self.assertTrue(self.c['manual']);self.assert_retained()

    def test_changed_protection_indexes_or_position_blocks_recovery(self):
        self.tp1(legacy=True);original=deepcopy(self.p.controller.state)
        for field in ('sl','size','index'):
            self.p.controller.state=deepcopy(original)
            t=next(t for t in self.c['tickets'].values() if not t['closed'])
            t[field]=99 if field=='index' else '123'
            self.assertFalse(self.p.controller._restore_paper_tp1_pause(self.p.controller.snapshot()))
            self.assertTrue(self.c['manual'])

    def test_other_manual_pause_is_not_removed(self):
        self.tp1(legacy=True);self.c['manual_reason']='ENTRY_SL_PARTIAL_OR_REJECTED'
        self.assertFalse(self.p.controller._restore_paper_tp1_pause(self.p.controller.snapshot()))
        self.assertTrue(self.c['manual'])

    def test_expired_cycle_is_not_revived(self):
        self.tp1(legacy=True);self.c['branch_deadline_utc']=self.f.reader.now-1
        self.assertFalse(self.p.controller._restore_paper_tp1_pause(self.p.controller.snapshot()))
        self.assertTrue(self.c['manual']);self.assertNotEqual(self.c['manual_reason'],'TP1_OWN_ENTRY_SL_UNAVAILABLE')

    def test_atomic_recovery_save_failure_keeps_persisted_latch(self):
        self.tp1(legacy=True)
        with patch.object(self.p.store,'save',side_effect=DurabilityError('fixture')):
            with self.assertRaises(DurabilityError):self.p.controller._restore_paper_tp1_pause(self.p.controller.snapshot())
        self.assertTrue(self.c['manual']);self.assertNotIn('entry_sl_once',self.g)
        self.f.step();self.assertFalse(self.c.get('manual'));self.assert_retained()

    def test_nonpaper_policy_still_pauses(self):
        f=live_fixture.TP1OwnEntry();f.setUp()
        try:
            f.f.b.data['spec']['freeze_level']=10000
            f.f.b.close('TP',position_id=f.ts[0]['id']);f.f.tick(3)
            self.assertEqual(f.f.bot.state['cycle'].get('manual_reason'),'TP1_OWN_ENTRY_SL_UNAVAILABLE')
            self.assertNotIn('entry_sl_once',f.g)
        finally:f.doCleanups()
