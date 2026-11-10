"""Bounded, allowlisted diagnostics: never export raw logs, state or credentials."""
from collections import deque
from datetime import datetime, timezone
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import stat

ROTATION_BYTES = 5_000_000
BACKUPS = 3
INPUT_TAIL_BYTES = 512 * 1024  # per existing segment; at most four segments
DOCUMENT_BYTES = 1024 * 1024
MAX_LINE_BYTES = 4096
PHASES = {'IDLE','WAIT_CANDLE','NEXT_PAIR','PAIR','POSITION','COOLDOWN','PAUSED',
          'PAUSED_CYCLE','SAFE_PAUSE','MANUAL','WAIT_START','WAIT_SIGNAL','WAIT_TRIGGER'}
EVENTS = set('''service_started service_observation manual_reconciliation_error safe_pause control_command cycle_manual_required
manual_observation may_send obligations_reconciled broker_reconciled
cooldown_finished_wait_new_close new_cycle_snapshot pair_intents_atomic cancel_intent
stop_between_branches fill_oco_intents crossed_stop_explicit_rejection_market_intent
pending_pair_cancelled actual_fill_adopted anchor_readback anchor_intent step_intent
manual_closure_accounted cycle_complete closure_accounted_and_next_branch
manual_reset_evidence manual_reset_duplicate manual_reset
candle_doji_wait candle_observed candle_cancel_confirmed candle_entry_rejected candle_level_rejected candle_stop_intent
control_action_reserved control_action_completed mode_selected interactive_confirmed_closure
unknown_gateway_outcome protection_unchanged mt5_unavailable'''.split())
EVENTS.update({'service_stopped', 'service_failed'})
EVENTS.update(['basket_actual_fill_ladder', 'basket_cancel_pending', 'basket_candle_observed', 'basket_close_reconciliation', 'basket_controller_loaded', 'basket_cooldown_finished', 'basket_cycle_complete', 'basket_doji', 'basket_edit_confirmed', 'basket_edit_modifications', 'basket_edit_requested', 'basket_expanded_once', 'basket_fills_adopted', 'basket_gap_exit_observed', 'basket_gap_profit_cleanup', 'basket_group_armed', 'basket_initial_protected', 'basket_initial_rejected_wait', 'basket_level_wait', 'basket_manual_reset', 'basket_manual_reset_evidence', 'basket_new_cycle', 'basket_paused', 'basket_paused_debt_retained', 'basket_settings_saved', 'basket_signal_replaced', 'basket_start', 'basket_status', 'basket_stop', 'basket_tick', 'basket_ticket_closed'])
EVENTS.update(['actual_fill_adopted', 'actual_fill_ladder_confirmed', 'actual_fill_ladder_intents', 'anchor_intent', 'anchor_readback', 'basket_actual_fill_ladder', 'basket_cancel_initial_fill_won', 'basket_cancel_initial_requested', 'basket_cancel_initial_wait_fresh_close', 'basket_cancel_pending', 'basket_candle_observed', 'basket_close_reconciliation', 'basket_controller_loaded', 'basket_cooldown_finished', 'basket_cycle_complete', 'basket_doji', 'basket_edit_confirmed', 'basket_edit_modifications', 'basket_edit_requested', 'basket_expanded_once', 'basket_fills_adopted', 'basket_gap_exit_observed', 'basket_gap_profit_cleanup', 'basket_group_armed', 'basket_initial_protected', 'basket_initial_rejected_wait', 'basket_level_wait', 'basket_manual_reset_evidence', 'basket_new_cycle', 'basket_paused', 'basket_paused_debt_retained', 'basket_settings_saved', 'basket_signal_replaced', 'basket_start', 'basket_status', 'basket_stop', 'basket_tick', 'basket_ticket_closed', 'bind_existing_legacy_cycle', 'broker_reconciled', 'cancel_intent', 'candle_cancel_confirmed', 'candle_doji_wait', 'candle_entry_rejected', 'candle_level_rejected', 'candle_observed', 'candle_stop_intent', 'closure_accounted_and_next_branch', 'control_action_completed', 'control_action_reserved', 'control_command', 'cooldown_finished_wait_new_close', 'crossed_stop_explicit_rejection_market_intent', 'cycle_complete', 'cycle_manual_required', 'drag_tp_recalculation_requested', 'entry_sl_once_confirmed', 'entry_sl_once_intents', 'entry_sl_once_prepared', 'fill_oco_intents', 'fixed_actual_fill_adopted', 'fixed_manual_reset_evidence', 'interactive_close_evidence', 'interactive_confirmed_closure', 'interactive_known_not_sent_retry', 'interactive_modules', 'interactive_pair_intents', 'interactive_protection_readback', 'interactive_protection_update', 'interactive_settings_mode', 'latest_close_initial_range_fixed', 'latest_close_range_migration_block', 'latest_close_range_policy', 'latest_close_recovery_range_fixed', 'manual_close_debt_covered', 'manual_close_intents', 'manual_close_prepared', 'manual_close_range_confirmed', 'manual_close_stopped_before_send', 'manual_closure_accounted', 'manual_observation', 'manual_range_after_closed_basket', 'manual_reconciliation_error', 'manual_reset_duplicate', 'manual_reset_evidence', 'new_cycle_snapshot', 'obligations_reconciled', 'opening_costs_observed', 'own_entry_sl_prepared', 'pair_intents_atomic', 'paper_flatten', 'paper_interrupt_reservation_reconciled', 'paper_manual_reset', 'pending_pair_cancelled', 'primary_v3_loaded', 'profile_actual_fill_targets_prepared', 'profile_fill_subset_confirmed', 'profile_ticket_tp_edit_requested', 'protection_known_not_sent_retry', 'recovery_mode_bound', 'recovery_profile_selected', 'recovery_profiles_saved', 'recovery_target_reached_close', 'recovery_waived_pending_preserved', 'recovery_waived_without_pnl_debit', 'replacement_after_confirmed_cancel', 'replacement_crossed', 'reverse_ladder_change_prepared', 'safe_pause', 'semi_cycle_received_quote', 'semi_wait_next_command', 'service_observation', 'step_intent', 'stop_between_branches', 'time_block_latched', 'time_block_pair_cancelled', 'time_block_unsent_entries_cancelled', 'time_resume_failed', 'time_resume_requested', 'time_resumed_explicitly', 'tp_debt_previous_main_range_confirmed', 'tp_distances_saved', 'tp_grid_edit_requested', 'trigger_replacement_requested', 'user_close_intent', 'user_deleted_initial_trigger', 'user_protection_requested', 'v3_doji', 'v3_entry_cancel_confirmed', 'v3_entry_cancel_requested', 'v3_entry_invalid', 'v3_entry_prepared', 'v3_frozen_link_protection_edit', 'v3_link_cancel_confirmed', 'v3_link_cancel_requested', 'v3_link_created', 'v3_link_edit_requested', 'v3_link_fills_adopted', 'v3_missed_candle', 'v3_partial_cancel_adopted', 'v3_pending_replacement_stopped', 'v3_relink_consumed', 'v3_schedule_cancelled', 'v3_schedule_consumed', 'v3_schedule_created', 'v3_settings_saved', 'v3_sl_adjust_requested', 'v3_wait_owner', 'v3_wait_paused'])
PHASES.update({'V3_WAIT_ENTRY','V3_WAIT_CANDLE','MIGRATION_BLOCK','INITIAL_WAIT','INITIAL_PENDING','INITIAL_POSITION','RECOVERY_WAIT','RECOVERY_PENDING','RECOVERY_POSITION','RECOVERY_CLEANUP'})
ERROR_TYPES = {'OSError','TimeoutError','ValueError','QuoteFreshnessError','ConnectionError',
               'ReadTimeout','ConnectTimeout','HTTPError','Error'}
