"""Owner-thread command gateway for the authenticated web UI.

A durable receipt precedes execution. A crash between execution and completion
leaves UNKNOWN, never permission to replay. No worker accesses Controller/SQLite.
"""
from copy import deepcopy
import hashlib
import json
import math
import queue
import re
import threading
import time


class Conflict(ValueError):
    pass


class ActionRejected(ValueError):
    """Executor positively established that the requested action was refused."""
    def __init__(self,message,*,public_message=None):
        super().__init__(message)
        self.public_message=public_message


class SnapshotStale(ActionRejected):
    """The cached preview no longer permits a new PAPER action."""
    def __init__(self, code):
        super().__init__(code, public_message='Снимок или котировка устарели. Дождитесь свежих данных и подтвердите действие заново.')
        self.code = code


def _request_digest(request):
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {'id', 'revision', 'action'}:
        raise ValueError('Request requires id, revision, action')
    if not isinstance(request['id'], str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}', request['id']):
        raise ValueError('Invalid action id')
    revision = request['revision']
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError('Invalid revision')
    if not isinstance(request['action'], dict):
        raise ValueError('Invalid action')
    try:
        encoded = json.dumps(request, sort_keys=True, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError) as exc:
        raise ValueError('Action must contain finite JSON values') from exc
    if len(encoded.encode('utf-8')) > 8192:
        raise ValueError('Action too large')
    return json.loads(encoded)


class ActionGateway:
    def __init__(self, controller, execute=None):
        self.controller = controller
        self.owner = threading.get_ident()
        self.execute = execute or self._execute
        self.diagnostics = None

    def _execute(self, action):
        if set(action) != {'type', 'text'} or action['type'] != 'command' or not isinstance(action['text'], str):
            raise ActionRejected('Unsupported action')
        # Only the controller's typed, pre-effect refusals are safe to classify.
        # Generic ValueError can follow a durable effect and must remain UNKNOWN.
        self.controller.command(action['text'])

    def apply(self, request, *, pre_execute=None):
        if threading.get_ident() != self.owner:
            raise RuntimeError('Controller actions require the owner thread')
        request = validate_request(request)
        state = self.controller.state
        digest = _request_digest(request)
        receipts = state.get('control_receipts', {})
        previous = receipts.get(request['id'])
        if previous:
            if previous['digest'] != digest:
                if self.diagnostics:self.diagnostics.action('action_conflict', request, status='CONFLICT')
                raise Conflict('Action id reused with different payload')
            if self.diagnostics:self.diagnostics.action('action_replayed', request, status=previous['status'], revision=previous['revision'])
            return deepcopy(previous)
        revision = state.get('control_revision', 0)
        if request['revision'] != revision:
            if self.diagnostics:self.diagnostics.action('action_conflict', request, status='CONFLICT', revision=revision)
            raise Conflict('State changed; refresh preview before Apply')
        receipt = dict(digest=digest, status='UNKNOWN', revision=revision+1)
        if request['action'].get('type') == 'paper_interrupt' and state.get('backend') == 'paper_mt5_quotes_v1':
            # This action commits its receipt with the entire virtual settlement.
            # A crash immediately after reservation is therefore safe to reconcile.
            receipt['paper_interrupt_atomic'] = True
        state.setdefault('control_receipts', {})[request['id']] = receipt
        state['control_revision'] = revision + 1
        self.controller.save('control_action_reserved')
        self.controller._active_control_id=request['id']
        started = time.perf_counter()
        error_type = None
        if self.diagnostics:self.diagnostics.action('action_reserved', request, status='UNKNOWN', revision=revision+1)
        try:
            # Recheck after reservation, immediately before effects. Existing
            # receipts return above and can never be relabelled or replayed.
            if pre_execute is not None:
                pre_execute(request['action'])
            self.execute(deepcopy(request['action']))
        except ActionRejected as exc:
            status = 'REJECTED'
            error_type = type(exc).__name__
            if exc.public_message is not None:receipt['reason']=str(exc.public_message)[:400]
        except (ValueError, OSError, TimeoutError) as exc:
            status = 'UNKNOWN'
            error_type = type(exc).__name__
        else:
            status = 'APPLIED'
        finally:
            self.controller._active_control_id=None
        # The command may have replaced the in-memory state while reconciling.
        receipt = self.controller.state['control_receipts'][request['id']]
        receipt['status'] = status
        self.controller.save('control_action_completed')
        if self.diagnostics:self.diagnostics.action('action_completed', request, status=status, revision=receipt['revision'], error_type=error_type, duration_ms=round((time.perf_counter()-started)*1000,3))
        return deepcopy(receipt)


