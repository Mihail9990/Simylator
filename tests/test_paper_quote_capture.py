"""Spawn-process tests do not import MetaTrader5 or access a terminal."""
from queue import Full
import time
import unittest
from tradfi.paper_quote_capture import ProcessQuoteReader, CaptureFailure, _latest
CONFIG=dict(terminal_path='fake',symbol='XAUUSD.s',expected_login=1,expected_server='Bybit-Live-6')

def synthetic_capture(config, observations, status, requests, responses, stop, published, consumed, failure):
    _latest(status, dict(ready=True, sequence=0))
    for seq in range(1, 31):
        market=dict(symbol='XAUUSD.s',timestamp=1000+seq, tick_msc=1000000+seq,
            quote=dict(bid=str(4000+seq),ask=str(4001+seq)),
            candle=dict(id=seq),capture=dict(sequence=seq,observed_monotonic=time.monotonic()))
        try:
            if seq-1-consumed.value>=config['_capture_capacity']:raise Full
            observations.put_nowait(market)
        except Full:
            failure.value=1;stop.wait();return
        published.value=seq
        _latest(status,dict(ready=True,sequence=seq,view=market))
        if stop.wait(.005):return
    while not stop.is_set():
        _latest(status,dict(ready=True,sequence=30,observation_complete=True,view=market))
        try:
            rid, command=requests.get(timeout=.02)
            responses.put((rid,True,dict(status='ALIGNED')))
        except Exception:pass

class CaptureTest(unittest.TestCase):
    def make(self, **kw):
        reader=ProcessQuoteReader(CONFIG,worker=synthetic_capture,**kw)
        self.addCleanup(reader.close)
        return reader
    def test_capture_continues_while_consumer_busy_and_preserves_fifo(self):
        reader=self.make();time.sleep(.25)
        self.assertEqual(reader.metrics()['captured'],30)
        self.assertEqual(reader.metrics()['processed'],0)
        self.assertFalse(reader.ready_for_commands())
        seen=[]
        for seq in range(1,31):
            sample=reader.read();seen.append(sample['capture']['sequence']);reader.acknowledge()
            sample['quote']['bid']='corrupted consumer copy'
        self.assertEqual(seen,list(range(1,31)))
        self.assertTrue(reader.ready_for_commands())
        self.assertNotEqual(reader.read()['quote']['bid'],'corrupted consumer copy')
        self.assertEqual(reader.align_time(),dict(status='ALIGNED'))
        reader.close();self.assertFalse(reader._process.is_alive())
    def test_overflow_is_explicit_not_latest_only_overwrite(self):
        reader=self.make(capacity=4);time.sleep(.12)
        self.assertEqual(reader.metrics()['captured'],4)
        with self.assertRaisesRegex(CaptureFailure,'OVERFLOW'):reader.read()
        self.assertEqual([reader._samples_in.items.get(timeout=1)['capture']['sequence'] for _ in range(4)],[1,2,3,4])
    def test_capture_process_death_is_visible(self):
        reader=self.make();reader._process.terminate();reader._process.join(timeout=2)
        with self.assertRaisesRegex(CaptureFailure,'EXITED'):reader.read()
    def test_invalid_interval_rejected_before_spawn(self):
        with self.assertRaises(ValueError):ProcessQuoteReader(dict(CONFIG,quote_capture_interval_seconds=0))
if __name__=='__main__':unittest.main()

