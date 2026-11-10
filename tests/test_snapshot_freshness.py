"""Offline publication-age and action-expiry tests using independent fake clocks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from tradfi.control import ActionGateway, OwnerMailbox, SnapshotStale
from tradfi.engine import Controller
from tradfi.simulator import Simulator
from tradfi.storage import Store
from tradfi.web_control import WebTransport


class SnapshotFreshnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'freshness.db')
        self.addCleanup(self.store.close)
        self.controller = Controller(self.store, Simulator())
        self.now = 100.0
        self.executed = []
        self.gateway = ActionGateway(self.controller, lambda action: self.executed.append(action))
        self.box = OwnerMailbox(monotonic=lambda: self.now, max_age_seconds=2)
        self.feed = dict(status='FRESH', timestamp_semantics='VERIFIED', age_seconds=0.25, reason=None,
                         quote=dict(bid='4000', ask='4000.1'), candles=[])
        self.web = WebTransport(self.box, password='local-pass', clock=lambda: 100.0)
        self.headers = {'Host': '127.0.0.1:8766', 'X-Local-Control': '1', 'Content-Type': 'application/json'}
        code, _, extra = self.web.request('POST', '/api/login', self.headers, b'{"password":"local-pass"}')
        self.assertEqual(code, 200)
        self.headers.update(Cookie=extra['Set-Cookie'].split(';')[0], **{'X-Controller-Scope': 'PAPER-test'})

    def request(self, name='fresh-request-01', revision=None, action=None):
        return dict(id=name, revision=self.controller.state.get('control_revision', 0) if revision is None else revision,
                    action=action or dict(type='command', text='/start'))

    def source(self):
        return dict(scope='PAPER-test', mode='PAPER', revision=self.controller.state.get('control_revision', 0),
                    feed=deepcopy(self.feed), action_results=deepcopy(self.controller.state.get('control_receipts', {})))

    def publish(self, limit=8):
        self.box.drain(self.gateway, self.source, limit=limit)

    def get(self):
        code, view, _ = self.web.request('GET', '/api/paper/state', self.headers)
        self.assertEqual(code, 200)
        json.dumps(view, allow_nan=False)
        return view

    def post(self, request):
        return self.web.request('POST', '/api/paper/action', self.headers, json.dumps(request).encode())

    def test_before_first_publication_is_not_fresh(self):
        self.assertEqual(self.get()['snapshot_freshness'], dict(fresh=False, age_seconds=None, max_age_seconds=2))
        with self.assertRaises(SnapshotStale):
            self.box.submit(self.request())
        self.assertTrue(self.box.requests.empty())

    def test_successful_gets_age_a_frozen_snapshot_without_mutating_it(self):
        self.publish()
        self.now += 1
        first = self.get()
        self.assertEqual(first['feed']['age_seconds'], 1.25)
        self.assertTrue(first['snapshot_freshness']['fresh'])
        self.now += 1.001
        stale = self.get()
        self.assertFalse(stale['snapshot_freshness']['fresh'])
        self.assertAlmostEqual(stale['feed']['age_seconds'], 2.251)
        self.assertEqual(stale['feed']['status'], 'WAITING')
        self.assertEqual(stale['feed']['reason'], 'SNAPSHOT_STALE')
        self.assertEqual(self.box._view['feed']['status'], 'FRESH')
        self.assertEqual(self.box._view['feed']['age_seconds'], 0.25)
        stale['feed']['quote']['bid'] = '0'
        self.assertEqual(self.get()['feed']['quote']['bid'], '4000')

    def test_snapshot_and_actual_quote_boundaries_are_separate(self):
        self.publish()
        self.now += 2
        self.assertTrue(self.get()['snapshot_freshness']['fresh'])
        self.assertEqual(self.get()['feed']['status'], 'FRESH')
        self.feed['age_seconds'] = 4.5
        self.publish()
        self.now += 0.5
        self.assertEqual(self.get()['feed']['status'], 'FRESH')
        self.now += 0.001
        view = self.get()
        self.assertTrue(view['snapshot_freshness']['fresh'])
        self.assertEqual(view['feed']['status'], 'WAITING')
        self.assertAlmostEqual(view['feed']['age_seconds'], 5.001)
        code, data, _ = self.post(self.request())
        self.assertEqual((code, data['error']), (409, 'FEED_STALE'))
        self.assertTrue(self.box.requests.empty())

    def test_verified_future_tick_tolerance_is_preserved(self):
        self.feed['age_seconds'] = -2
        self.publish()
        self.assertEqual(self.get()['feed']['status'], 'FRESH')
        self.now += 1
        self.assertEqual(self.get()['feed']['age_seconds'], -1)
        self.feed['age_seconds'] = -2.001
        self.publish()
        self.assertEqual(self.get()['feed']['status'], 'WAITING')

    def test_waiting_unverified_and_invalid_feed_ages_never_become_fresh(self):
        for overrides in (dict(status='WAITING'), dict(timestamp_semantics='UNVERIFIED'),
                          *[dict(age_seconds=age) for age in (None, True, '0', float('nan'), float('inf'), float('-inf'), -3, 10 ** 400)]):
            with self.subTest(overrides=overrides):
                self.feed.update(status='FRESH', timestamp_semantics='VERIFIED', age_seconds=0)
                self.feed.update(overrides)
                self.publish()
                self.assertEqual(self.get()['feed']['status'], 'WAITING')
                with self.assertRaises(SnapshotStale):
                    self.box.submit(self.request())

    def test_invalid_or_backwards_monotonic_clock_fails_closed(self):
        self.publish()
        for now in (None, True, '101', float('nan'), float('inf'), float('-inf'), -1, 99, 10 ** 400):
            with self.subTest(now=now):
                self.now = now
                view = self.get()
                self.assertEqual(view['snapshot_freshness'], dict(fresh=False, age_seconds=None, max_age_seconds=2))
                self.assertEqual(view['feed']['status'], 'WAITING')
                self.assertIsNone(view['feed']['age_seconds'])
                self.assertEqual(self.post(self.request())[0], 409)
        self.assertTrue(self.box.requests.empty())

    def test_invalid_publication_clock_cannot_look_fresh_later(self):
        for invalid in (float('nan'), float('inf'), -1):
            self.now = invalid
            self.publish()
            self.now = 101
            self.assertFalse(self.get()['snapshot_freshness']['fresh'])
            self.assertEqual(self.get()['feed']['status'], 'WAITING')

    def test_slow_view_construction_counts_toward_quote_age(self):
        self.feed['age_seconds'] = 4.5
        def slow_source():
            view = self.source()
            self.now += 1
            return view
        self.box.drain(self.gateway, slow_source)
        view = self.get()
        self.assertEqual(view['snapshot_freshness'], dict(fresh=True, age_seconds=0, max_age_seconds=2))
        self.assertEqual(view['feed']['age_seconds'], 5.5)
        self.assertEqual(view['feed']['status'], 'WAITING')

    def test_stale_http_action_is_rejected_before_enqueuing(self):
        self.publish()
        self.now += 2.001
        request = self.request()
        before = deepcopy(self.controller.state)
        code, data, _ = self.post(request)
        self.assertEqual((code, data['error']), (409, 'SNAPSHOT_STALE'))
        self.assertTrue(self.box.requests.empty())
        self.assertEqual(before, self.controller.state)
        self.assertFalse(self.executed)

    def test_queued_action_expires_before_owner_resumes_and_rejection_is_durable(self):
        self.publish()
        request = self.request()
        self.assertEqual(self.post(request)[0], 202)
        self.now += 3
        # A new market read could be fresh now; it cannot renew the queued preview.
        self.feed['age_seconds'] = 0
        self.publish()
        self.assertFalse(self.executed)
        result = self.get()['action_results'][request['id']]
        self.assertEqual(result['status'], 'REJECTED')
        self.assertIn('устарели', result['reason'])
        self.assertEqual(self.store.load()['control_receipts'][request['id']]['status'], 'REJECTED')
        self.assertEqual(self.gateway.apply(request)['status'], 'REJECTED')
        self.assertFalse(self.executed)
        self.assertEqual(self.controller.state['control_revision'], 1)

    def test_later_publication_does_not_renew_a_queued_preview(self):
        self.publish()
        request = self.request()
        self.box.submit(request)
        self.now += 1.5
        self.publish(limit=0)
        self.now += 1
        self.assertTrue(self.get()['snapshot_freshness']['fresh'])
        self.publish()
        self.assertEqual(self.get()['action_results'][request['id']]['status'], 'REJECTED')
        self.assertFalse(self.executed)

    def test_guard_checks_after_slow_durable_reservation(self):
        self.publish()
        request = self.request()
        self.box.submit(request)
        def slow_save(event):
            if event == 'control_action_reserved':
                self.now += 3
        self.store.fault = slow_save
        self.publish()
        self.assertEqual(self.get()['action_results'][request['id']]['status'], 'REJECTED')
        self.assertFalse(self.executed)

    def test_fresh_republication_allows_a_new_request_not_an_expired_one(self):
        self.publish()
        self.now += 3
        self.publish()
        request = self.request()
        self.assertEqual(self.post(request)[0], 202)
        self.publish()
        self.assertEqual(self.get()['action_results'][request['id']]['status'], 'APPLIED')
        self.assertEqual(len(self.executed), 1)

    def test_exact_stop_and_time_recheck_remain_available_while_stale(self):
        self.publish()
        self.now += 3
        self.feed.update(status='WAITING', timestamp_semantics='UNVERIFIED', age_seconds=None)
        for index, text in enumerate(('/stop', '/resume_time')):
            request = self.request(name='safe-request-%02d' % index, action=dict(type='command', text=text))
            self.assertEqual(self.post(request)[0], 202)
            self.publish()
            self.assertEqual(self.get()['action_results'][request['id']]['status'], 'APPLIED')
            self.now += 3
        self.assertEqual([action['text'] for action in self.executed], ['/stop', '/resume_time'])
        for action in (dict(type='paper_resume'), dict(type='paper_flatten', confirmed=True), dict(type='paper_interrupt'),
                       dict(type='command', text='/stop', extra=True), dict(type='command', text='/resume_time', extra=True)):
            self.assertEqual(self.post(self.request(action=action))[0], 409)
        self.assertTrue(self.box.requests.empty())

    def test_known_durable_applied_unknown_or_rejected_receipts_do_not_reexecute(self):
        for index, status in enumerate(('APPLIED', 'UNKNOWN', 'REJECTED')):
            with self.subTest(status=status):
                self.publish()
                request = self.request(name='receipt-request-%02d' % index)
                self.gateway.apply(request)
                self.controller.state['control_receipts'][request['id']]['status'] = status
                self.controller.save('fixture_receipt')
                self.publish()
                self.now += 3
                before = deepcopy(self.controller.state)
                count = len(self.executed)
                code, data, _ = self.post(request)
                self.assertEqual((code, data['status']), (202, status))
                self.assertTrue(self.box.requests.empty())
                conflict = dict(request, action=dict(type='command', text='/stop'))
                self.assertEqual(self.post(conflict)[0], 409)
                self.assertEqual(before, self.controller.state)
                self.assertEqual(len(self.executed), count)
                self.assertEqual(self.get()['action_results'][request['id']]['status'], status)

    def test_duplicate_queued_before_receipt_publication_keeps_unknown(self):
        self.publish()
        request = self.request()
        self.box.submit(request)
        self.box.submit(request)
        def uncertain(action):
            self.executed.append(action)
            self.now += 3
            raise TimeoutError('unknown after effect')
        self.gateway.execute = uncertain
        self.publish()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(self.get()['action_results'][request['id']]['status'], 'UNKNOWN')
        self.assertEqual(self.store.load()['control_receipts'][request['id']]['status'], 'UNKNOWN')
        self.assertEqual(self.controller.state['control_revision'], 1)

    def test_paper_session_refresh_cannot_execute_an_expired_queued_action(self):
        from test_tradfi_paper import Reader
        from tradfi.paper import PaperSession
        reader = Reader()
        paper = PaperSession(Path(self.tmp.name) / 'paper-session.db', reader,
                             clock=lambda: reader.now, monotonic=lambda: self.now)
        self.addCleanup(paper.close)
        paper.step()
        self.assertTrue(paper.mailbox.view()['snapshot_freshness']['fresh'])
        request = dict(id='paper-expiry-001', revision=0, action=dict(type='paper_costs', commission_per_lot_side='2'))
        paper.mailbox.submit(request)
        self.now += 3
        reader.tick('4000', '4000.1', seconds=3)
        paper.step()
        self.assertEqual(paper.exchange.p['commission_per_lot_side'], '0')
        self.assertEqual(paper.mailbox.view()['action_results'][request['id']]['status'], 'REJECTED')
        self.assertTrue(paper.mailbox.view()['snapshot_freshness']['fresh'])
        self.assertEqual(paper.mailbox.view()['feed']['status'], 'FRESH')
        paper.mailbox.submit(dict(request, id='paper-fresh-002', revision=1))
        paper.step()
        self.assertEqual(paper.exchange.p['commission_per_lot_side'], '2')
        self.assertEqual(paper.mailbox.view()['action_results']['paper-fresh-002']['status'], 'APPLIED')

    def test_new_waiting_publication_blocks_an_earlier_queued_request(self):
        self.publish()
        request = self.request()
        self.box.submit(request)
        self.now += 0.5
        self.feed['status'] = 'WAITING'
        self.publish(limit=0)
        self.publish()
        self.assertEqual(self.get()['action_results'][request['id']]['status'], 'REJECTED')
        self.assertFalse(self.executed)


    def test_action_guards_do_not_copy_large_presentation_history(self):
        class HistoryMustNotBeCopied:
            def __deepcopy__(self, memo):
                raise AssertionError('Action guard copied presentation history')
        self.publish()
        self.box._view['position_history'] = HistoryMustNotBeCopied()
        request = self.request()
        self.box.submit(request)
        self.publish()
        self.assertEqual(len(self.executed), 1)
        self.assertEqual(self.get()['action_results'][request['id']]['status'], 'APPLIED')
