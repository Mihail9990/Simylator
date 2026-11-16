"""Priority reset transport remains scoped, idempotent and generation-safe."""
from copy import deepcopy
import threading
import unittest
from tradfi.control import ActionGateway, ActionRejected, Conflict
from tradfi.paper_commands import PaperCommandMailbox, apply_reset_supersession


class Controller:
    def __init__(self):
        self.state=dict(backend='paper_mt5_quotes_v1',simulator_variant='sl-overlap-recovery-v1',
                        control_revision=10,control_receipts={})
        self.saved=[];self.reset_count=0;self.normal=[];self.during_reset=None;self.fail_reset=False
    def save(self,event):
        self.saved.append((event,deepcopy(self.state)))
        self._durable_state=deepcopy(self.state)
    def execute(self,action):
        if action=={'type':'paper_reset_cycle'}:
            if self.fail_reset:raise ActionRejected('No observed close price')
            if self.during_reset:self.during_reset()
            updated=deepcopy(self.state)
            apply_reset_supersession(updated,self)
            updated['control_receipts'][self._active_control_id]['status']='APPLIED'
            self.reset_count+=1;self.state=updated;self.save('atomic_reset')
        else:self.normal.append(deepcopy(action))


class ResetPriorityTests(unittest.TestCase):
    def setUp(self):
        self.bot=Controller();self.gateway=ActionGateway(self.bot,execute=self.bot.execute)
        self.mail=PaperCommandMailbox(capacity=1,monotonic=lambda:200,max_age_seconds=2,current_execution_view=lambda:self.view())
        self.mail.drain(self.gateway,self.view,limit=0)
    def view(self):
        return dict(mode='PAPER',scope='paper',revision=self.bot.state['control_revision'],
            candle_recovery={'variant':'sl-overlap-recovery-v1'},feed={'status':'WAITING'},
            action_results={rid:{'status':r['status'],'revision':r['revision'],'reason':r.get('reason')} for rid,r in self.bot.state['control_receipts'].items()})
    def req(self,id,reset=False,revision=None):
        return dict(id=id,revision=self.bot.state['control_revision'] if revision is None else revision,
                    action={'type':'paper_reset_cycle'} if reset else {'type':'command','text':'/start'})
    def test_reset_bypasses_full_queue_context_and_stale_revision(self):
        old=self.req('old-command');self.mail.submit(old)
        reset=self.req('reset-command',True,0);reset['context_token']='0'*64
        self.assertEqual(self.mail.submit(reset)['status'],'QUEUED')
        self.assertTrue(self.mail.drain_reset(self.gateway,self.view))
        self.assertEqual(self.bot.reset_count,1);self.assertEqual(self.bot.normal,[])
        self.assertEqual(self.mail.requests.qsize(),0)
        self.assertEqual(self.bot.state['control_receipts']['old-command']['status'],'REJECTED')
        self.assertEqual(self.bot.state['control_receipts']['reset-command']['status'],'APPLIED')
        self.assertTrue(self.bot.state['control_receipts']['reset-command']['paper_cycle_reset_atomic'])
        self.assertNotIn('control_action_completed',[event for event,_ in self.bot.saved])
    def test_arrival_during_reset_cannot_execute_even_with_predicted_revision(self):
        self.bot.during_reset=lambda:self.mail.submit(self.req('during-command'))
        self.mail.submit(self.req('reset-command',True))
        self.mail.drain_reset(self.gateway,self.view)
        self.assertEqual(self.mail.requests.qsize(),1)
        self.mail.drain(self.gateway,self.view)
        self.assertEqual(self.bot.normal,[])
        self.assertEqual(self.bot.state['control_receipts']['during-command']['status'],'REJECTED')
    def test_repeated_reset_does_not_clear_fresh_queue(self):
        reset=self.req('reset-command',True);self.mail.submit(reset);self.mail.drain_reset(self.gateway,self.view)
        self.mail.submit(self.req('new-command'))
        self.mail._receipts={}  # retry received before refreshed receipt reaches transport
        self.mail.submit(reset);self.mail.drain_reset(self.gateway,self.view)
        self.assertEqual(self.bot.reset_count,1)
        self.assertEqual(self.mail.requests.qsize(),1)
        self.mail.drain(self.gateway,self.view)
        self.assertEqual(len(self.bot.normal),1)
    def test_failed_reset_does_not_remove_queued_requests(self):
        self.mail.submit(self.req('old-command'));self.bot.fail_reset=True
        self.mail.submit(self.req('reset-command',True));self.mail.drain_reset(self.gateway,self.view)
        self.assertEqual(self.mail.requests.qsize(),1);self.assertEqual(self.bot.reset_count,0)
        self.assertEqual(self.bot.state['control_receipts']['reset-command']['status'],'REJECTED')
    def test_unknown_receipts_superseded_but_success_receipts_kept(self):
        self.bot.state['control_receipts']={'unknown-old':dict(status='UNKNOWN',digest='x',revision=2),
            'done-old':dict(status='APPLIED',digest='y',revision=3)}
        self.mail.submit(self.req('reset-command',True));self.mail.drain_reset(self.gateway,self.view)
        receipts=self.bot.state['control_receipts']
        self.assertEqual(receipts['unknown-old']['status'],'REJECTED')
        self.assertEqual(receipts['unknown-old']['previous_status'],'UNKNOWN')
        self.assertEqual(receipts['done-old']['status'],'APPLIED')
    def test_gateway_bypass_never_applies_live_other_variant_or_other_action(self):
        for change,action in [({'backend':'mt5_live'}, {'type':'paper_reset_cycle'}),
                              ({'simulator_variant':'legacy'}, {'type':'paper_reset_cycle'}),
                              ({}, {'type':'paper_reset_total','confirmed':True})]:
            bot=Controller();bot.state.update(change);gateway=ActionGateway(bot,execute=bot.execute)
            with self.assertRaises(Conflict):gateway.apply(dict(id='stale-command',revision=0,action=action))
            self.assertEqual(bot.reset_count,0)
        with self.assertRaises(Conflict):self.gateway.apply(self.req('ordinary-old',False,0))
    def test_gateway_reset_skips_guard_only_for_exact_scoped_action(self):
        def reject(_):raise AssertionError('Reset invoked ordinary context guard')
        self.gateway.apply(self.req('reset-command',True,0),pre_execute=reject)
        self.assertEqual(self.bot.reset_count,1)
    def test_non_overlap_mailbox_cannot_request_reset(self):
        self.mail._view['candle_recovery']['variant']='legacy'
        with self.assertRaises(ActionRejected):self.mail.submit(self.req('reset-command',True))
    def test_same_id_changed_payload_still_conflicts(self):
        reset=self.req('reset-command',True);self.gateway.apply(reset)
        reset['revision']=0
        with self.assertRaises(Conflict):self.gateway.apply(reset)