class CaptureSessionOrderingTest(unittest.TestCase):
    def test_new_manual_action_waits_for_captured_backlog_without_rejection(self):
        import tempfile
        from pathlib import Path
        from tradfi.paper import PaperSession
        from test_tradfi_paper import Reader
        reader=Reader();reader.caught_up=True
        reader.ready_for_commands=lambda:reader.caught_up
        with tempfile.TemporaryDirectory() as tmp:
            session=PaperSession(Path(tmp)/'paper.db',reader,clock=lambda:reader.now,monotonic=time.monotonic)
            try:
                session.step()
                request=dict(id='capture-watermark-001',
                    context_token=session.mailbox.view()['command_context_token'],
                    revision=session.controller.state.get('control_revision',0),
                    action=dict(type='paper_costs',commission_per_lot_side='2'))
                session.mailbox.submit(request)
                reader.caught_up=False
                session.step()
                self.assertEqual(session.exchange.p['commission_per_lot_side'],'0')
                self.assertEqual(session.mailbox.requests.qsize(),1)
                reader.caught_up=True;session.step()
                self.assertEqual(session.exchange.p['commission_per_lot_side'],'2')
                self.assertEqual(session.controller.state['control_receipts'][request['id']]['status'],'APPLIED')
            finally:session.close()
    def test_fatal_capture_failure_is_not_swallowed_as_quote_wait(self):
        import tempfile
        from pathlib import Path
        from tradfi.paper import PaperSession
        from test_tradfi_paper import Reader
        reader=Reader()
        def fail():raise CaptureFailure('QUOTE_CAPTURE_OVERFLOW')
        reader.read=fail
        with tempfile.TemporaryDirectory() as tmp:
            session=PaperSession(Path(tmp)/'paper.db',reader,clock=lambda:reader.now,monotonic=time.monotonic)
            try:
                with self.assertRaises(CaptureFailure):session.step()
            finally:session.close()

# Runs the real owner loop with capability-limited fakes inside spawn.
def owned_native_capture(config, observations, status, requests, responses, stop, published, consumed, failure):
    import json, os
    from pathlib import Path
    from tradfi.paper_quote_capture import capture_loop
    def record(method):
        with Path(config['events']).open('a') as f:f.write(json.dumps([method,os.getpid()])+'\n')
    class API:
        def initialize(self,*args,**kwargs):record('initialize');return True
        def shutdown(self):record('shutdown')
    class Reader:
        def __init__(self,*args,**kwargs):self.seq=0
        def read(self):
            record('read');self.seq+=1
            return dict(tick_msc=self.seq,quote=dict(bid=str(self.seq)),candle=dict(id=self.seq),spec={})
        def view(self):return dict(quote={},candles=[])
        def align_time(self):record('align');return dict(status='ALIGNED')
    capture_loop(config, observations,status,requests,responses,stop,published,consumed,failure,api=API(),reader_factory=Reader)

class NativeOwnershipTest(unittest.TestCase):
    def test_initialize_read_sync_shutdown_share_child_owner(self):
        import json, os, tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'events.jsonl'
            reader=ProcessQuoteReader(dict(CONFIG,events=str(path)),worker=owned_native_capture)
            try:
                time.sleep(.2);reader.read();reader.align_time()
            finally:reader.close()
            events=[json.loads(s) for s in path.read_text().splitlines()]
            self.assertEqual({v[0] for v in events},{'initialize','read','align','shutdown'})
            self.assertEqual(len({v[1] for v in events}),1)
            self.assertNotEqual(events[0][1],os.getpid())
            self.assertEqual(events[0][0],'initialize');self.assertEqual(events[-1][0],'shutdown')

def capture_error_after_one(config, observations, status, requests, responses, stop, published, consumed, failure):
    observations.put(dict(quote=dict(bid='4000'),capture=dict(sequence=1,observed_monotonic=time.monotonic())))
    published.value=1
    _latest(status,dict(ready=True,sequence=1,error='MT5_CAPTURE_UNAVAILABLE'))
    stop.wait()

def capture_start_failure(config, observations, status, requests, responses, stop, published, consumed, failure):
    failure.value=2
    stop.wait()

class CaptureFailureTest(unittest.TestCase):
    def test_empty_queue_with_source_error_does_not_fake_success(self):
        reader=ProcessQuoteReader(CONFIG,worker=capture_error_after_one)
        try:
            self.assertEqual(reader.read()['quote']['bid'],'4000');reader.acknowledge()
            with self.assertRaisesRegex(OSError,'MT5_CAPTURE_UNAVAILABLE'):reader.read()
        finally:reader.close()
    def test_start_failure_cleans_up_child(self):
        import multiprocessing
        before={p.pid for p in multiprocessing.active_children()}
        with self.assertRaises(CaptureFailure):ProcessQuoteReader(CONFIG,worker=capture_start_failure)
        self.assertEqual({p.pid for p in multiprocessing.active_children()},before)

