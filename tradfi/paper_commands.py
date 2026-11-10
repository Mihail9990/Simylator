"""Explicit PAPER command execution on the last verified quote, never feed replay."""
from copy import deepcopy
from contextlib import contextmanager
from .control import OwnerMailbox, SnapshotStale, ActionRejected
from .settings import number


def reject(message):
    raise ActionRejected(message, public_message=message)


class PaperCommandMailbox(OwnerMailbox):
    @staticmethod
    def _require_fresh(action, view, *, stage=None, queue_age=None):
        # Local obligations must be current. Quote availability/validity is checked
        # by the owner against its own persisted PAPER state, not an HTTP cache.
        if action in ({'type':'command','text':'/stop'}, {'type':'command','text':'/resume_time'}):
            return
        if not view.get('snapshot_freshness', {}).get('fresh'):
            raise SnapshotStale('SNAPSHOT_STALE', stage=stage,
                                snapshot_age=view.get('snapshot_freshness', {}).get('age_seconds'), queue_age=queue_age)


def quote_info(exchange):
    market = exchange.p.get('last_verified_market')
    unavailable = dict(available=False, source='LAST_VERIFIED_MT5')
    try:
        if not market or market['timestamp_semantics'] != 'VERIFIED':return unavailable
        bid, ask = number(market['quote']['bid']), number(market['quote']['ask'])
        stamp = number(market['timestamp']);age = number(exchange.clock())-stamp
        if bid <= 0 or ask < bid or stamp <= 0 or age < -2:return unavailable
        from .broker import Spec
        Spec.load(market['spec'])
        if market['symbol'] != exchange.symbol:return unavailable
        return dict(available=True, source='LAST_VERIFIED_MT5', bid=str(bid), ask=str(ask),
                    timestamp=float(stamp), age_seconds=float(age), stale=bool(age > 5 or not exchange.available))
    except (ValueError, KeyError, TypeError, ArithmeticError):
        return unavailable


@contextmanager
def command_scope(session, action):
    exchange, controller = session.exchange, session.controller
    if exchange.backend_id != 'paper_mt5_quotes_v1' or controller.broker is not exchange:
        reject('Последняя котировка доступна только в PAPER.')
    active = getattr(controller, '_active_control_id', None)
    safe=action in ({'type':'command','text':'/stop'},{'type':'command','text':'/resume_time'})
    if not safe and (any(r.get('status')=='UNKNOWN' and rid != active for rid,r in controller.state.get('control_receipts', {}).items()) or any(cmd['status']=='UNKNOWN' for cmd in controller.state['commands'].values())):
        reject('Предыдущий исход UNKNOWN: повтор и новые команды запрещены до сверки.')
    cycle=controller.state.get('cycle') or {}
    if not safe and any(cycle.get(key) for key in ('edit','v3_edit','v3_rearm','v3_relink','manual_close','initial_cancel')):
        reject('Предыдущая правка или отмена ещё не завершена; новая команда не продолжает чужую операцию.')
    # Never execute an unrelated READY command just because a new owner action
    # supplied a usable old quote. UNKNOWN is never promoted to READY.
    exchange.command_baseline = set(controller.state['commands'])
    exchange.command_quote = quote_info(exchange)
    try:
        yield
    finally:
        exchange.command_baseline = None
        exchange.command_quote = None


