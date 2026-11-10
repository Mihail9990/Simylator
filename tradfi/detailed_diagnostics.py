"""Bounded PAPER observations. No raw requests, exception text or credentials."""
from collections import deque
import math
import time
import threading
import uuid

EVENTS = {'owner_step', 'action_reserved', 'action_completed', 'action_replayed', 'action_conflict', 'transport_rejected'}
STATUSES = {'APPLIED', 'REJECTED', 'UNKNOWN', 'CONFLICT', 'OK', 'UNAVAILABLE'}
ERRORS = {'ActionRejected', 'SnapshotStale', 'ValueError', 'OSError', 'TimeoutError', 'ConnectionError', 'QuoteFreshnessError'}
ACTIONS = {'command', 'paper_interrupt', 'paper_resume', 'paper_flatten', 'paper_costs', 'settings', 'edit', 'recovery_writeoff', 'cancel_trigger', 'protection', 'OTHER'}
ACTIONS.update({'start','modules','triggers','delete_trigger','close_position','recovery_profiles','recovery_profile_select','v3_settings','v3_entry','v3_link','v3_cancel_link','v3_cancel_entry','v3_cancel_request','v3_adjust_sl','reverse_ladder','manual_close','writeoff_recovery'})
CODES = {'QUEUE_EXPIRED','CONTEXT_CHANGED','SNAPSHOT_STALE','FEED_STALE','SCOPE_CHANGED','ACTION_CONFLICT','INVALID_ACTION','QUEUE_FULL','INVALID_LOG_REQUEST','AUTH_REQUIRED','SESSION_EXPIRED','ORIGIN_DENIED','HEADER_REQUIRED','AUTH_NOT_CONFIGURED','BOUNDED_JSON_REQUIRED','INVALID_LOGIN','LOGIN_RATE_LIMIT','AUTH_FAILED','SESSION_LIMIT','LOGS_UNAVAILABLE','NOT_FOUND'}
COMMANDS = {'/start', '/stop', '/status', '/reset_manual', '/resume_time'}
GUARD_STAGES = {'submit','execution_queue','execution_current','execution_context','execution_preview','execution_published'}
NUMBERS = {'snapshot_age_seconds','queue_age_seconds','reservation_ms','sequence', 'at_utc', 'duration_ms', 'read_ms', 'observe_ms', 'tick_ms', 'publish_ms', 'revision', 'queue_depth', 'positions_count', 'orders_count', 'quote_age_seconds', 'dropped_records','http_status'}


def sanitize(row):
    if not isinstance(row, dict) or not isinstance(row.get('event'),str) or row['event'] not in EVENTS:
        return {}
    safe = {'event': row['event']}
    for key in NUMBERS:
        value = row.get(key)
        if type(value) in (int, float) and -1e15 <= value <= 1e15 and math.isfinite(value):
            safe[key] = value
    for key, allowed in (('status', STATUSES), ('error_type', ERRORS), ('action', ACTIONS), ('command', COMMANDS), ('error_code', CODES), ('guard_stage', GUARD_STAGES)):
        value = row.get(key)
        if isinstance(value, str) and value in allowed:
            safe[key] = value
    for key in ('run_id', 'receipt_id'):
        value = row.get(key)
        if isinstance(value, str) and len(value) in (32,36) and len(value.replace('-','')) == 32 and all(c in '0123456789abcdef' for c in value.replace('-','')):
            safe[key] = value
    return safe


class DetailedDiagnostics:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.run_id = uuid.uuid4().hex
        self.records = deque(maxlen=200)
        self.sequence = 0
        self.dropped = 0
        self.lock = threading.Lock()

    def record(self, event, **fields):
        # Observability must never change a command's outcome.
        try:
            with self.lock:
                self.sequence += 1
                if len(self.records) == self.records.maxlen:
                    self.dropped += 1
                row = sanitize(dict(fields, event=event, run_id=self.run_id,
                                    sequence=self.sequence, at_utc=self.clock(), dropped_records=self.dropped))
                if row:
                    self.records.append(row)
        except Exception:
            pass

    def action(self, event, request, **fields):
        try:
            action = request.get('action', {})
            kind = action.get('type')
            self.record(event, action=kind if isinstance(kind,str) and kind in ACTIONS else 'OTHER',
                        command=action.get('text') if kind == 'command' else None,
                        receipt_id=request.get('id'), **fields)
        except Exception:
            pass

    def drain(self):
        with self.lock:
            records = list(self.records)
            self.records.clear()
            return records
