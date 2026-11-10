"""Explicit diagnostic schema: numeric trading facts, no credentials/raw messages."""
from decimal import Decimal,InvalidOperation
import hashlib
import re

VERSION='panel-diagnostics-v2'
BASE='ca31378567a31cb585715aaa7b0c4093e966c3b3' # implementation baseline, not a runtime HEAD claim
NUMERIC={'sequence','line','margin_mode','price','entry','price_open','sl','tp','volume','volume_min','volume_step','volume_max','size','bid','ask','time','time_msc','utc_time','timestamp','age_seconds','raw_time_msc','offset_seconds','spread','profit','commission','swap','fee','realized','debt','recovery','retcode','revision','control_revision','point','trade_tick_size','trade_contract_size','trade_stops_level','trade_freeze_level','filling_mode','trade_exemode','trade_mode','order_mode','expiration_mode','trade_calc_mode','margin_buy','margin_sell','action','type','type_filling','type_time','deviation','started','last_candle','tick_msc','cursor','COST_RESERVE','INITIAL_SIZE','INITIAL_ENTRY_DISTANCE','INITIAL_SL_DISTANCE','INITIAL_TP_DISTANCE','BASKET_COUNT','TICKET_SIZE','RECOVERY_SL_DISTANCE','EXPANSION_DISTANCE','TP_STEP_MULTIPLIER','CYCLE_COOLDOWN_SECONDS'}
NUMERIC.update({'gross_tp','cost_reserve','shortfall','commission_per_lot_side','total_net','open_net','closed_net','started_at_utc','deadline_utc','last_observed_utc','max_duration_seconds'})
IDS={'run_id','id','ticket','position','order','deal','position_id','operation_id','receipt','receipt_id','cycle_id'}
CONTAINERS={'last_rejection','last_error','result','settings','active_settings','cfg','spec','specification','quote','positions','orders','tickets','request','commands','time_block','paper','expanded','levels','frozen_levels','account','edit','old','new','history','error_trace'}
CONTAINERS.update({'feed','metrics','projection','cycle','signal','main_range','tp_distances','active_tp_distances'})
TEXT={'phase','status','stage','side','operation','event','reason','mode','source','symbol','error_type','function','file','line','currency_profit'}
ALLOWED_WORDS={'XAUUSD.s','XAUUSDT','BUY','SELL','DEMO','PAPER','buy','sell','stop_buy','stop_sell','modify_stop','cancel','close','protection','auto','USDT','UST','USD'}