def advance_command(session, action):
    """Only explicit intent stages. No tick, candle following or TP/SL triggers."""
    bot = session.controller
    kind = action.get('type')
    if kind == 'paper_flatten' and bot.state.get('cycle'):
        snap=bot.snapshot();bot._reconcile_commands(snap);bot._observe(snap)
        bot.state['phase']='PAUSED_CYCLE';bot.save('paper_explicit_close_observed');return
    if kind in {'paper_interrupt','paper_flatten','paper_resume','paper_costs'}:return
    if kind == 'command':return
    for _ in range(12):
        c = bot.state.get('cycle')
        if not c:return
        before = repr((bot.state, session.exchange.p))
        snap = bot.snapshot();bot._reconcile_commands(snap)
        if not bot._observe(snap):return
        if any(cmd['status']=='UNKNOWN' for cmd in bot.state['commands'].values()):return
        if c.get('manual') and kind != 'command':return
        if c.get('v3_edit'):bot._advance_link(snap)
        edited = deepcopy(c.get('edit'))
        if edited:
            waiting = bot._advance_edit(snap)
            if not waiting and edited.get('groups') and not c['tickets'] and kind in {'basket_levels','basket_settings'}:
                signal=c['signal'];side=signal['side'];sign=1 if side=='BUY' else -1
                level=str(number(signal['close'])+sign*number(c['cfg']['INITIAL_ENTRY_DISTANCE']))
                bot._arm(snap,'initial',c.get('signal_override') or {side:level})
                bot.state['phase']='INITIAL_PENDING'
        if c.get('v3_rearm') and not c.get('edit') and kind in {'basket_levels','basket_settings','tp_distances'}:
            request=c.pop('v3_rearm')
            bot._arm_v3(snap,request,request['side'],request['level'])
        if c.get('v3_relink') and not c.get('edit') and kind in {'protection','v3_adjust_sl','own_entry_sl'}:
            request=c.pop('v3_relink')
            bot._request_link(dict(type='v3_link',cycle_id=c['id'],count=request['count'],expected_positions=sorted(p['id'] for p in snap['positions'])),recovery_profile=request.get('recovery_profile'),legacy=not bool(request.get('recovery_profile')))
        bot.dispatch()
        snap=bot.snapshot();bot._reconcile_commands(snap)
        if not bot._observe(snap):return
        if kind=='v3_entry' and action.get('timing')=='now' and snap['positions']:
            bot.state['phase']='RECOVERY_POSITION'
        bot.save('paper_explicit_command_advanced')
        if repr((bot.state, session.exchange.p)) == before:return
        if not c.get('edit') and not c.get('v3_edit') and not c.get('v3_rearm') and not c.get('v3_relink') and not c.get('initial_cancel'):
            return