class OwnerMailbox:
    """Transport workers only queue immutable requests and read copied views.

    PAPER opts into a monotonic publication deadline. Generic controller mailboxes
    retain their original behavior; neither HTTP workers nor this projection read
    MT5, SQLite or mutable controller state.
    """
    def __init__(self, capacity=32, *, monotonic=time.monotonic, max_age_seconds=None):
        if max_age_seconds is not None and (self._nonnegative(max_age_seconds) is None or max_age_seconds <= 0):
            raise ValueError('Snapshot age limit must be finite and positive')
        self.requests = queue.Queue(maxsize=capacity)
        self.lock = threading.Lock()
        self._view = {'status': 'STARTING'}
        self._published_at = None
        self._receipts = {}
        self.monotonic = monotonic
        self.max_age_seconds = max_age_seconds
        self.outcomes = {}

    @staticmethod
    def _finite(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            return float(value) if math.isfinite(value) else None
        except (OverflowError, ValueError):
            return None

    @classmethod
    def _nonnegative(cls, value):
        value = cls._finite(value)
        return value if value is not None and value >= 0 else None

    def _aged_view(self, view, published_at, now):
        result = deepcopy(view)
        if self.max_age_seconds is None:
            return result
        published_at, now = self._nonnegative(published_at), self._nonnegative(now)
        elapsed = self._nonnegative(now - published_at) if now is not None and published_at is not None else None
        fresh = elapsed is not None and elapsed <= self.max_age_seconds
        result['snapshot_freshness'] = dict(fresh=fresh, age_seconds=elapsed, max_age_seconds=self.max_age_seconds)
        feed = result.get('feed')
        if isinstance(feed, dict):
            base_age = self._finite(feed.get('age_seconds'))
            # Preserve the verified source's existing two-second future-tick
            # allowance; publication/elapsed ages never permit negative values.
            age = self._finite(base_age + elapsed) if base_age is not None and base_age >= -2 and elapsed is not None else None
            feed['age_seconds'] = age
            # Never upgrade WAITING/unverified data, and never freeze FRESH just
            # because an HTTP worker can still serve the previous publication.
            if feed.get('status') == 'FRESH' and (not fresh or age is None or age > 5 or feed.get('timestamp_semantics') != 'VERIFIED'):
                feed['status'] = 'WAITING'
                feed['reason'] = feed.get('reason') or ('SNAPSHOT_STALE' if not fresh else 'STALE_MT5')
        return result

    def _guard_view(self, now):
        # Action-time checks must not copy the potentially large history/chart
        # payload after sampling the clock. Only freshness evidence is needed.
        feed = self._view.get('feed')
        feed = feed if isinstance(feed, dict) else {}
        evidence = {'feed': {key: feed.get(key) for key in ('status', 'timestamp_semantics', 'age_seconds', 'reason')}}
        return self._aged_view(evidence, self._published_at, now)

    @staticmethod
    def _require_fresh(action, view):
        # These exact commands stop new exposure or recheck time. They do not
        # authorize a new entry, quote-based close, resume, edit or settlement.
        if action in ({'type': 'command', 'text': '/stop'}, {'type': 'command', 'text': '/resume_time'}):
            return
        if not view.get('snapshot_freshness', {}).get('fresh'):
            raise SnapshotStale('SNAPSHOT_STALE')
        feed = view.get('feed') or {}
        if feed.get('status') != 'FRESH' or feed.get('timestamp_semantics') != 'VERIFIED':
            raise SnapshotStale('FEED_STALE')

    def submit(self, request):
        request = validate_request(request)
        preview = None
        with self.lock:
            if self.max_age_seconds is not None:
                previous = self._receipts.get(request['id'])
                if previous:
                    if previous['digest'] != _request_digest(request):
                        raise Conflict('Action id reused with different payload')
                    # Return a known durable result without enqueuing work. In
                    # particular an UNKNOWN receipt is never permission to retry.
                    return dict(id=request['id'], **{k: previous[k] for k in ('status', 'revision')})
                self._require_fresh(request['action'], self._guard_view(self.monotonic()))
                feed = self._view.get('feed') or {}
                preview = (self._published_at, {'feed': {key: deepcopy(feed.get(key)) for key in ('status', 'timestamp_semantics', 'age_seconds', 'reason')}})
            self.requests.put_nowait((request, preview))
        return {'id': request['id'], 'status': 'QUEUED'}

    def view(self):
        with self.lock:
            return self._aged_view(self._view, self._published_at, self.monotonic())

    def _guard_execution(self, action, preview):
        with self.lock:
            now = self.monotonic()
            # Keep the original preview deadline even if an earlier drain has
            # published a newer snapshot while this request remained queued.
            published_at, view = preview
            self._require_fresh(action, self._aged_view(view, published_at, now))
            self._require_fresh(action, self._guard_view(now))

    def drain(self, gateway, view_factory, limit=8):
        if threading.get_ident() != gateway.owner:
            raise RuntimeError('Mailbox drain and snapshot publication require the owner thread')
        for _ in range(limit):
            try:
                request, preview = self.requests.get_nowait()
            except queue.Empty:
                break
            try:
                if self.max_age_seconds is None:
                    result = gateway.apply(request)
                else:
                    result = gateway.apply(request, pre_execute=lambda action: self._guard_execution(action, preview))
                self.outcomes[request['id']] = {k: result[k] for k in ('status', 'revision')}
            except Conflict:
                self.outcomes[request['id']] = {'status': 'CONFLICT'}
            except ValueError:
                self.outcomes[request['id']] = {'status': 'REJECTED'}
            finally:
                self.requests.task_done()
        # Receipts remain durable; this bounded transport cache is only presentation.
        while len(self.outcomes) > 256:
            self.outcomes.pop(next(iter(self.outcomes)))
        projection_started = self.monotonic()
        view = deepcopy(view_factory())
        durable = deepcopy(view.get('action_results', {}))
        # A conflicting reuse must not erase an existing UNKNOWN receipt.
        published = deepcopy(self.outcomes)
        published.update(durable)
        view['action_results'] = published
        receipts = deepcopy(gateway.controller.state.get('control_receipts', {})) if self.max_age_seconds is not None else {}
        with self.lock:
            published_at = self._nonnegative(self.monotonic())
            started = self._nonnegative(projection_started)
            duration = self._nonnegative(published_at - started) if published_at is not None and started is not None else None
            if self.max_age_seconds is not None:
                feed = view.get('feed')
                if isinstance(feed, dict):
                    base_age = self._finite(feed.get('age_seconds'))
                    # The reader measures quote age before the rest of the view
                    # is built. Conservatively include that work, then stamp the
                    # actual publication; an HTTP worker only adds elapsed time.
                    feed['age_seconds'] = self._finite(base_age + duration) if base_age is not None and base_age >= -2 and duration is not None else None
            self._view = view
            self._published_at = published_at if duration is not None else None
            self._receipts = receipts
