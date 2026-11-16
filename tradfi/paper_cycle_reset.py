"""One atomic PAPER reset: real simulated settlement, debt waiver, automatic restart."""
from copy import copy, deepcopy
from decimal import Decimal as D
import json
import time
import uuid
from .control import ActionRejected
from .storage import DurabilityError
from .settings import number
from .broker import Spec
from .engine import TERMINAL
from .paper_position_history import position_history, _price
from .sl_overlap_recovery import VARIANT


def reject(message):raise ActionRejected(message,public_message=message)


def recover_receipts(session):
    changed=False
    for receipt in session.controller.state.get('control_receipts',{}).values():
        if receipt.get('status')=='UNKNOWN' and receipt.get('paper_cycle_reset_atomic'):
            receipt.update(status='REJECTED',reason='Сброс не был сохранён; позиции, история и RECOVERY не изменены этой командой.')
            changed=True
    if changed:session.controller.save('paper_reset_reservation_reconciled')


def _market(session):
    # The reader's last successful sample may be newer than the strategy FIFO.
    # Reset intentionally supersedes that old cycle, not reconstructs its ticks.
    candidates=[]
    try:candidates.append(session.reader.view())
    except Exception:pass
    candidates.append(session.exchange.p.get('last_verified_market'))
    for market in candidates:
        try:
            if not isinstance(market,dict) or market.get('timestamp_semantics') not in {'VERIFIED','BROKER_RAW'} or market.get('symbol')!=session.exchange.symbol:continue
            Spec.load(market['spec'])
            bid,ask=number(market['quote']['bid']),number(market['quote']['ask'])
            if bid<=0 or ask<bid or number(market['timestamp'])<=0:continue
            number(market['candle']['id'])
            return deepcopy(market)
        except (KeyError,TypeError,ValueError,ArithmeticError):continue
    return None


def _validate(p,state):
    if any(not row.get('owned') for row in p['positions']+p['orders']):reject('Не подтверждены виртуальные позиции или ордера; данные не удалены.')
    ids=[row['id'] for row in p['orders']+p['order_history']]
    if len(ids)!=len(set(ids)):reject('Повреждена история виртуальных ордеров; данные не удалены.')
    history=position_history(p,state['currency'],False,state['commands'])
    if not history['metrics']['history_complete']:reject('История виртуальных исполнений неполна; результат закрытия нельзя выдумать.')
    by_position={}
    for deal in p['deals']:by_position.setdefault(deal['position_id'],[]).append(deal)
    for position in p['positions']:
        rows=by_position[position['id']];ins=[d for d in rows if d['entry']=='IN'];outs=[d for d in rows if d['entry']=='OUT']
        remaining=sum((number(d['volume']) for d in ins),D(0))-sum((number(d['volume']) for d in outs),D(0))
        if outs or remaining!=number(position['size']) or number(_price(ins))!=number(position['entry']) or any(d['side']!=position['side'] for d in ins):
            reject('Размер или цена виртуальной позиции не подтверждены; позиция не удалена.')
    return by_position