def cancel_without_quote(session, action):
    """Cancel only authoritative local paper obligations; no invented price/spec."""
    import json
    from .storage import DurabilityError
    bot, exchange = session.controller, session.exchange
    kind=action.get('type');stop=action=={'type':'command','text':'/stop'}
    if not stop and kind not in {'cancel_initial','v3_cancel_entry','v3_cancel_link','v3_cancel_request'}:return False
    state=deepcopy(bot.state);paper=deepcopy(exchange.p);c=state.get('cycle')
    if state.get('migration_block'):reject('Нужна сверка прежнего цикла.')
    if any(not x.get('owned') for x in paper['orders']+paper['positions']):reject('Есть неизвестные виртуальные обязательства.')
    if not stop:
        fields={'type','cycle_id','expected_orders'} if kind=='cancel_initial' else {'type','cycle_id'}
        if set(action)!=fields or not c or action['cycle_id']!=c['id']:reject('Цикл изменился; обновите панель.')
        if c.get('edit') or c.get('v3_edit') or c.get('v3_rearm') or c.get('initial_cancel') or c.get('manual'):reject('Сначала сверка предыдущей операции.')
    ids=set()
    if stop:
        state['stop']=True;state['stop_mode']='FINISH' if c and c.get('tickets') else 'CANCEL'
        ids=set(c['entries']) if c else set()
        if c:c.pop('v3_request',None)
    elif kind=='cancel_initial':
        expected=action['expected_orders']
        if c['tickets'] or paper['positions']:reject('Начальный ордер уже исполнен; позиция сохраняет защиту.')
        if state['stop'] or state['phase'] not in {'INITIAL_PENDING','INITIAL_WAIT'}:reject('Нет активного начального триггера.')
        if not isinstance(expected,list) or not expected or sorted(expected)!=sorted(o['id'] for o in paper['orders']):reject('Начальные ордера изменились; обновите панель.')
        ids={cid for g in c['groups'] if g['role']=='initial' and not g.get('retired') for cid in g['commands']}
        if not ids or any(o['command_id'] not in ids for o in paper['orders']):reject('Есть ордера другого назначения.')
    elif kind=='v3_cancel_request':
        if not c.get('v3_request'):reject('Нет запланированного входа.')
        c.pop('v3_request');state['phase']='V3_WAIT_ENTRY'
    elif kind=='v3_cancel_link':
        if not c.get('v3_link'):reject('Нет связанных триггеров.')
        ids=set(c['v3_link']['ids']);c['v3_link']['cancel']=True
    else:
        groups=bot._cancel_entry_groups({'orders':paper['orders']},c)
        if not groups:reject('Нет ожидающих ручных триггеров.')
        ids={cid for g in groups for cid in g['commands']}
    # Command UNKNOWN may represent a send. Never relabel it as a cancelled
    # unfilled order based solely on absence; only concrete local orders count.
    cancelled=set()
    for order in list(paper['orders']):
        if order['command_id'] not in ids:continue
        paper['orders'].remove(order);order['status']='CANCELLED';paper['order_history'].append(order)
        cancelled.add(order['command_id'])
    for cid in ids:
        cmd=state['commands'][cid]
        if cid in cancelled or cmd['status']=='READY':
            cmd['status']='CANCELLED';paper['results'][cid]={'status':'CANCELLED','paper_explicit_cancel':True}
    if kind=='cancel_initial':
        for g in c['groups']:g['retired']=True
        for key in ('replacement','signal_override','level_wait'):c.pop(key,None)
        c['wait_for_signal']=True;state['phase']='INITIAL_WAIT'
        state['last_candle']=max(state['last_candle'],int(number(session.clock())//60)*60-60)
    elif kind=='v3_cancel_entry':
        for g in c['groups']:
            if not set(g['commands'])&ids:continue
            g['commands']=[cid for cid in g['commands'] if state['commands'][cid]['status']!='CANCELLED']
            g['v3_count']=len(g['commands'])
            if not g['commands']:g['retired']=True
    elif kind=='v3_cancel_link':
        link=c['v3_link']
        if all(state['commands'][cid]['status']=='CANCELLED' for cid in link['ids']):
            next(g for g in c['groups'] if g['id']==link['group'])['retired']=True
            c.pop('v3_link')
    if stop and c:state['phase']='PAUSED_CYCLE'
    state['last_paper_command_quote']=deepcopy(exchange.command_quote)
    active=getattr(bot,'_active_control_id',None)
    if active:state['control_receipts'][active].update(status='APPLIED',paper_quote=deepcopy(exchange.command_quote))
    encode=lambda x:json.dumps(x,sort_keys=True,allow_nan=False)
    store=session.store
    try:
        store.db.execute('BEGIN IMMEDIATE')
        stored=json.loads(store.db.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()[0])
        if store.load()!=bot._durable_state or stored!=exchange.p:raise DurabilityError('Local PAPER cancellation state changed')
        store.verify_deal_ledger(state)
        store.db.execute('UPDATE paper_exchange SET payload=? WHERE id=1',(encode(paper),))
        store.db.execute('UPDATE snapshot SET payload=? WHERE id=1',(encode(state),))
        store.db.execute('INSERT INTO audit(event,payload) VALUES(?,?)',('paper_local_cancel',encode({'type':kind,'cancelled':sorted(cancelled)})))
        if store.fault:store.fault('paper_local_cancel_before_commit')
        store.db.execute('COMMIT')
        if store.fault:store.fault('paper_local_cancel_after_commit')
    except BaseException as exc:
        if store.db.in_transaction:store.db.execute('ROLLBACK')
        raise DurabilityError('PAPER cancellation commit failed; reload') from exc
    existing_cycle=bot.state.get('cycle')
    if existing_cycle is not None and state.get('cycle') is not None and existing_cycle.get('id')==state['cycle'].get('id'):
        existing_cycle.clear();existing_cycle.update(state['cycle']);state['cycle']=existing_cycle
    bot.state.clear();bot.state.update(state)
    bot._durable_state=deepcopy(state);exchange.p=paper
    return True