def torn_observation_frame(config, observations, status, requests, responses, stop, published, consumed, failure):
    import os, struct
    _latest(status,dict(ready=True,sequence=0))
    time.sleep(.05)
    # Simulate death midway through Queue feeder's framed payload. Poll sees a
    # header, but recv_bytes cannot return: a main-thread Queue.get would hang.
    os.write(observations._writer.fileno(),struct.pack('!i',10_000_000))
    published.value=1
    stop.wait()

class TornFrameTest(unittest.TestCase):
    @unittest.skipUnless(__import__('os').name=='posix','POSIX pipe fault injection')
    def test_torn_frame_cannot_block_strategy_owner(self):
        reader=ProcessQuoteReader(CONFIG,worker=torn_observation_frame)
        try:
            time.sleep(.1);started=time.monotonic()
            with self.assertRaises(OSError):reader.read()
            self.assertLess(time.monotonic()-started,1)
            reader._process.terminate();reader._process.join(timeout=2)
            started=time.monotonic()
            with self.assertRaises(CaptureFailure):reader.read()
            self.assertLess(time.monotonic()-started,.5)
        finally:reader.close()

class CaptureReceiptTest(unittest.TestCase):
    def test_watermark_admits_command_before_later_queued_samples(self):
        reader=ProcessQuoteReader(CONFIG,worker=synthetic_capture)
        try:
            time.sleep(.22)
            first=reader.read();reader.acknowledge()
            cutoff=first['capture']['observed_monotonic']+.015
            self.assertFalse(reader.ready_for_commands(cutoff))
            while not reader.ready_for_commands(cutoff):
                reader.read();reader.acknowledge()
            self.assertLess(reader.metrics()['processed'],reader.metrics()['captured'])
        finally:reader.close()
    def test_unacknowledged_sample_retries_without_skipping_next(self):
        reader=ProcessQuoteReader(CONFIG,worker=synthetic_capture)
        try:
            first=reader.read();again=reader.read()
            self.assertEqual(first,again)
            self.assertEqual(reader.metrics()['processed'],0)
            reader.acknowledge()
            self.assertEqual(reader.read()['capture']['sequence'],2)
        finally:reader.close()
    def test_post_receipt_unchanged_read_barrier(self):
        reader=ProcessQuoteReader(CONFIG,worker=synthetic_capture)
        try:
            time.sleep(.22)
            for _ in range(30):reader.read();reader.acknowledge()
            cutoff=time.monotonic()
            # A future read must publish a post-receipt heartbeat even though
            # current published sequence is already fully consumed.
            self.assertFalse(reader.ready_for_commands(cutoff))
            deadline=time.monotonic()+1
            while not reader.ready_for_commands(cutoff) and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(reader.ready_for_commands(cutoff))
        finally:reader.close()

class CaptureMailboxIntegrationTest(unittest.TestCase):
    def test_session_passes_receipt_time_and_drains_only_first_command(self):
        import tempfile
        from pathlib import Path
        from tradfi.paper import PaperSession
        from test_tradfi_paper import Reader
        reader=Reader();reader.command_receipt_watermark=True
        reader.cutoffs=[];reader.reached=100.;reader.acks=0
        def ready(queued_at=None):
            reader.cutoffs.append(queued_at)
            return queued_at is None or queued_at<=reader.reached
        def ack():reader.acks+=1
        reader.ready_for_commands=ready;reader.acknowledge=ack
        now=[100.]
        with tempfile.TemporaryDirectory() as tmp:
            session=PaperSession(Path(tmp)/'paper.db',reader,clock=lambda:reader.now,monotonic=lambda:now[0])
            try:
                session.step()
                for n in range(2):
                    now[0]=101.+n
                    session.mailbox.submit(dict(id='capture-mailbox-%03d'%n,
                        context_token=session.mailbox.view()['command_context_token'],
                        revision=session.controller.state.get('control_revision',0),
                        action=dict(type='paper_costs',commission_per_lot_side='2')))
                session.step()
                self.assertEqual(reader.cutoffs[-1],101.)
                self.assertEqual(session.mailbox.requests.qsize(),2)
                reader.reached=101.;session.step()
                self.assertEqual(session.mailbox.requests.qsize(),1)
                session.step()
                self.assertEqual(reader.cutoffs[-1],102.)
                self.assertEqual(session.mailbox.requests.qsize(),1)
                reader.reached=102.;session.step()
                self.assertEqual(session.mailbox.requests.qsize(),0)
                self.assertEqual(reader.acks,5)
            finally:session.close()

