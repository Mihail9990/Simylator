from copy import deepcopy
from decimal import Decimal as D
import time
import unittest
from unittest.mock import patch
import test_candle_paper_session as fixtures
from tradfi.control import ActionRejected
from tradfi.storage import DurabilityError
from tradfi.paper import PaperSession

class CycleResetTests(unittest.TestCase):
    setUp=fixtures.CandlePaperSession.setUp
    step=fixtures.CandlePaperSession.step
    start=fixtures.CandlePaperSession.start
    def request(self,id='reset-cycle-000001',revision=0):
        return dict(id=id,revision=revision,action={'type':'paper_reset_cycle'})
    def reset(self):return self.session.gateway.apply(self.request())
    def test_open_positions_settled_orders_cancelled_history_preserved_and_auto_restart(self):
        self.start();old=self.bot.state['cycle']['id'];before=deepcopy(self.session.exchange.p['deals'])
        positions=deepcopy(self.session.exchange.p['positions']);orders=deepcopy(self.session.exchange.p['orders'])
        quote=deepcopy(self.reader.view()['quote'])
        receipt=self.reset();self.bot=self.session.controller
        self.assertEqual(receipt['status'],'APPLIED');self.assertIsNone(self.bot.state['cycle'])
        self.assertEqual(self.bot.state['phase'],'WAIT_CANDLE');self.assertFalse(self.bot.state['stop'])
        self.assertEqual(self.bot.state['basket_carry'],'0')
        self.assertFalse(self.session.exchange.p['positions']);self.assertFalse(self.session.exchange.p['orders'])
        self.assertEqual(self.session.exchange.p['deals'][:len(before)],before)
        exits=self.session.exchange.p['deals'][len(before):]
        self.assertEqual(len(exits),len(positions));self.assertEqual(exits[0]['price'],quote['bid'])
        self.assertEqual(exits[0]['reason'],'PAPER_RESET')
        self.assertEqual(self.bot.state['last_paper_reset']['cancelled_orders'],[o['id'] for o in orders])
        total_before=sum((D(d[k]) for d in self.session.exchange.p['deals'] for k in ('profit','commission','swap','fee')),D(0))
        self.assertEqual(D(self.session.view()['metrics']['all_closed_net']),total_before)
        self.reader.tick('4000','4000.1',seconds=60,bar=('3999','4000'));self.step(6)
        self.assertNotEqual(self.bot.state['cycle']['id'],old);self.assertTrue(self.session.exchange.p['orders'])
        self.assertEqual(D(self.bot.state['cycle']['recovery']),0)
    def test_unknown_edit_manual_clock_and_raw_source_do_not_block(self):
        self.start();c=self.bot.state['cycle'];c.update(manual=True,manual_reason='TEST',edit={'stage':'UNKNOWN'})
        self.bot.state.update(phase='MANUAL',time_block={'reason':'OLD'})
        self.bot.state.setdefault('control_receipts',{})['old-unknown']=dict(status='UNKNOWN',digest='old',revision=0)
        pending=next(iter(c['entries']));self.bot.state['commands'][pending]['status']='UNKNOWN'
        self.bot.save('fixture_blockers')
        self.reader.data['timestamp_semantics']='BROKER_RAW';self.reader.data['status']='OBSERVED'
        self.session.exchange.available=False
        with patch.object(self.reader.safety,'check',return_value='OLD_CLOCK'):
            self.assertEqual(self.reset()['status'],'APPLIED')
        self.assertIsNone(self.bot.state['time_block']);self.assertFalse(self.bot.state['stop'])
        self.assertEqual(self.bot.state['control_receipts']['old-unknown']['status'],'REJECTED')
    def test_flat_without_quote_resets_carry_without_fabricated_trade(self):
        self.bot.state['basket_carry']='7';self.bot.save('carry')
        self.session.exchange.p.pop('last_verified_market',None);self.session.exchange.market=None;self.session.exchange.persist()
        with patch.object(self.reader,'view',return_value={}):self.reset()
        self.assertEqual(self.bot.state['basket_carry'],'0');self.assertFalse(self.session.exchange.p['deals'])
        self.assertEqual(self.bot.state['last_paper_reset']['written_off_recovery'],'7')
        self.assertEqual(self.bot.state['phase'],'WAIT_CANDLE')
        self.assertEqual(self.session.view()['metrics']['all_closed_net'],'0')
    def test_open_without_any_quote_rejects_without_deleting_positions(self):
        self.start();self.session.exchange.p.pop('last_verified_market',None);self.session.exchange.market=None
        before=deepcopy(self.session.exchange.p)
        with patch.object(self.reader,'view',return_value={}):
            with self.assertRaises(ActionRejected):self.session.action({'type':'paper_reset_cycle'})
        self.assertEqual(self.session.exchange.p,before)
    def test_reset_is_priority_before_reader_or_strategy_and_supersedes_old_queue(self):
        self.start();revision=self.bot.state.get('control_revision',0)
        self.session.mailbox.submit(dict(id='old-start-00001',revision=revision,action={'type':'command','text':'/start'}))
        self.session.mailbox.submit(self.request())
        with patch.object(self.reader,'read',side_effect=AssertionError('waited for quotes')),patch.object(self.bot,'tick',side_effect=AssertionError('strategy before reset')):
            self.session.step()
        self.assertEqual(self.bot.state['control_receipts']['old-start-00001']['status'],'REJECTED')
        self.assertEqual(self.bot.state['control_receipts']['reset-cycle-000001']['status'],'APPLIED')
        self.assertTrue(self.session.mailbox.requests.empty())
    def test_pre_reset_capture_samples_never_enter_new_cycle(self):
        self.start();self.reset();cutoff=self.session._paper_reset_capture_cutoff
        sample=deepcopy(self.reader.read());sample['capture']={'observed_monotonic':cutoff-1,'sequence':1}
        with patch.object(self.reader,'read',return_value=sample),patch.object(self.bot,'tick',side_effect=AssertionError('old samples replayed')),patch.object(self.reader,'acknowledge',create=True) as ack:
            self.session.step();ack.assert_called_once()
        self.assertFalse(self.session.exchange.p['orders'])
    def test_superseded_unchanged_sample_does_not_starve_new_commands(self):
        self.start();self.reset();cutoff=self.session._paper_reset_capture_cutoff
        self.session.mailbox.drain(self.session.gateway,self.session.view,limit=0)
        request=dict(id='new-stop-after-reset',revision=self.bot.state['control_revision'],
                     action={'type':'command','text':'/stop'})
        self.assertEqual(self.session.mailbox.submit(request)['status'],'QUEUED')
        sample=deepcopy(self.reader.read());sample['capture']={'observed_monotonic':cutoff-1,'sequence':1}
        with patch.object(self.reader,'read',return_value=sample),patch.object(self.bot,'tick',side_effect=AssertionError('old samples replayed')),patch.object(self.reader,'acknowledge',create=True) as ack:
            self.session.step();ack.assert_called_once()
        self.assertEqual(self.bot.state['control_receipts'][request['id']]['status'],'APPLIED')
        self.assertTrue(self.bot.state['stop']);self.assertFalse(self.session.exchange.p['orders'])

    def test_rollback_and_crash_after_commit_are_atomic_and_idempotent(self):
        self.start();before=deepcopy(self.session.exchange.p)
        def fail(event):
            if event=='paper_reset_snapshot_written':raise RuntimeError('disk')
        self.session.store.fault=fail
        with self.assertRaises(DurabilityError):self.reset()
        self.session.store.fault=None
        self.assertEqual(self.session.exchange.p,before)
        # New owner startup resolves the reserved-only receipt without replay.
        path=self.session.store.path if hasattr(self.session.store,'path') else self.session.store.db.execute('PRAGMA database_list').fetchone()[2]
        self.session.close();self.session=PaperSession(path,self.reader,clock=lambda:self.reader.now,monotonic=lambda:self.reader.now);self.addCleanup(self.session.close);self.bot=self.session.controller
        self.assertEqual(self.bot.state['control_receipts']['reset-cycle-000001']['status'],'REJECTED')
        def after(event):
            if event=='paper_reset_after_commit':raise RuntimeError('crash after durable')
        self.session.store.fault=after
        with self.assertRaises(DurabilityError):self.session.gateway.apply(self.request('reset-cycle-000002'))
        self.session.store.fault=None
        self.assertEqual(self.bot.state['control_receipts']['reset-cycle-000002']['status'],'APPLIED')
        self.assertFalse(self.session.exchange.p['positions']);count=len(self.bot.state['paper_resets'])
        self.assertEqual(self.session.gateway.apply(self.request('reset-cycle-000002'))['status'],'APPLIED')
        self.assertEqual(len(self.bot.state['paper_resets']),count)

    def test_reset_drops_future_old_clock_candle_boundary(self):
        self.start();self.bot.state['last_candle']=self.reader.data['candle']['id']+86400
        self.bot.save('old_future_clock')
        self.reader.data['timestamp_semantics']='BROKER_RAW';self.reader.data['status']='OBSERVED'
        self.reset()
        self.assertEqual(self.bot.state['last_candle'],self.reader.data['candle']['id'])
        self.reader.tick('4000','4000.1',seconds=60,bar=('3999','4000'));self.step(6)
        self.assertIsNotNone(self.bot.state['cycle']);self.assertTrue(self.session.exchange.p['orders'])

    def test_uncommitted_unknown_reset_reconciles_on_same_id_retry_without_restart(self):
        self.start();request=self.request()
        with patch.object(self.session.gateway,'execute',side_effect=ValueError('pre-effect failure')):
            self.assertEqual(self.session.gateway.apply(request)['status'],'UNKNOWN')
        before=deepcopy(self.session.exchange.p)
        self.session.mailbox.drain(self.session.gateway,self.session.view,limit=0)
        self.assertEqual(self.session.mailbox.submit(request)['status'],'QUEUED')
        self.session.step()
        self.assertEqual(self.bot.state['control_receipts'][request['id']]['status'],'REJECTED')
        self.assertEqual(self.session.exchange.p,before)
        self.assertEqual(self.session.gateway.apply(self.request('reset-cycle-000002'))['status'],'APPLIED')

    def test_source_failure_is_not_a_reset_blocker(self):
        self.start();self.session.latch_capture_failure('QUOTE_CAPTURE_EXITED')
        self.session.mailbox.submit(self.request())
        with patch.object(self.reader,'read',side_effect=AssertionError('source before reset')):
            self.session.step()
        self.assertEqual(self.bot.state['basket_carry'],'0')
        self.assertEqual(self.bot.state['phase'],'WAIT_CANDLE')
        self.assertEqual(self.bot.state['control_receipts']['reset-cycle-000001']['status'],'APPLIED')
        self.assertEqual(self.session.view()['paper']['capture_failure'],'QUOTE_CAPTURE_EXITED')
    def test_sell_positions_close_at_ask_and_display_baseline_is_untouched(self):
        self.start();level=self.session.exchange.p['orders'][0]['price']
        self.reader.tick(level,str(D(level)+D('.1')));self.step(8)
        self.assertEqual({p['side'] for p in self.session.exchange.p['positions']},{'SELL'})
        self.session.action({'type':'paper_reset_total','confirmed':True})
        baseline=deepcopy(self.bot.state['paper_total_baseline'])
        ids={p['id'] for p in self.session.exchange.p['positions']};ask=self.reader.view()['quote']['ask']
        self.reset()
        exits=[d for d in self.session.exchange.p['deals'] if d['position_id'] in ids and d['entry']=='OUT']
        self.assertEqual(len(exits),len(ids));self.assertEqual({d['price'] for d in exits},{ask})
        self.assertEqual(self.bot.state['paper_total_baseline'],baseline)