# Closed vocabulary from application states; token-shaped secrets are not safe text.
SAFE_TEXT = {'OUT', 'NOT_FOUND', 'INITIAL_POSITION', 'LOGIN_RATE_LIMIT', 'RANGE_AFTER_PARTIAL_TP', 'EXTERNAL_UTC_UNAVAILABLE', 'SESSION_LIMIT', 'POSITION', 'UNKNOWN_ORDER_IDENTITY', 'EXTERNAL_UTC_RESPONSE', 'INVALID_LOG_REQUEST', 'QUEUED', 'TRADFI_WEB_PASSWORD', 'V3', 'EXPERT', 'MT5_SOURCE_UNAVAILABLE', 'ENTRY_SL_PARTIAL_OR_REJECTED', 'AFTER_SL', 'PAIR', 'REJECTED_RESPONSE', 'MANUAL_CLOSE_LAST_EXIT_AMBIGUOUS', 'TIME_CHECK_FAILED', 'EXPANSION_DISTANCE', 'SNAPSHOT_TOO_SLOW', 'SIGNED_OUT', 'SNAPSHOT_UNAVAILABLE', 'BASE_TP_DISTANCE', 'MANUAL_CLOSE_EXPOSURE_CHANGED', 'FILLED', 'TP_DEBT_MAIN_RANGE_MISSING', 'APPLIED', 'CONFIG_IDENTITY', 'V3_LINK_REPLACEMENT_REQUIRES_REVIEW', 'EXTERNAL_UTC_INCONSISTENT', 'HISTORY_TIME_PROFILE_UNVERIFIED', 'PAUSED', 'INVALID_LOGIN', 'RECOVERY_CLEANUP', 'ROLLBACK', 'BASKET_COUNT', 'EDIT_QUOTE_CHANGED', 'ERROR', 'AGE_LIMIT', 'EDIT_FILL_OR_CLOSE_RACE', 'SCOPE_CHANGED', 'MT5_UTC_INVALID_TIMESTAMP', 'EXTERNAL_UTC_OFFSET_OR_UNCERTAINTY', 'INVALID_SERVER_TIMESTAMP', 'TP_STEP_MULTIPLIER', 'PARTIAL_POSITION_CHANGE', 'PROTECTION_READBACK', 'MANUAL_RANGE_GROUP_MISMATCH', 'MT5_READ_ONLY', 'SL', 'INITIAL_ENTRY_DISTANCE', 'MANUAL_CLOSE_VOLUME_CHANGED', 'PENDING_OBSERVED', 'BUY', 'SAFE_PAUSE', 'ORIGIN_DENIED', 'TP1_OWN_ENTRY_SL_UNAVAILABLE', 'NOT_SENT', 'SESSION_EXPIRED', 'API_EVIDENCE_UNAVAILABLE', 'DEBUG', 'MIGRATION_BLOCK', 'IN', 'MT5_SYMBOL', 'EXTERNAL_UTC_', 'WAIT', 'RECOVERY_WAIVED', 'INVALID_LOCAL_CLOCK', 'SIMULATOR_ONLY', 'FEED_STALE', 'PARTIAL_TP', 'EXTERNAL_UTC_TIMEOUT', 'MT5_CURRENCY_MODE', 'UNKNOWN_POSITION_IDENTITY', 'CLOSE_TERMINAL_READBACK', 'CANCEL_TERMINAL_READBACK', 'RECOVERY_POSITION', 'MT5_PACKAGE', 'CONFIG_READ', 'RECOVERY_SL_DISTANCE', 'BRANCH1_RECOVERY', 'EXTERNAL_UTC_HTTP_STATUS', 'CONFIG_LOGIN', 'DONE', 'INITIAL_TP_AND_REVERSAL_FILLED', 'OFFLINE_SIMULATOR', 'ORDER_NOT_FINAL', 'TIME_PROFILE', 'MT5_PATH_MISMATCH', 'SL_DISTANCE_AFTER_PARTIAL_TP', 'UNEXPECTED_CLOSE_REASON', 'AMBIGUOUS_OR_NONEXISTENT_SERVER_TIME', 'WAIT_SIGNAL', 'SIMULATED_LINEAR', 'PAPER_INTERRUPT', 'ACCEPTED', 'ACTUAL_LADDER_PARTIAL_FAILURE', 'LAST_RECOVERY_EXIT_UNCONFIRMED_OR_AMBIGUOUS', 'UNAVAILABLE', 'TICKET_SIZE', 'MISSING_DURABLE_JOURNAL', 'POST', 'WAIT_CANDLE', 'REJECTED', 'ACKNOWLEDGED', 'FIRST', 'UST', 'MT5_UTC_WAIT_REFERENCE', 'INITIAL_SL_DISTANCE', 'CONFIRMED', 'WAIT_TRIGGER', 'PARTIAL_CLOSE_UNSUPPORTED', 'UNSUPPORTED_EXIT', 'IDLE', 'CONFLICT', 'MT5_DISCONNECTED', 'PAUSED_CYCLE', 'CONFIG_FLAGS', 'BASKET_VALIDATION_FAILED', 'BRANCH1_SL_DISTANCE', 'MIXED', 'V3_WAIT_ENTRY', 'MARKET', 'PARTIAL_FILL_OR_NONINDEPENDENT_TICKET', 'QUEUE_FULL', 'INITIAL_TP_NET_LOSS_REQUIRES_REVIEW', 'FILLED_OPEN_EVIDENCE', 'TERMINAL_UNFILLED_EVIDENCE', 'INITIAL', 'UNKNOWN', 'BASKET_COMMAND_REJECTED', 'AMBIGUOUS_HISTORY_TIME', 'PAPER', 'INITIAL_TP_DISTANCE', 'WAIT_START', 'AWAIT_FILL_EVIDENCE', 'MT5_UTC_READ_TIMEOUT', 'SL_DISTANCE_AFTER_SL', 'EXTERNAL_UTC_UNVERIFIED', 'OBSERVATION_INVALID', 'WARNING', 'NOT_OBSERVED_SINCE_START', 'INVALID_CYCLE_CLOCK', 'MT5_UTC_CONTRACT_MISMATCH', 'MANUAL_CLOSE_PARTIAL_FAILURE', 'STARTING', 'INVALID_LOCAL_', 'OK', 'EDIT_PARTIAL_FAILURE_REVIEW_BROKER_TRUTH', 'GET', 'WAIT_FRESH_M1', 'MT5_UTC_WAIT_FRESH_DATA', 'CLOSING_COSTS_UNKNOWN', 'ACTION_CONFLICT', 'INVALID_CYCLE_DEADLINE', 'FINISH', 'BRANCH1_RANGE', 'PENDING', 'EXPLICIT_EXECUTION_ACTIVATION_REQUIRED', 'OPENING_COSTS_UNKNOWN', 'EXTERNAL_EXIT', 'MODIFY', 'FRESH', 'OWN_ENTRY_SL', 'HEADER_REQUIRED', 'COMMIT', 'PREPARED', 'ACTUAL_LADDER_GAP_EXIT_REQUIRES_REVIEW', 'PORT_BUSY', 'INITIAL_PENDING', 'EXTERNAL_UTC_RTT', 'USD', 'STALE', 'WEB_PASSWORD', 'PAPER_MANUAL_RESET', 'AUTH_FAILED', 'MT5_QUOTE', 'BOTH_RECOVERY_SIDES_FILLED', 'TRADFI_LAUNCH_SESSION', 'V3_SOURCE_EXIT_UNCONFIRMED', 'OUT_OF_ORDER_MT5', 'EXPLICIT_BYBIT_LIVE6_TIME_PROFILE_REQUIRED', 'CLOSE', 'ENTRY_SL_PREFLIGHT_CHANGED', 'STARTUP_UNKNOWN', 'CRITICAL', 'MERGED_TICKETS_UNSUPPORTED', 'TRADFI_LAUNCH_INSTANCE', 'ANCHOR', 'INCOMPLETE_BASKET_CANCELLED_TICKETS', 'RECOVERY_WAIT', 'FAILED', 'UTC_CLOCK_MOVED_BACKWARDS', 'SNAPSHOT_STALE', 'V3_PARTIAL_BATCH_CANCELLED', 'AUTHENTICATED', 'WAIT_FRESH_MT5', 'CONFIG_BACKEND', 'CYCLE_COOLDOWN_SECONDS', 'MT5_INITIALIZE', 'PARTIAL_CLOSE', 'CLOCK_DISCONTINUITY', 'CROSSED', 'TICK_SECONDS_MILLISECONDS_DISAGREE', 'MANUAL', 'OFFLINE_INJECTED_CLOCK', 'BOOKED_TICKET_NET_EXCLUDING_CARRY_AND_FLOATING', 'READY', 'AUTH_NOT_CONFIGURED', 'PAPER_INTERRUPTED_RETAINED', 'NEXT_PAIR', 'INCOMPLETE_TICKET_EVIDENCE', 'MIXED_EXIT_REASONS', 'TP_DEBT_MAIN_RANGE_INVALID', 'STEP', 'PAPER_ON_MT5_READ_ONLY', 'CONFIG_MAGIC', 'STOP', 'WAITING', 'CANCELLED', 'EDIT_PROTECTION_CHANGED', 'DEBT_COVERED', 'EXTERNAL_UTC_STALE', 'STALE_MT5', 'TP_DEBT_RANGE_REQUIRES_FLAT_RECONCILIATION', 'PAPER_CYCLE_INTERRUPTED', 'CYCLE_24H_LIMIT', 'INITIAL_AND_RECOVERY_OVERLAP', 'CLOSED_EVIDENCE', 'EXECUTION', 'OBSERVED', 'AWAITING_POST_MUTATION_READBACK', 'AUTH_REQUIRED', 'RANGE_AFTER_SL', 'FIXED_RECOVERY', 'STATE_GUARD', 'MT5_ACCOUNT_MISMATCH', 'MANUAL_CLOSE_RESIDUAL_EXPOSURE', 'READ_ONLY', 'V3_PARTIAL_OR_REJECTED_BATCH', 'MANUAL_RESET', 'MARKET_TIME_STATE', 'MISSING_OR_INVALID_CYCLE_DEADLINE', 'AWAIT_POSITION_EVIDENCE', 'INVALID_ACTION', 'WEB_CONFIG', 'COST_RESERVE', 'MT5_SOURCE_IDENTITY_CHANGED', 'CANCEL', 'LOCAL_CLOCK_JUMP', 'TP', 'MANUAL_CLOSE_RANGE_INVALID_AFTER_FILL', 'OBSOLETE', 'ACTUAL_FILL_LADDER_REQUIRES_REVIEW', 'LOCK_BUSY', 'MANUAL_RESET_RETAINED', 'UNVERIFIED', 'COOLDOWN', 'NO_SEND_EVIDENCE', 'RECOVERY_PENDING', 'OTHER', 'INVALID_MARKET_TIMESTAMP', 'PARTIAL_FILL', 'V3_PENDING_REPLACEMENT_REQUIRES_REVIEW', 'RECOVERY_TARGET', 'INITIAL_WAIT', 'MT5_TIMESTAMP_SEMANTICS_UNVERIFIED', 'V3_LINK_EDIT_REJECTED', 'V3_WAIT_CANDLE', 'BOUNDED_JSON_REQUIRED', 'VOLUME_EVIDENCE_INCONSISTENT', 'INFO', 'MANUAL_CLOSE_NATURAL_EXIT_RACE', 'WAIVE_RECOVERY_KEEP_PENDING_NO_PNL_CHANGE', 'LOCAL_CLOCK_', 'INVALID_MARKET_TIME_OFFSET_SECONDS', 'LOGS_UNAVAILABLE', 'INITIAL_SIZE', 'SELL', 'VERIFIED', 'WAIVE_RECOVERY_ONLY_NO_PNL_DEBIT', 'CONFIG_JSON'}

