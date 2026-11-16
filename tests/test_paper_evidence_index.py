"""Indexed PAPER reconciliation must match unindexed evidence, including late facts."""
from copy import deepcopy
from types import SimpleNamespace
import random
import unittest
from tradfi.engine import Controller
from tradfi.paper_evidence_index import PaperEvidenceIndex


class EvidenceIndexTest(unittest.TestCase):
    def reconcile(self, state, snap, indexed):
        c=object.__new__(Controller);c.evidence_contract='';c.state=deepcopy(state)
        c.broker=SimpleNamespace(backend_id='paper_mt5_quotes_v1' if indexed else 'test',result=lambda cid:{'status':'ACCEPTED','order_id':'o'+cid})
        c.save=lambda event:None
        c._reconcile_commands(deepcopy(snap))
        return c.state

    def test_matches_unindexed_all_statuses_and_shuffled_late_evidence(self):
        for seed in range(30):
            rng=random.Random(seed)
            commands={};orders=[];history=[];deals=[];positions=[]
            for i in range(80):
                cid=str(i);pid='p'+cid;oid='o'+cid
                commands[cid]=dict(id=cid,kind=rng.choice(['STOP','MARKET','CANCEL','MODIFY','CLOSE']),status=rng.choice(['READY','UNKNOWN','ACCEPTED','FILLED','DONE','CANCELLED','REJECTED','OBSOLETE']),order_id=oid,position_id=pid,sl='1',tp='3',result={'order_id':oid})
                if rng.random()<.8:
                    history.append(dict(id=oid,command_id=cid,status=rng.choice(['FILLED','CANCELLED','REJECTED'])))
                if rng.random()<.6:
                    orders.append(dict(id=oid,command_id=cid,status='PENDING'))
                if rng.random()<.7:
                    deals.append(dict(entry='IN',command_id=cid if rng.random()<.5 else 'other',order_id=oid,position_id=pid))
                if rng.random()<.7:
                    deals.append(dict(entry='OUT',position_id=pid,sl=rng.choice(['1','2']),tp='3'))
                if rng.random()<.3:positions.append(dict(id=pid,sl='1',tp='3'))
            rng.shuffle(deals);rng.shuffle(history)
            snap=dict(orders=orders,order_history=history,deals=deals,positions=positions)
            state=dict(commands=commands)
            self.assertEqual(self.reconcile(state,snap,False),self.reconcile(state,snap,True),seed)

    def test_index_is_rebuilt_for_same_length_mutation(self):
        snap=dict(orders=[],order_history=[dict(id='o1',command_id='1',status='CANCELLED')],deals=[],positions=[])
        before=PaperEvidenceIndex(snap)
        snap['order_history'][0]['status']='PENDING'
        after=PaperEvidenceIndex(snap)
        self.assertIn('o1',before.terminal_orders)
        self.assertNotIn('o1',after.terminal_orders)

    def test_order_union_preserves_precedence_and_duplicates(self):
        rows=[dict(id='x',command_id='1',status='CANCELLED'),dict(id='o1',command_id='other',status='FILLED'),dict(id='x',command_id='1',status='REJECTED')]
        index=PaperEvidenceIndex(dict(orders=rows[:1],order_history=rows[1:],deals=[],positions=[]))
        self.assertEqual(index.entry_orders('1','o1'),rows)

    def test_malformed_evidence_keeps_legacy_validation(self):
        from types import SimpleNamespace
        from tradfi.paper_evidence_index import paper_index
        controller=SimpleNamespace(broker=SimpleNamespace(backend_id='paper_mt5_quotes_v1'))
        snap=dict(orders=[],order_history=[],deals=[{'entry':'IN','order_id':'o'}],positions=[])
        self.assertIsNone(paper_index(controller,snap))
        snap['deals']=[];snap['order_history']=[{'id':'o'}]
        self.assertIsNone(paper_index(controller,snap))

if __name__=='__main__':unittest.main()
