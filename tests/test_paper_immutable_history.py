from copy import deepcopy
import unittest
from tradfi.paper_immutable_history import ImmutableHistory
import test_tradfi_paper as fixtures


class ImmutableHistoryTest(unittest.TestCase):
    def test_rows_detached_read_only_and_deepcopy_reuses(self):
        original=[{'id':'1','nested':{'x':[{'v':1}]}}]
        cache=ImmutableHistory();rows=cache.snapshot(original)
        self.assertIs(deepcopy(rows)[0],rows[0])
        with self.assertRaises(TypeError):rows[0]['id']='bad'
        with self.assertRaises(TypeError):rows[0]['nested']['x'][0]['v']=2
        rows.clear();self.assertEqual(original[0]['id'],'1')
        self.assertEqual(len(cache.snapshot(original)),1)

    def test_correction_append_replacement_and_removal_invalidate(self):
        rows=[{'id':'a','value':1}];cache=ImmutableHistory()
        first=cache.snapshot(rows)[0]
        self.assertIs(cache.snapshot(rows)[0],first)
        rows.append({'id':'b','value':2})
        self.assertIs(cache.snapshot(rows)[0],first)
        rows[0]['value']=3
        changed=cache.snapshot(rows)
        self.assertEqual(changed[0]['value'],3);self.assertEqual(first['value'],1)
        rows[:]=[{'id':'c','value':4},{'id':'d','value':5}]
        self.assertEqual([x['id'] for x in cache.snapshot(rows)],['c','d'])
        rows.clear();self.assertEqual(cache.snapshot(rows),[])

    def test_same_value_different_type_is_not_suppressed(self):
        cache=ImmutableHistory();rows=[{'id':'x','value':1}]
        cache.snapshot(rows);rows[0]['value']=True
        self.assertIs(cache.snapshot(rows)[0]['value'],True)
        rows[0]['value']=1.0
        self.assertIs(type(cache.snapshot(rows)[0]['value']),float)

    def test_controller_history_immutable_but_public_snapshot_detached(self):
        f=fixtures.PaperTest();f.setUp()
        try:
            f.start();f.reader.tick('4000.9','4001');f.step(3)
            exchange=f.session.exchange
            internal=exchange.snapshot_for_controller()
            self.assertTrue(internal['deals'])
            with self.assertRaises(TypeError):internal['deals'][0]['profit']='999'
            public=exchange.snapshot();public['deals'][0]['profit']='999'
            self.assertNotEqual(exchange.p['deals'][0]['profit'],'999')
            self.assertEqual(f.session.controller.snapshot()['deals'],exchange.p['deals'])
        finally:f.tearDown();f.doCleanups()

class PaperDurableCopyTest(unittest.TestCase):
    def test_unchanged_reuses_copy_but_changed_state_is_detached(self):
        from unittest.mock import patch
        f=fixtures.PaperTest();f.setUp()
        try:
            c=f.session.controller
            old=c._durable_state
            with patch.object(c.store,'save',wraps=c.store.save) as save:
                c.save('same_state')
                self.assertEqual(save.call_count,1)
            self.assertIs(c._durable_state,old)
            c.state['notifications'].append({'id':'changed','text':'new'})
            c.save('changed_state')
            self.assertIsNot(c._durable_state,old)
            self.assertEqual(c.store.load(),c._durable_state)
            c.state['notifications'][-1]['text']='later'
            self.assertEqual(c._durable_state['notifications'][-1]['text'],'new')
        finally:f.tearDown();f.doCleanups()

    def test_type_only_change_updates_durable_copy(self):
        f=fixtures.PaperTest();f.setUp()
        try:
            c=f.session.controller;c.state['test_numeric']=1;c.save('numeric')
            c.state['test_numeric']=True;c.save('boolean')
            self.assertIs(c._durable_state['test_numeric'],True)
            self.assertIs(c.store.load()['test_numeric'],True)
        finally:f.tearDown();f.doCleanups()

if __name__=='__main__':unittest.main()