def valid_capture(config, observations, status, requests, responses, stop, published, consumed, failure):
    from test_tradfi_paper import Reader
    source=Reader()
    for seq in (1,2):
        source.tick(str(4000+seq),str(4001+seq))
        market=source.read();market['capture']=dict(sequence=seq,observed_monotonic=time.monotonic())
        observations.put(market);published.value=seq
        _latest(status,dict(ready=True,sequence=seq,observation_complete=True,view=market))
    stop.wait()

class CaptureControllerRetryTest(unittest.TestCase):
    def test_failed_controller_does_not_ack_or_skip_observed_sample(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from tradfi.paper import PaperSession
        reader=ProcessQuoteReader(CONFIG,worker=valid_capture)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                session=PaperSession(Path(tmp)/'paper.db',reader,clock=lambda:1793620832.,monotonic=time.monotonic)
                try:
                    with patch.object(session.controller,'tick',side_effect=RuntimeError('synthetic failure')):
                        session.step()
                    self.assertEqual(reader.metrics()['processed'],0)
                    self.assertEqual(session.exchange.market['quote']['bid'],'4001')
                    session.step()
                    self.assertEqual(reader.metrics()['processed'],1)
                    self.assertEqual(session.exchange.market['quote']['bid'],'4001')
                    session.step()
                    self.assertEqual(reader.metrics()['processed'],2)
                    self.assertEqual(session.exchange.market['quote']['bid'],'4002')
                finally:session.close()
        finally:reader.close()

class CaptureReceiverFailureTest(unittest.TestCase):
    def test_broken_receiver_is_explicit_even_while_producer_alive(self):
        from tradfi.paper_quote_capture import _Receiver
        class Broken:
            def get(self,**kw):raise EOFError('synthetic broken pipe')
        reader=ProcessQuoteReader(CONFIG,worker=synthetic_capture)
        try:
            reader._samples_in.close()
            reader._samples_in=_Receiver(Broken())
            deadline=time.monotonic()+1
            while not reader._samples_in.failure and time.monotonic()<deadline:time.sleep(.01)
            self.assertTrue(reader._process.is_alive())
            with self.assertRaisesRegex(CaptureFailure,'IPC_FAILED'):reader.read()
        finally:reader.close()

class CaptureRuntimeFaultTest(unittest.TestCase):
    def test_fatal_capture_keeps_panel_readable_without_further_source_or_strategy(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from tradfi.paper_runtime import _run_paper
        from tradfi.web_control import PaperPanel
        from test_tradfi_paper import Reader
        reader=Reader();reader.calls=0;reader.closes=0
        def read():
            reader.calls+=1
            if reader.calls>=2:raise CaptureFailure('QUOTE_CAPTURE_OVERFLOW')
            return reader.data.copy()
        def close():reader.closes+=1
        reader.read=read;reader.close=close
        panels=[];views=[]
        class Panel(PaperPanel):
            def start(self):panels.append(self)
            def close(self):pass
            def step(self):views.append(self.paper_session.view())
        class Stop:
            count=0
            def is_set(self):return self.count>=4
            def wait(self,seconds):self.count+=1
        with tempfile.TemporaryDirectory() as tmp:
            config=dict(terminal_path='fake',expected_login=1,expected_server='Bybit-Live-6',
                symbol='XAUUSD.s',state_file=str(Path(tmp)/'paper.db'))
            with patch('tradfi.paper_quote_capture.ProcessQuoteReader',return_value=reader):
                _run_paper(config,panel_factory=Panel,stop_event=Stop(),
                    clock=lambda:reader.now,monotonic=lambda:reader.now)
            self.assertEqual(reader.calls,2)
            self.assertGreaterEqual(reader.closes,1)
            self.assertEqual(len(views),4)
            self.assertTrue(all(v['paper'].get('capture_failure')=='QUOTE_CAPTURE_OVERFLOW' for v in views[1:]))
            self.assertTrue(all(v['feed']['status']=='ERROR' for v in views[1:]))
