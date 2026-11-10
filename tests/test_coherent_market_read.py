"""Synthetic read-only rollover races; no native trading capabilities."""
import unittest
from types import SimpleNamespace
from market_fixtures import UTCAPI
from tradfi.native_time import NativeUTCVerifier
from tradfi.paper_market import MarketReader

class CoherentMarketReadTest(unittest.TestCase):
    def setup_api(self):
        self.api=UTCAPI()
        self.api.now=int(self.api.now//60)*60+59.999
        self.api.delay=.005
        return self.api
    def verifier(self):
        a=self.api
        return NativeUTCVerifier(SimpleNamespace(api=a,symbol='XAUUSD.s',monotonic=lambda:a.now,max_age=5),SimpleNamespace(trusted_utc=lambda:a.now))
    def test_verifier_rollover_recovers_once(self):
        a=self.setup_api();v=self.verifier()
        self.assertTrue(v());self.assertEqual(a.reads,4)
    def test_verifier_persistent_previous_tick_still_blocks(self):
        a=self.setup_api();tick=a.symbol_info_tick('XAUUSD.s');a.now+=.1
        a.symbol_info_tick=lambda _:tick
        v=self.verifier();self.assertFalse(v());self.assertEqual(v.reason,'MT5_UTC_WAIT_FRESH_DATA')
    def test_verifier_combined_timeout_not_reset(self):
        a=self.setup_api();a.delay=1.1
        v=self.verifier();self.assertFalse(v());self.assertEqual(v.reason,'MT5_UTC_READ_TIMEOUT')
    def test_verifier_nonadjacent_mismatch_not_retried(self):
        a=self.setup_api();a.delay=0;original=a.copy_rates_from_pos
        def wrong(*args):
            rows=original(*args)
            for row in rows:row['time']+=3600
            return rows
        a.copy_rates_from_pos=wrong
        v=self.verifier();self.assertFalse(v());self.assertEqual(a.reads,2)
    def reader(self):
        a=self.api
        safety=SimpleNamespace(poll_refresh=lambda:None,check=lambda:None,trusted_utc=lambda:a.now)
        r=MarketReader(a,symbol='XAUUSD.s',clock=lambda:a.now,monotonic=lambda:a.now,safety=safety)
        r.next_check=float('inf')
        # Isolate verified read's tick/bars race, after display-only read.
        r.read_raw=lambda:None
        return r
    def test_reader_rollover_recovers_once(self):
        a=self.setup_api();r=self.reader();result=r.read()
        self.assertEqual(result['status'],'FRESH');self.assertEqual(a.reads,4)
        self.assertEqual(result['candles'][-1]['utc_time'],int(result['timestamp']//60)*60)
    def test_reader_combined_timeout_not_reset(self):
        a=self.setup_api();a.delay=1.1;r=self.reader()
        with self.assertRaisesRegex(OSError,'STALE_MT5'):r.read()
        self.assertIsNone(r.last)
    def test_reader_persistent_mismatch_still_blocks(self):
        a=self.setup_api();a.delay=0;original=a.copy_rates_from_pos
        def delayed(*args):
            rows=original(*args)
            for row in rows:row['time']-=60
            return rows
        a.copy_rates_from_pos=delayed;r=self.reader()
        with self.assertRaisesRegex(OSError,'WAIT_FRESH_M1'):r.read()
        self.assertEqual(a.reads,4);self.assertIsNone(r.last)