def reset_cycle(session,action):
    from .paper import PaperExchange
    bot,exchange=session.controller,session.exchange;state=bot.state
    if action!={'type':'paper_reset_cycle'}:reject('Неверная команда сброса.')
    if type(exchange) is not PaperExchange or bot.broker is not exchange or state.get('backend')!=PaperExchange.backend_id or state.get('simulator_variant')!=VARIANT:
        reject('Сброс доступен только отдельному PAPER SL OVERLAP.')
    cutoff=time.monotonic()
    expected=deepcopy(bot._durable_state);expected_exchange=deepcopy(exchange.p)
    try:
        positions=_validate(exchange.p,state)
        market=_market(session)
        if exchange.p['positions'] and market is None:reject('Нет ни одной сохранённой допустимой котировки для закрытия виртуальных позиций. Сброс не выполнен; цена не выдумана.')
        fee=number(exchange.p['commission_per_lot_side'])
        if fee<0:reject('Некорректна модель комиссии.')
        planned=copy(exchange);planned.p=deepcopy(exchange.p);planned.command_baseline=None
        planned.command_quote=None;planned.market=market
        if market is not None:planned.p['last_verified_market']=deepcopy(market)
        quote=None if market is None else dict(source='LAST_OBSERVED_MT5',bid=market['quote']['bid'],ask=market['quote']['ask'],timestamp=market['timestamp'],timestamp_semantics=market['timestamp_semantics'])
        closed=[p['id'] for p in planned.p['positions']];cancelled=[o['id'] for o in planned.p['orders']]
        for order in list(planned.p['orders']):
            planned.p['orders'].remove(order);order.update(status='CANCELLED',cancel_reason='PAPER_CYCLE_RESET');planned.p['order_history'].append(order)
        for position in list(planned.p['positions']):
            planned._close(position,'PAPER_RESET',market['quote']['bid' if position['side']=='BUY' else 'ask'])
            planned.p['deals'][-1]['paper_quote']=deepcopy(quote)
        _validate(planned.p,state)
        cycle=state.get('cycle') or {};entries=set(cycle.get('entries',[]))
        affected={d['position_id'] for d in planned.p['deals'] if d['entry']=='IN' and d.get('command_id') in entries}|set(closed)
        deals=[d for d in planned.p['deals'] if d['position_id'] in affected]
        net=sum((number(d[k]) for d in deals for k in ('profit','commission','swap','fee')),D(0))
        carry=number(state.get('basket_carry','0'));waived=number(cycle.get('recovery_waived','0'))
        if carry<0 or waived<0:reject('Некорректен сохранённый учёт RECOVERY.')
        actual_debt=max(D(0),carry-net-waived)
        boundary=number(market['candle']['id']) if market else D(-1)
    except (KeyError,TypeError,ValueError,ArithmeticError) as exc:
        if isinstance(exc,ActionRejected):raise
        reject('Не удалось подтвердить сохранённые PAPER-данные; история и позиции не удалены.')
    active=getattr(bot,'_active_control_id',None)
    reset_id=active or uuid.uuid4().hex
    updated=deepcopy(state)
    settlement=dict(id=cycle.get('id') or 'reset-'+reset_id,reset_id=reset_id,contract='ticket_basket_v1',currency=state['currency'],
                    completion='PAPER_RESET_RECOVERY_WAIVED',cycle_net=str(net),realized=str(net-carry),
                    recovery_before=cycle.get('recovery',str(carry)),written_off_recovery=str(actual_debt),
                    recovery='0',recovery_retained='0',at_utc=session.clock(),paper_quote=quote,
                    closed_positions=closed,cancelled_orders=cancelled,deal_ids=[d['id'] for d in deals],automatic_restart=True)
    if deals:updated['completed'].append(deepcopy(settlement))
    updated.setdefault('recovery_writeoffs',[]).append(dict(id=reset_id,cycle_id=cycle.get('id'),amount=str(actual_debt),currency=state['currency'],at_utc=session.clock(),realized_before=str(net-carry),accounting='PAPER_RESET_WAIVE_RECOVERY_NO_PNL_DEBIT'))
    updated['last_paper_reset']=deepcopy(settlement)
    updated.setdefault('paper_resets',[]).append(deepcopy(settlement))
    filled={d.get('command_id') for d in planned.p['deals'] if d['entry']=='IN'}
    for cid,cmd in updated['commands'].items():
        if cmd['status'] not in TERMINAL or cmd.get('cycle_id')==cycle.get('id'):
            if cmd['kind'] in {'STOP','MARKET'}:cmd['status']='FILLED' if cid in filled else 'CANCELLED'
            elif cmd['status'] not in TERMINAL:cmd['status']='OBSOLETE'
            cmd['paper_reset']=reset_id
            planned.p['results'][cid]=dict(status=cmd['status'],paper_reset=reset_id)
    for deal in deals:
        if deal['entry']=='OUT' and deal['id'] not in updated['seen_deals']:updated['seen_deals'].append(deal['id'])
    updated.update(cycle=None,basket_carry='0',phase='WAIT_CANDLE',stop=False,stop_mode='',time_block=None,last_candle=int(boundary))
    updated.pop('migration_block',None)
    planned.p['paused_gap']=False
    from .paper_commands import apply_reset_supersession
    apply_reset_supersession(updated,bot)
    if active:updated['control_receipts'][active]['status']='APPLIED'
    updated['notifications'].append(dict(id='paper-reset-'+reset_id,text='Цикл и RECOVERY сброшены. История сохранена. Новый цикл включён автоматически со следующей закрытой свечи.'))
    updated['notifications']=updated['notifications'][-200:]
    _commit(session,expected,expected_exchange,updated,planned.p,settlement)
    session._paper_reset_capture_cutoff=cutoff
    session.snapshot=None if market is None else dict(account=exchange.account,hedging=True,history_complete=False,spec=deepcopy(market['spec']),now=session.clock(),quote=deepcopy(market['quote']),candle=deepcopy(market['candle']),orders=[],positions=[])
    return settlement


def _commit(session,expected,expected_exchange,state,paper,settlement):
    store=session.store;encode=lambda x:json.dumps(x,sort_keys=True,allow_nan=False)
    try:
        store.db.execute('BEGIN IMMEDIATE')
        previous=json.loads(store.db.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()[0])
        if store.load()!=expected or previous!=expected_exchange:raise DurabilityError('PAPER reset snapshot changed')
        store.verify_deal_ledger(expected)
        store.db.execute('UPDATE paper_exchange SET payload=? WHERE id=1',(encode(paper),))
        if store.fault:store.fault('paper_reset_exchange_written')
        store.db.execute('UPDATE snapshot SET payload=? WHERE id=1',(encode(state),))
        if store.fault:store.fault('paper_reset_snapshot_written')
        store.db.execute('INSERT INTO audit(event,payload) VALUES(?,?)',('paper_cycle_reset',encode(settlement)))
        if store.fault:store.fault('paper_reset_before_commit')
        store.db.execute('COMMIT')
        if store.fault:store.fault('paper_reset_after_commit')
    except BaseException as exc:
        if store.db.in_transaction:store.db.execute('ROLLBACK')
        if isinstance(exc,DurabilityError):raise
        raise DurabilityError('Atomic PAPER reset failed; reload durable state') from exc
    finally:
        session.controller.state=store.load();session.controller._durable_state=deepcopy(session.controller.state)
        session.exchange.p=json.loads(store.db.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()[0])
