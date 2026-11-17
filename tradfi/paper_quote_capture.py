"""PAPER quote acquisition isolated from controller, database and web work.

Production uses a spawned process and one MT5 owner. FIFO contains observed
samples, not a promise to receive every exchange tick. No latest-only overwrite.
Overflow is explicit and terminal for this capture run. Pending facts remain
only until capture shutdown; the volatile queue is not a crash-replay journal.
"""
from copy import deepcopy
import importlib
import multiprocessing
from queue import Empty, Full, Queue
import time
import threading


class CaptureFailure(OSError):
    pass


class _StopSignal:
    """Single-writer flag: shutdown cannot inherit a dead child's Event lock."""
    def __init__(self, value):self.value=value
    def is_set(self):return bool(self.value.value)
    def set(self):self.value.value=True
    def wait(self, timeout=None):
        until=None if timeout is None else time.monotonic()+max(0,timeout)
        while not self.is_set():
            remaining=.02 if until is None else until-time.monotonic()
            if remaining<=0:return False
            time.sleep(min(.02,remaining))
        return True


class _Receiver:
    """A torn multiprocessing frame can block recv_bytes despite Queue timeout.

    Only this daemon helper touches that pipe; the strategy owner reads a local
    Queue with real bounded waits and can detect producer death independently.
    """
    def __init__(self, source, *, latest=False):
        self.source=source;self.latest=latest;self.items=Queue(1)
        self.stop=threading.Event();self.failure=None
        self.thread=threading.Thread(target=self._run,name='paper-capture-receiver',daemon=True)
        self.thread.start()
    def _run(self):
        while not self.stop.is_set():
            try:item=self.source.get(timeout=.1)
            except Empty:continue
            except Exception as exc:
                if not self.stop.is_set():self.failure=type(exc).__name__
                return
            while not self.stop.is_set():
                try:self.items.put(item,timeout=.1);break
                except Full:
                    if self.latest:
                        try:self.items.get_nowait()
                        except Empty:pass
    def close(self):
        self.stop.set()  # Never join a possibly torn native pipe frame.


def _latest(status, item):
    # Only diagnostics/display may coalesce, never execution observations.
    item=dict(item, generated_monotonic=time.monotonic())
    try: status.put_nowait(item)
    except Full:
        try: status.get_nowait()
        except Empty: pass
        try: status.put_nowait(item)
        except Full: pass


def capture_loop(config, observations, status, requests, responses, stop,
                 published, consumed, failure, *, api=None, reader_factory=None):
    """All native capabilities, including initialization and shutdown, live here."""
    from .paper_market import RawMarketReader
    api = api if api is not None else importlib.import_module('MetaTrader5')
    seq = 0
    fingerprint = None
    try:
        if not api.initialize(config['terminal_path'], timeout=10000):
            raise OSError('MT5 initialize failed')
        reader = (reader_factory or RawMarketReader)(api, symbol=config['symbol'],
            clock=time.time, monotonic=time.monotonic,
            expected_account=str(config['expected_login'])+'@'+config['expected_server'],
            market_time_offset_seconds=0)
        _latest(status, dict(ready=True, sequence=0))
        interval = float(config.get('quote_capture_interval_seconds', .1))
        while not stop.is_set():
            started = time.monotonic()
            try:
                request = requests.get_nowait()
            except Empty: request = None
            if request is not None:
                rid, command = request
                try:
                    if command != 'align': raise ValueError('Unknown capture command')
                    result = reader.align_time()
                    try: responses.put_nowait((rid, True, result))
                    except Full: pass  # stale display RPC must never block quotes
                except Exception:
                    try: responses.put_nowait((rid, False, None))
                    except Full: pass
            try:
                market = reader.read()
                # Distinct quotes in one millisecond and closed-candle changes
                # are distinct facts. Do not collapse a rapid out-and-back.
                identity = repr((market.get('tick_msc'), market.get('quote'),
                                 market.get('candle'), market.get('spec')))
                if identity != fingerprint:
                    sample = deepcopy(market)
                    sample['capture'] = dict(sequence=seq+1,
                        observed_monotonic=time.monotonic())
                    try:
                        if seq-consumed.value >= config.get('_capture_capacity',4096):raise Full
                        observations.put_nowait(sample)
                    except Full:
                        failure.value = 1
                        _latest(status, dict(ready=True, fatal='QUOTE_CAPTURE_OVERFLOW',
                            sequence=seq, view=reader.view()))
                        # Keep the process alive to retain its queue. No further
                        # sampling until explicit service restart, no false fills.
                        stop.wait()
                        return
                    seq += 1
                    published.value = seq
                    fingerprint = identity
                _latest(status, dict(ready=True, sequence=seq, observation_complete=True, view=reader.view(),
                    capture_ms=(time.monotonic()-started)*1000))
            except Exception:
                _latest(status, dict(ready=True, sequence=seq, observation_complete=True,
                    error='MT5_CAPTURE_UNAVAILABLE', view=reader.view()))
            stop.wait(max(0, interval-(time.monotonic()-started)))
    except Exception:
        failure.value = 2
        _latest(status, dict(fatal='QUOTE_CAPTURE_START_FAILED', sequence=seq))
    finally:
        api.shutdown()
        # Closing the service does not promise replay of its volatile queue.
        # Do not wait forever for a dead consumer to drain pipe feeder threads.
        for channel in (observations, status, responses):
            if hasattr(channel, 'cancel_join_thread'):channel.cancel_join_thread()