def ident(value):
    value=str(value)
    if re.fullmatch(r'(?:[0-9]{1,20}|[a-f0-9-]{16,80})',value):return value
    return 'hash-'+hashlib.sha256(value.encode('utf-8',errors='replace')).hexdigest()[:16]

def project(value,key='',depth=0):
    if depth>7:return None
    if isinstance(value,dict):
        result={}
        for k,v in list(value.items())[:200]:
            if k not in NUMERIC|IDS|CONTAINERS|TEXT|{'enabled','ready','paused_gap','stopped','identifier'}:continue
            safe=project(v,k,depth+1)
            if safe is not None:result[k]=safe
        return result
    if isinstance(value,(list,tuple)):return [project(v,key,depth+1) for v in value[-100:]]
    if key in IDS:return ident(value)
    if key in NUMERIC:
        try:
            n=Decimal(str(value))
            return str(n) if n.is_finite() and len(str(n))<=80 else None
        except (InvalidOperation,ValueError):return None
    if isinstance(value,bool):return value
    if key=='identifier':return '••'+str(value)[-2:]
    if key in TEXT and isinstance(value,str):
        if value in ALLOWED_WORDS or value in SAFE_TEXT:return value
        if key=='event':
            from .log_export import EVENTS
            if value in EVENTS:return value
        if key=='error_type':
            from .log_export import ERROR_TYPES
            if value in ERROR_TYPES:return value
        return 'REDACTED_TEXT'
    return None
