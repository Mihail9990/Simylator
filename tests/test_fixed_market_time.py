"""Fixed profile verified against independent UTC; no MT5 installation or writes."""
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
import numpy as np
from tradfi.time_profile import (validate_market_time_offset, market_timestamp_to_utc,
                                 market_tick_to_utc, TimeProfileError)
from tradfi.native_time import NativeUTCVerifier
from tradfi.paper_market import MarketReader
from tradfi.time_safety import TimeSafety
from market_fixtures import UTCAPI

class FixedMarketTimeTest(unittest.TestCase):
    def setup_market(self, offset=7200, raw_shift=7200):
        self.api=UTCAPI();self.api.shift=raw_shift
        self.api.info=self.api.info._replace(trade_stops_level=1)
        original=self.api.copy_rates_from_pos
        self.api.copy_rates_from_pos=lambda *a:np.array([(r['time'],r['open'],r['high'],r['low'],r['close']) for r in original(*a)],dtype=[('time','i8'),('open','f8'),('high','f8'),('low','f8'),('close','f8')])
        self.api.order_send=self.api.order_check=lambda *a: self.fail('Native mutation forbidden')
        self.safety=TimeSafety(clock=lambda:self.api.now,monotonic=lambda:self.api.now)
        self.seed()
        b=SimpleNamespace(api=self.api,symbol='XAUUSD.s',monotonic=lambda:self.api.now,max_age=5,market_time_offset_seconds=offset)
        self.verifier=NativeUTCVerifier(b,self.safety);self.safety.semantics_verifier=self.verifier
    def seed(self):
        self.safety.anchor=(self.api.now,self.api.now);self.safety.result=dict(ok=True,checked_monotonic=self.api.now)
    def test_config_boundaries_and_invalid_values(self):
        self.assertEqual(validate_market_time_offset({}),0)
        for x in (-50400,50400,0,7200,-7200):self.assertEqual(validate_market_time_offset({'market_time_offset_seconds':x}),x)
        for x in (True,None,'7200',7200.,1,50460,-50460):
            with self.subTest(x=x),self.assertRaises(TimeProfileError):validate_market_time_offset({'market_time_offset_seconds':x})
    def test_numpy_conversion_and_raw_tick_consistency(self):
        self.assertEqual(market_timestamp_to_utc(np.int64(100000),7200),92800)
        for t,ms in ((100000,100001000),(100000,99999999)):
            with self.assertRaises(TimeProfileError):market_tick_to_utc(SimpleNamespace(time=t,time_msc=ms),7200)
        self.assertEqual(market_tick_to_utc(SimpleNamespace(time=100000,time_msc=100000999),7200),92800.999)
    def test_fixed_offsets_ignore_former_seasonal_boundaries(self):
        for offset in (-7200,7200):
            self.setup_market(offset,offset)
            for date in ('2026-03-08T01:59:59','2026-03-08T02:00:00','2026-11-01T01:59:59','2026-11-01T02:00:00'):
                self.api.now=datetime.fromisoformat(date).replace(tzinfo=timezone.utc).timestamp();self.seed()
                self.assertTrue(self.verifier(),(offset,date,self.verifier.reason))
                self.assertEqual(self.verifier.observation['normalized_tick_utc'],self.api.now)
                json.dumps(self.verifier.observation,allow_nan=False)
    def test_wrong_offset_stale_future_double_conversion_block(self):
        self.setup_market()
        for raw in (0,10800,7200-6,7200+3,-7200):
            self.api.shift=raw
            self.assertFalse(self.verifier(),raw)
        self.api.shift=7200;self.assertTrue(self.verifier())
        self.safety.anchor=None;self.assertFalse(self.verifier());self.assertIsNone(self.verifier.observation)
    def test_tick_candle_disagreement_blocks(self):
        self.setup_market();original=self.api.copy_rates_from_pos
        def wrong(*a):
            rows=original(*a);rows['time']+=3600;return rows
        self.api.copy_rates_from_pos=wrong
        self.assertFalse(self.verifier())


    def test_actual_paper_restart_keeps_raw_cursor_with_nonzero_offset(self):
        import tempfile
        from pathlib import Path
        from tradfi.paper import PaperSession
        self.setup_market()
        def reader():
            value=MarketReader(self.api,symbol='XAUUSD.s',clock=lambda:self.api.now,monotonic=lambda:self.api.now,safety=self.safety,expected_account='111111111@Bybit-Live-6',market_time_offset_seconds=7200)
            value.next_check=float('inf')
            return value
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'paper.db'
            session=PaperSession(path,reader(),clock=lambda:self.api.now,monotonic=lambda:self.api.now)
            try:
                session.step()
                cursor=session.exchange.p['cursor'];keys=list(session.exchange.p['cursor_keys'])
                self.assertEqual(cursor,int((self.api.now+7200)*1000))
                self.assertEqual(session.exchange.market['candle']['id'],int(self.api.now//60)*60-60)
            finally:session.close()
            session=PaperSession(path,reader(),clock=lambda:self.api.now,monotonic=lambda:self.api.now)
            try:
                self.assertEqual(session.exchange.p['cursor'],cursor)
                self.assertEqual(session.exchange.p['cursor_keys'],keys)
                session.step()
                self.assertEqual(session.exchange.p['cursor'],cursor)
                self.assertEqual(session.exchange.p['cursor_keys'],keys)
                self.assertFalse(session.exchange.p['deals'])
                self.assertFalse(hasattr(self.api,'sends'));self.assertFalse(hasattr(self.api,'checks'))
            finally:session.close()