class _DisplayClock:
    def __init__(self, capture): self.capture = capture
    def view(self):
        self.capture._refresh_status()
        value = deepcopy(self.capture._status.get('view', {}).get('display_clock', {}))
        if self.capture._closed:
            return dict(value,status='DISCONNECTED',source_connected=False,estimated_broker_time=None)
        if value.get('status') == 'ALIGNED' and value.get('estimated_broker_time') is not None:
            elapsed = max(0, time.monotonic()-self.capture._status_at)
            value['estimated_broker_time'] += elapsed
            if value.get('elapsed_seconds') is not None: value['elapsed_seconds'] += elapsed
        return value


class ProcessQuoteReader:
    market_time_mode = 'mt5_raw_v1'
    command_receipt_watermark = True
    market_time_offset_seconds = 0
    def __init__(self, config, *, context=None, worker=capture_loop, capacity=4096):
        self.config = dict(config, _capture_capacity=capacity)
        self.capacity = capacity
        interval = float(config.get('quote_capture_interval_seconds', .1))
        if not .01 <= interval <= 1: raise ValueError('Invalid quote capture interval')
        if not isinstance(capacity, int) or capacity < 1: raise ValueError('Invalid capture capacity')
        ctx = context or multiprocessing.get_context('spawn')
        self._observations = ctx.Queue(capacity)
        self._updates = ctx.Queue(8)
        self._requests = ctx.Queue(8)
        self._responses = ctx.Queue(8)
        self._stop = _StopSignal(ctx.Value('b', False, lock=False))
        self._published = ctx.Value('Q', 0, lock=False)
        self._failure = ctx.Value('i', 0, lock=False)
        self._processed = ctx.Value('Q', 0, lock=False)
        self._process = ctx.Process(target=worker, args=(self.config, self._observations,
            self._updates, self._requests, self._responses, self._stop, self._published, self._processed, self._failure),
            name='paper-mt5-capture', daemon=True)
        self._expected_account = str(config['expected_login'])+'@'+config['expected_server']
        self.reason = 'MT5_CAPTURE_STARTING'
        self._closed = False
        self._status = {}; self._status_at = time.monotonic()
        self._consumed = 0; self._acked_at = None; self._last = None; self._request_id = 0
        self.display_clock = _DisplayClock(self)
        self._process.start()
        # Durable identity allows the installer to stop a capture orphan without
        # touching MT5 or guessing from a generic Python process name.
        from .managed import managed_root
        root=managed_root()
        if root is not None:
            from install_tradfi import register_process_ownership, SetupError
            try:register_process_ownership(root,self._process.pid,'quote_worker')
            except (SetupError,OSError):pass  # Exact own-venv spawn argv remains independently verifiable.
        self._samples_in = _Receiver(self._observations)
        self._updates_in = _Receiver(self._updates, latest=True)
        self._responses_in = _Receiver(self._responses)
        deadline = time.monotonic()+15
        try:
            while not self._status.get('ready'):
                self._refresh_status()
                self._check_failure()
                if time.monotonic() >= deadline:
                    raise TimeoutError('QUOTE_CAPTURE_START_TIMEOUT')
                time.sleep(.01)
        except BaseException:
            self.close()
            raise

    def _refresh_status(self):
        # A terminated producer may leave a Queue frame incomplete. Never read
        # its pipe after death; shared failure flag remains nonblocking.
        if not self._process.is_alive():return
        while True:
            try:
                self._status = self._updates_in.items.get_nowait()
                self._status_at = self._status.get('generated_monotonic', time.monotonic())
            except Empty: break
        self.reason = self._status.get('fatal') or self._status.get('error')

    def _check_failure(self):
        if any(receiver.failure for receiver in (self._samples_in,self._updates_in,self._responses_in)):
            raise CaptureFailure('QUOTE_CAPTURE_IPC_FAILED')
        if self._failure.value: raise CaptureFailure('QUOTE_CAPTURE_OVERFLOW' if self._failure.value == 1 else 'QUOTE_CAPTURE_START_FAILED')
        if self._status.get('fatal'): raise CaptureFailure(self._status['fatal'])
        if not self._process.is_alive():
            raise CaptureFailure('QUOTE_CAPTURE_EXITED')

    def read(self):
        self._refresh_status(); self._check_failure()
        if self._consumed > self._processed.value:return deepcopy(self._last)
        try: sample = self._samples_in.items.get(timeout=.2)
        except Empty:
            self._refresh_status(); self._check_failure()
            if self._status.get('error'): raise OSError(self._status['error'])
            if self._last is None: raise OSError('MT5_CAPTURE_WAIT_FIRST_SAMPLE')
            return deepcopy(self._last)
        try:seq = sample['capture']['sequence']
        except (KeyError,TypeError):raise CaptureFailure('QUOTE_CAPTURE_INVALID_SAMPLE') from None
        if type(seq) is not int:raise CaptureFailure('QUOTE_CAPTURE_INVALID_SAMPLE')
        if seq != self._consumed+1: raise CaptureFailure('QUOTE_CAPTURE_SEQUENCE_GAP')
        self._consumed = seq; self._last = sample
        return deepcopy(sample)

    def acknowledge(self):
        """Owner calls only after exchange/controller successfully process sample."""
        self._processed.value = self._consumed
        self._acked_at=(self._last or {}).get('capture',{}).get('observed_monotonic')

    def ready_for_commands(self, queued_at=None):
        self._refresh_status(); self._check_failure()
        if queued_at is None:return self._processed.value >= self._published.value
        observed=self._acked_at
        if observed is not None and observed >= queued_at:return True
        # A post-receipt completed read acts as a barrier even for an unchanged
        # quote. Merely catching current published count is insufficient: a
        # native read may still be in flight before it publishes its sample.
        return bool(self._status.get('observation_complete') and
            self._status.get('generated_monotonic',0) >= queued_at and
            self._processed.value >= self._status.get('sequence',0))

    def metrics(self):
        pending=max(0, self._published.value-self._processed.value)
        observed=self._acked_at
        lag=max(0,time.monotonic()-observed) if pending and observed is not None else 0
        return dict(captured=self._published.value, processed=self._processed.value,
            pending=pending, capacity=self.capacity, processing_lag_seconds=lag,
            read_ms=self._status.get('capture_ms'),
            interval_seconds=float(self.config.get('quote_capture_interval_seconds',.1)),
            mode='ISOLATED_PROCESS', sampling='OBSERVED_SAMPLES', reason=self.reason)

    def view(self):
        self._refresh_status()
        value = deepcopy(self._status.get('view') or self._last or
            dict(source='MT5_READ_ONLY',symbol=self.config['symbol'],quote={},candles=[],timestamp=None))
        value['capture'] = self.metrics()
        if self.reason: value.update(status='WAITING', reason=self.reason)
        return value

    def align_time(self):
        self._request_id += 1; rid = self._request_id
        self._requests.put_nowait((rid, 'align'))
        deadline = time.monotonic()+3
        while time.monotonic() < deadline:
            self._refresh_status(); self._check_failure()
            try: response_id, ok, value = self._responses_in.items.get(timeout=.1)
            except Empty: continue
            if response_id != rid: continue
            if not ok: raise OSError('MT5_CAPTURE_ALIGN_FAILED')
            return value
        raise TimeoutError('MT5_CAPTURE_ALIGN_TIMEOUT')

    def close(self):
        if self._closed:return
        self._closed = True
        for receiver in (self._samples_in,self._updates_in,self._responses_in):receiver.close()
        # A killed producer may have held a multiprocessing Event lock. Do not
        # touch shared synchronization primitives after observed process death.
        if self._process.is_alive():
            self._stop.set()
            self._process.join(timeout=3)
            if self._process.is_alive():
                self._process.terminate(); self._process.join(timeout=2)
        for queue in (self._observations, self._updates, self._requests, self._responses):
            queue.cancel_join_thread(); queue.close()