LEVELS = {'DEBUG','INFO','WARNING','ERROR','CRITICAL'}
STAMP = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ')
LEGACY = re.compile(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d{3} (DEBUG|INFO|WARNING|ERROR|CRITICAL) (.*)')


def _event_name(value):
    # May-send UUIDs, account/order/deal IDs and arbitrary event payloads are omitted.
    if not isinstance(value,str): return None
    name=value.split(':',1)[0]
    return name if name in EVENTS else None


def project_record(record):
    event=None; extra={}
    if record.name=='tradfi.paper_runtime' and record.msg in {'service_started','service_stopped','service_failed'}:
        event=record.msg
    elif record.name=='tradfi.engine':
        if record.msg=='%s phase=%s' and isinstance(record.args,tuple) and len(record.args)==2:
            event=_event_name(record.args[0])
            if not isinstance(record.args[1],str) or record.args[1] not in PHASES: return None
            extra['phase']=record.args[1]
        elif record.msg=='Unknown gateway outcome %s: %s':
            event='unknown_gateway_outcome'
        elif record.msg=='Protection unchanged: %s':
            event='protection_unchanged'
    elif record.name in {'root','tradfi.runtime'} and record.msg=='MT5 temporarily unavailable: %s':
        event='mt5_unavailable'
        kind=record.args[0] if isinstance(record.args,tuple) and len(record.args)==1 else 'Error'
        extra['error_type']=kind if isinstance(kind,str) and kind in ERROR_TYPES else 'Error'
    if not event or record.levelname not in LEVELS: return None
    stamp=datetime.fromtimestamp(record.created,timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return dict(version=1,utc=stamp,level=record.levelname,event=event,**extra)


class SafeFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(project_record(record),sort_keys=True,ensure_ascii=True)


def diagnostic_handler(path):
    handler=RotatingFileHandler(path,maxBytes=ROTATION_BYTES,backupCount=BACKUPS,encoding='utf-8')
    handler.addFilter(lambda record: project_record(record) is not None)
    handler.setFormatter(SafeFormatter())
    return handler


def sanitize_line(line):
    """Re-project even previously rotated/legacy files; never trust file contents."""
    if len(line)>MAX_LINE_BYTES: return None
    try:
        row=json.loads(line)
    except (ValueError,UnicodeError):
        row=None
    if isinstance(row,dict):
        if (row.get('version')!=1 or not isinstance(row.get('utc'),str)
                or not STAMP.fullmatch(row['utc']) or not isinstance(row.get('level'),str) or row['level'] not in LEVELS
                or not isinstance(row.get('event'),str) or row['event'] not in EVENTS):
            return None
        safe={k:row[k] for k in ('version','utc','level','event')}
        if isinstance(row.get('phase'),str) and row['phase'] in PHASES: safe['phase']=row['phase']
        if isinstance(row.get('error_type'),str) and row['error_type'] in ERROR_TYPES: safe['error_type']=row['error_type']
        return json.dumps(safe,sort_keys=True).encode('ascii')+b'\n'
    match=LEGACY.fullmatch(line.strip())
    if not match: return None
    stamp,level,message=match.groups()
    event_phase=re.fullmatch(r'([a-z_]+(?::[0-9a-f]{24})?) phase=([A-Z_]+)',message)
    if event_phase:
        event=_event_name(event_phase[1]); phase=event_phase[2]
        if not event or phase not in PHASES: return None
        safe=dict(version=1,legacy_local_time=stamp,level=level,event=event,phase=phase)
    elif message.startswith('Unknown gateway outcome '):
        safe=dict(version=1,legacy_local_time=stamp,level=level,event='unknown_gateway_outcome')
    elif message.startswith('Protection unchanged: '):
        safe=dict(version=1,legacy_local_time=stamp,level=level,event='protection_unchanged')
    elif message.startswith('MT5 temporarily unavailable: '):
        safe=dict(version=1,legacy_local_time=stamp,level=level,event='mt5_unavailable')
    else: return None
    return json.dumps(safe,sort_keys=True).encode('ascii')+b'\n'


def build_document(log_path):
    """Best-effort tail of fixed rotated paths. Disk work belongs only to document worker."""
    base=Path(log_path)
    lines=deque(); size=0; omitted=0; files_read=0; tails_cut=0; unavailable=0
    budget=DOCUMENT_BYTES-2048
    for suffix in ('.3','.2','.1',''):
        path=Path(str(base)+suffix)
        try:
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                unavailable+=1; continue
            with path.open('rb') as handle:
                handle.seek(0,2); end=handle.tell(); start=max(0,end-INPUT_TAIL_BYTES)
                handle.seek(start); raw=handle.read(min(end-start,INPUT_TAIL_BYTES))
            files_read+=1
            if start:
                tails_cut+=1
                raw=raw.split(b'\n',1)[1] if b'\n' in raw else b''
            for line in raw.splitlines():
                cleaned=sanitize_line(line.decode('utf-8',errors='replace'))
                if cleaned is None:
                    omitted+=1; continue
                lines.append(cleaned); size+=len(cleaned)
                while size>budget:
                    size-=len(lines.popleft()); omitted+=1
        except (OSError,ValueError):
            unavailable+=1  # rotation/missing files never expose paths or exception strings
    header=(
        'TradFi sanitized diagnostic log v1\n'
        'Scope: current configured log plus .1/.2/.3; bounded tails, not complete history.\n'
        'Only event/phase/time/severity and safe error categories; no raw exceptions, IDs, state or secrets.\n'
        f'files_read={files_read}; unavailable={unavailable}; truncated_tails={tails_cut}; '
        f'omitted_records={omitted}; exported_records={len(lines)}\n'
        'Rotation may change during reading; this is a best-effort diagnostic snapshot.\n'
        + ('No runtime log files are readable: logging may not have started, the path may be unavailable, or an older version did not capture runtime logs. PAPER detailed diagnostics are a separate export.\n' if not files_read else '')
        + ('No allowlisted runtime events are available in this snapshot.\n' if files_read and not lines else '')
        + '\n'
    ).encode('ascii')
    return header+b''.join(lines)
