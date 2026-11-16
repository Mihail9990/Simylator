"""PAPER uses broker coordinates, never the VPS clock, to observe M1."""
import tempfile
import unittest
from pathlib import Path
from market_fixtures import UTCAPI
from tradfi.paper_market import RawMarketReader
from tradfi.paper import PaperSession
from tradfi.paper_commands import quote_info
from tradfi.engine import Controller

class PaperRawTimeTests(unittest.TestCase):
    def make_reader(self,api,wall=None):
        return RawMarketReader(api,symbol='XAUUSD.s',clock=wall or (lambda:api.now),monotonic=lambda:api.now,
            expected_account='111111111@Bybit-Live-6')
    def test_broker_ahead_of_vps_no_offset_or_utc_proof(self):
        api=UTCAPI();api.shift=7200
        reader=self.make_reader(api,lambda:100)
        reader.safety.check=lambda: self.fail('must not check UTC reference')
        value=reader.read()
        self.assertEqual(value['timestamp'],api.now+7200)
        self.assertEqual(value['timestamp_semantics'],'BROKER_RAW')
        self.assertIsNone(value['quote']['utc_time'])
        self.assertTrue(Controller._fresh_closed_candle(dict(candle=value['candle'],now=100)))
        self.assertEqual(reader.view()['status'],'OBSERVED')
        self.assertIsNone(reader.view()['age_seconds'])
    def test_session_and_commands_accept_broker_coordinate(self):
        api=UTCAPI();api.shift=7200
        with tempfile.TemporaryDirectory() as tmp:
            session=PaperSession(Path(tmp)/'paper.db',self.make_reader(api),clock=lambda:api.now,monotonic=lambda:api.now)
            try:
                session.step()
                self.assertIsNotNone(session.snapshot)
                self.assertTrue(quote_info(session.exchange)['available'])
                self.assertEqual(quote_info(session.exchange)['source'],'LAST_OBSERVED_MT5')
                self.assertIsNone(quote_info(session.exchange)['age_seconds'])
                api.now+=1000
                session.step()
                self.assertIsNotNone(session.snapshot)
            finally:session.close()
    def test_cached_tick_and_reconnect_not_a_wall_clock_age_failure(self):
        api=UTCAPI();reader=self.make_reader(api)
        reader.read();sequence=reader.raw_time.sequence
        reader.read()
        self.assertEqual(sequence,reader.raw_time.sequence)
        api.unavailable=True
        with self.assertRaises(OSError):reader.read()
        api.unavailable=False
        value=reader.read()
        self.assertEqual(value['status'],'OBSERVED')
        self.assertGreater(reader.raw_time.sequence,sequence)

    def test_existing_nonzero_binding_is_not_silently_reinterpreted(self):
        from tradfi.storage import Store
        from tradfi.market_time_state import bind_market_time_state, MarketTimeStateError
        api=UTCAPI()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'paper.db';store=Store(path)
            bind_market_time_state(store,dict(market_time_offset_seconds=7200,expected_login=111111111,expected_server='Bybit-Live-6'))
            store.close()
            with self.assertRaises(MarketTimeStateError):
                PaperSession(path,self.make_reader(api),clock=lambda:api.now,monotonic=lambda:api.now)

    def test_display_alignment_monotonic_not_wall(self):
        api=UTCAPI();reader=self.make_reader(api,lambda:-999)
        reader.read();self.assertEqual(reader.view()['display_clock']['status'],'UNALIGNED')
        reader.align_time();anchor=reader.view()['display_clock']['broker_time']
        api.now+=12
        self.assertEqual(reader.view()['display_clock']['estimated_broker_time'],anchor+12)
        api.unavailable=True
        with self.assertRaises(OSError):reader.read()
        self.assertEqual(reader.view()['display_clock']['status'],'DISCONNECTED')
    def test_explicit_sync_does_not_change_trading_state_and_reconnect_requires_sync(self):
        from copy import deepcopy
        api=UTCAPI();reader=self.make_reader(api)
        with tempfile.TemporaryDirectory() as tmp:
            session=PaperSession(Path(tmp)/'paper.db',reader,clock=lambda:api.now,monotonic=lambda:api.now)
            try:
                session.step()
                before=deepcopy(session.exchange.p)
                phase=session.controller.state['phase']
                session.action(dict(type='command',text='/sync_mt5_time'))
                self.assertEqual(session.view()['time_status']['display_clock']['status'],'ALIGNED')
                self.assertEqual(session.view()['time_status']['market_time_mode'],'mt5_raw_v1')
                self.assertEqual(session.exchange.p,before)
                self.assertEqual(session.controller.state['phase'],phase)
                api.unavailable=True
                session.step()
                self.assertEqual(session.view()['time_status']['display_clock']['status'],'DISCONNECTED')
                api.unavailable=False
                session.step()
                self.assertEqual(session.view()['time_status']['display_clock']['status'],'UNALIGNED')
                self.assertTrue(quote_info(session.exchange)['available'])
            finally:session.close()

    def test_display_sync_needs_only_identity_tick_and_monotonic_clock(self):
        api=UTCAPI();reader=self.make_reader(api,lambda:self.fail('No Windows clock'))
        reader._bars=lambda *args:self.fail('Display sync must not require candles')
        reader.safety.check=lambda:self.fail('No UTC/freshness gate')
        reader.align_time()
        self.assertEqual(reader.display_clock.view()['status'],'ALIGNED')
        reader._expected_account='different@Bybit-Live-6'
        with self.assertRaises(OSError):reader.align_time()
        self.assertEqual(reader.display_clock.view()['status'],'DISCONNECTED')

    def test_invalid_tick_or_offset_rejected(self):
        api=UTCAPI()
        with self.assertRaises(ValueError):RawMarketReader(api,symbol='XAUUSD.s',clock=lambda:api.now,monotonic=lambda:api.now,market_time_offset_seconds=7200)
        original=api.symbol_info_tick
        api.symbol_info_tick=lambda s:original(s)._replace(ask=3999)
        with self.assertRaises(ValueError):self.make_reader(api).read()
    def test_nonadjacent_closed_bars_after_break_are_valid(self):
        api=UTCAPI();original=api.copy_rates_from_pos
        def bars(*a):
            values=original(*a)
            for item in values[:-1]:item['time']-=3600
            return values
        api.copy_rates_from_pos=bars
        value=self.make_reader(api).read()
        self.assertTrue(Controller._fresh_closed_candle(dict(candle=value['candle'])))

if __name__=='__main__':unittest.main()
