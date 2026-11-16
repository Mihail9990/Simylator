"""Performance shortcuts retain complete, detached history and execution evidence."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tradfi.paper_position_history import PositionHistoryCache, position_history
import test_paper_position_history
from test_tradfi_paper import Reader
from tradfi.paper import PaperSession

class HistoryCacheTests(unittest.TestCase):
    def setUp(self):
        helper=test_paper_position_history.PaperPositionHistory()
        self.data=helper.exchange(helper.deal('i','IN',command_id='c'),helper.deal('o','OUT'))
        self.commands={'c':dict(kind='STOP',status='FILLED')}
        self.cache=PositionHistoryCache()
    def view(self):return self.cache.view(self.data,'UST',True,self.commands)
    def test_unchanged_projection_reused_and_detached(self):
        before=deepcopy(self.data);one=self.view();one['metrics']['all_closed_net']='bad';one['position_history'][0]['side']='bad'
        with patch('tradfi.paper_position_history.position_history',side_effect=AssertionError('Unchanged ledger recomputed')):
            two=self.view()
        self.assertEqual(two,position_history(self.data,'UST',True,self.commands));self.assertEqual(self.cache.rebuilds,1);self.assertEqual(self.data,before)
    def test_same_length_corrections_and_corruption_invalidate(self):
        self.view()
        for field,value in [('profit','4'),('profit','NaN'),('id','i'),('volume','.02'),('time',None)]:
            self.data['deals'][1][field]=value
            self.assertEqual(self.view(),position_history(self.data,'UST',True,self.commands))
        self.assertEqual(self.cache.rebuilds,6)
    def test_type_only_corrections_invalidate(self):
        for value in [1, True, 1.0, 0.0, -0.0]:
            self.data['deals'][1]['time']=value
            self.assertEqual(self.view(),position_history(self.data,'UST',True,self.commands))
        self.assertEqual(self.cache.rebuilds,5)
    def test_live_command_completeness_and_currency_invalidate(self):
        self.view();self.commands['missing']=dict(kind='STOP',status='FILLED')
        self.assertFalse(self.view()['metrics']['history_complete'])
        self.commands['missing']['status']='CANCELLED';self.assertTrue(self.view()['metrics']['history_complete'])
        self.data['positions']=[dict(id='p')]
        self.assertEqual(self.view(),position_history(self.data,'UST',True,self.commands))
        self.assertEqual(self.cache.view(self.data,'USD',True,self.commands),position_history(self.data,'USD',True,self.commands))
    def test_replacement_append_and_clear_invalidate(self):
        self.view();self.data['deals']=deepcopy(self.data['deals']);self.data['deals'][1]['price']='4004'
        self.assertEqual(self.view(),position_history(self.data,'UST',True,self.commands))
        self.data['deals'].append(deepcopy(self.data['deals'][1]));self.assertFalse(self.view()['metrics']['history_complete'])
        self.data['deals']=[];self.assertFalse(self.view()['metrics']['history_complete'])

class CompactPaperSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.reader=Reader();self.session=PaperSession(Path(self.tmp.name)/'p.db',self.reader,clock=lambda:self.reader.now,monotonic=lambda:self.reader.now)
        self.addCleanup(self.session.close);self.session.step()
    def test_default_controller_snapshot_keeps_full_evidence(self):
        self.assertIn('deals',self.session.exchange.snapshot());self.assertIn('order_history',self.session.exchange.snapshot())
        self.assertTrue(self.session.exchange.snapshot()['history_complete'])
        compact=self.session.exchange.snapshot(include_history=False)
        self.assertNotIn('deals',compact);self.assertNotIn('order_history',compact);self.assertFalse(compact['history_complete'])
        self.assertNotIn('deals',self.session.snapshot)
    def test_display_never_copies_execution_history_from_saved_snapshot(self):
        class MustNotCopy(list):
            def __deepcopy__(self,memo):raise AssertionError('Presentation copied complete execution history')
        self.session.snapshot['deals']=MustNotCopy();self.session.snapshot['order_history']=MustNotCopy()
        view=self.session.view();self.assertEqual(view['position_history'],[])
    def test_timings_are_bounded_numeric_diagnostics(self):
        self.session._diagnostic_last_step=0;self.reader.tick('4000','4000.1');self.session.step()
        rows=self.session.diagnostics.drain();row=[r for r in rows if r['event']=='owner_step'][-1]
        for key in ('duration_ms','tick_ms','exchange_snapshot_ms','exchange_persist_ms','history_ms','view_ms'):
            self.assertIn(key,row);self.assertGreaterEqual(row[key],0)

    def test_publication_cadence_does_not_skip_strategy_samples(self):
        self.session.publication_interval=100
        previous=self.session.exchange.p['cursor']
        with patch.object(self.session.controller,'tick',wraps=self.session.controller.tick) as tick, patch.object(self.session.mailbox,'drain',wraps=self.session.mailbox.drain) as publish:
            self.reader.tick('4001','4001.1');self.session.step()
            self.assertEqual(tick.call_count,1)
            self.assertEqual(publish.call_count,0)
            self.assertNotEqual(self.session.exchange.p['cursor'],previous)
        import time
        self.session._last_publication=time.perf_counter()-self.session.publication_interval-1
        with patch.object(self.session.mailbox,'drain',wraps=self.session.mailbox.drain) as publish:
            self.reader.tick('4002','4002.1');self.session.step()
            self.assertEqual(publish.call_count,1)

    def test_fatal_capture_keeps_panel_readonly_and_rejects_actions(self):
        from tradfi.control import ActionRejected
        before=deepcopy(self.session.controller.state)
        self.session.latch_capture_failure('QUOTE_CAPTURE_OVERFLOW')
        view=self.session.mailbox.view()
        self.assertTrue(view['read_only'])
        self.assertEqual(view['feed']['status'],'ERROR')
        self.assertEqual(view['paper']['capture_failure'],'QUOTE_CAPTURE_OVERFLOW')
        self.assertTrue(any(row.get('event')=='capture_failed' and row.get('error_code')=='QUOTE_CAPTURE_OVERFLOW' for row in self.session.diagnostics.drain()))
        with patch.object(self.reader,'read',side_effect=AssertionError('source read after failure')), patch.object(self.session.controller,'tick',side_effect=AssertionError('strategy after failure')):
            self.session.step()
        with self.assertRaises(ActionRejected):self.session.action({'type':'command','text':'/start'})
        self.assertEqual(self.session.controller.state,before)
