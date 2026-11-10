"""Atomic, PAPER-only cycle interruption. No broker dispatch or native writes.

The simulator's exchange, settlement and action receipt share one SQLite commit.
A failed commit leaves no partial cancels, closes or accounting to replay.
"""
from copy import copy, deepcopy
from decimal import Decimal as D
import json

from .control import ActionRejected
from .engine import TERMINAL
from .settings import number
from .storage import DurabilityError


def reject(message):
    raise ActionRejected(message, public_message=message)


def recover_receipts(session):
    """A reserved interruption without an atomic settlement provably did nothing."""
    state = session.controller.state
    changed = False
    for receipt in state.get('control_receipts', {}).values():
        if receipt.get('status') == 'UNKNOWN' and receipt.get('paper_interrupt_atomic'):
            receipt.update(status='REJECTED', reason='Прерывание не было сохранено. Позиции и долг не изменены этой командой; проверьте свежий снимок и подтвердите заново.')
            changed = True
    if changed:
        session.controller.save('paper_interrupt_reservation_reconciled')


def _ids(values):
    return isinstance(values, list) and all(isinstance(x, str) for x in values) and len(values) == len(set(values))


def _facts(state, exchange, cycle):
    """Reconcile from the authoritative paper history, not cached ticket totals."""
    entries = set(cycle['entries'])
    commands = state['commands']
    if len(entries) != len(cycle['entries']) or not entries <= commands.keys():
        reject('История заявок цикла неполна; требуется сверка.')
    if any(cmd.get('cycle_id') != cycle['id'] for cid, cmd in commands.items() if cid in entries):
        reject('Принадлежность заявки циклу не подтверждена.')
    deals = exchange['deals']
    if len({d['id'] for d in deals}) != len(deals):
        reject('Повторяющиеся исполнения в истории; требуется сверка.')
    all_position_ids = {d['position_id'] for d in deals if d['entry'] == 'IN'}
    if any(d['entry'] not in {'IN', 'OUT'} or (d['entry'] == 'OUT' and d['position_id'] not in all_position_ids) for d in deals):
        reject('Выход без подтверждённого входа в истории; требуется сверка.')
    ins = [d for d in deals if d['entry'] == 'IN' and d.get('command_id') in entries]
    by_position = {}
    for d in ins:
        by_position.setdefault(d['position_id'], []).append(d)
    live = {p['id']: p for p in exchange['positions']}
    if len(live) != len(exchange['positions']) or not live.keys() <= by_position.keys():
        reject('Неизвестная виртуальная позиция; требуется сверка.')
    if any(not x.get('owned') for x in exchange['positions'] + exchange['orders']):
        reject('Принадлежность виртуальной позиции или ордера не подтверждена.')
    if any(o.get('command_id') not in entries for o in exchange['orders']):
        reject('Есть виртуальный ордер вне текущего цикла; требуется сверка.')
    if any(cmd['status'] not in TERMINAL and cmd.get('cycle_id') != cycle['id'] for cmd in commands.values()):
        reject('Есть несверенная заявка другого цикла.')
    seen_commands = set()
    for pid, rows in by_position.items():
        cids = {d['command_id'] for d in rows}
        if len(cids) != 1 or seen_commands & cids:
            reject('Состав виртуальных исполнений неоднозначен.')
        seen_commands.update(cids)
        cmd = commands[next(iter(cids))]
        size = number(cmd['size'])
        if size <= 0 or sum((number(d['volume']) for d in rows), D(0)) != size:
            reject('Неполный объём входа; требуется сверка.')
        if any(d['side'] != cmd['side'] or number(d['price']) <= 0 for d in rows):
            reject('Направление или цена исполнения не подтверждены.')
        exits = [d for d in deals if d['entry'] == 'OUT' and d['position_id'] == pid]
        if pid in live:
            p = live[pid]
            entry = sum((number(d['price']) * number(d['volume']) for d in rows), D(0)) / size
            if exits or number(p['size']) != size or p['side'] != cmd['side'] or number(p['entry']) != entry:
                reject('Частичное закрытие или изменившаяся позиция; требуется сверка.')
        elif sum((number(d['volume']) for d in exits), D(0)) != size:
            reject('Исчезнувшая позиция без полного выхода; требуется сверка.')
    orders = exchange['orders'] + exchange['order_history']
    if len({o['id'] for o in orders}) != len(orders):
        reject('Повторяющиеся виртуальные ордера; требуется сверка.')
    for cid in entries:
        cmd = commands[cid]
        if cmd['kind'] not in {'STOP', 'MARKET'} or cmd.get('side') not in {'BUY', 'SELL'}:
            reject('Недопустимая виртуальная заявка входа.')
        owned_orders = [o for o in orders if o.get('command_id') == cid]
        if len(owned_orders) > 1:
            reject('Несколько виртуальных ордеров для одной заявки; требуется сверка.')
        if (cmd['status'] == 'FILLED' or any(o.get('status') == 'FILLED' for o in owned_orders)) and cid not in seen_commands:
            reject('История исполненного ордера неполна; RECOVERY сохранён.')
        result = exchange['results'].get(cid, {})
        if result.get('status') == 'ACCEPTED' and not any(o['id'] == result.get('order_id') for o in owned_orders):
            reject('Принятый виртуальный ордер отсутствует в истории; требуется сверка.')
    if not set(cycle.get('tickets', {})) <= by_position.keys():
        reject('История билетов цикла неполна; требуется сверка.')
    return by_position


def interrupt_cycle(session, action):
    fields = {'type', 'cycle_id', 'expected_positions', 'expected_orders', 'confirmed', 'gap_confirmed'}
    if set(action) != fields or action.get('confirmed') is not True or not isinstance(action.get('gap_confirmed'), bool):
        reject('Нужно явное подтверждение прерывания бумажного цикла.')
    controller, exchange = session.controller, session.exchange
    state = controller.state
    # Do not let a reused class/function become a route to a real broker.
    from .paper import PaperExchange
    if type(exchange) is not PaperExchange or controller.broker is not exchange or state['backend'] != PaperExchange.backend_id:
        reject('Прерывание доступно только независимому PAPER-симулятору.')
    cycle = state.get('cycle')
    if not cycle or not isinstance(action['cycle_id'], str) or action['cycle_id'] != cycle['id']:
        reject('Цикл уже изменился или завершён; обновите панель.')
    if state.get('migration_block') or cycle.get('contract') != 'ticket_basket_v1':
        reject('Нельзя прервать непроверенный прежний цикл; нужна сверка.')
    if session.reader.safety.check() or state.get('time_block'):
        reject('Время не подтверждено. Прерывание не выполнено; дождитесь проверенных свежих котировок.')
    snap = session._fresh()
    if exchange.market.get('timestamp_semantics') != 'VERIFIED' or snap['account'] != state['account']:
        reject('Источник котировки или PAPER-счёт не подтверждён.')
    bid, ask = number(snap['quote']['bid']), number(snap['quote']['ask'])
    if bid <= 0 or ask < bid:
        reject('Нет допустимых Bid/Ask для закрытия.')
    if bool(exchange.p['paused_gap']) != action['gap_confirmed']:
        reject('Состояние перерыва изменилось. Обновите панель и подтвердите закрытие по новой котировке без восстановления пропущенных тиков.')
    for field, key in (('expected_positions', 'positions'), ('expected_orders', 'orders')):
        if not _ids(action[field]) or sorted(action[field]) != sorted(x['id'] for x in snap[key]):
            reject('Состав виртуальных позиций или ордеров изменился; подтвердите заново.')
    try:
        positions = _facts(state, exchange.p, cycle)
        fee = number(exchange.p['commission_per_lot_side'])
        if fee < 0:
            raise ValueError('negative commission')
        # Plan every cancellation, close and amount in a detached exchange first.
        planned = copy(exchange)
        planned.p = deepcopy(exchange.p)
        for order in list(planned.p['orders']):
            planned.p['orders'].remove(order)
            order.update(status='CANCELLED', cancel_reason='PAPER_CYCLE_INTERRUPTED')
            planned.p['order_history'].append(order)
        for position in list(planned.p['positions']):
            planned._close(position, 'PAPER_INTERRUPT', snap['quote']['bid' if position['side'] == 'BUY' else 'ask'])
        _facts(state, planned.p, cycle)
        cycle_deals = [d for d in planned.p['deals'] if d['position_id'] in positions]
        net = sum((sum((number(d[k]) for k in ('profit', 'commission', 'swap', 'fee')), D(0)) for d in cycle_deals), D(0))
        carry_before = number(state['basket_carry'])
        if carry_before < 0:
            raise ValueError('negative carry')
        realized = net - carry_before
        carry = max(D(0), -realized)
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        if isinstance(exc, ActionRejected):
            raise
        reject('Не удалось сверить историю или расходы. Позиции и RECOVERY не изменены.')

    active_id = getattr(controller, '_active_control_id', None)
    expected = deepcopy(controller._durable_state)
    expected_exchange = deepcopy(exchange.p)
    updated = deepcopy(state)
    settlement = dict(id=cycle['id'], currency=state['currency'], contract=cycle['contract'],
                      completion='PAPER_INTERRUPTED_RETAINED', cycle_net=str(net), realized=str(realized),
                      recovery_before=str(cycle['recovery']), carry_before=str(carry_before),
                      recovery_retained=str(carry), written_off_recovery='0',
                      at_utc=session.clock(), quote=deepcopy(snap['quote']),
                      closed_positions=sorted(action['expected_positions']), cancelled_orders=sorted(action['expected_orders']),
                      deal_ids=[d['id'] for d in cycle_deals], gap_confirmed=action['gap_confirmed'],
                      commission_per_lot_side=str(fee))
    updated['completed'].append(settlement)
    updated['basket_carry'] = str(carry)
    updated['last_paper_interrupt'] = deepcopy(settlement)
    for d in cycle_deals:
        if d['entry'] == 'OUT' and d['id'] not in updated['seen_deals']:
            updated['seen_deals'].append(d['id'])
    filled = {d.get('command_id') for d in cycle_deals if d['entry'] == 'IN'}
    for cid, cmd in updated['commands'].items():
        if cmd.get('cycle_id') != cycle['id']:
            continue
        if cmd['kind'] in {'STOP', 'MARKET'}:
            cmd['status'] = 'FILLED' if cid in filled else 'CANCELLED'
        elif cmd['status'] not in TERMINAL:
            cmd['status'] = 'OBSOLETE'
        cmd['paper_interrupted'] = cycle['id']
        # The authoritative virtual exchange also seals previously READY/UNKNOWN
        # command IDs, so no stale dispatch can recreate an order after settlement.
        planned.p['results'][cid] = dict(status='DONE', paper_interrupted=cycle['id'])
    updated.update(cycle=None, phase='WAIT_START', stop=True, stop_mode='', last_candle=snap['candle']['id'])
    updated['notifications'].append(dict(id='paper-interrupt-' + cycle['id'], text='Бумажный цикл прерван. Позиции закрыты, ордера отменены. RECOVERY сохранён: ' + str(carry) + ' UST. Новый цикл только по кнопке «Старт».'))
    updated['notifications'] = updated['notifications'][-200:]
    if active_id:
        updated['control_receipts'][active_id]['status'] = 'APPLIED'
    # Planning may be slow on a long history. Recheck time immediately before
    # committing, so a clock change or an aged quote cannot close the model.
    if session.reader.safety.check() or controller.state.get('time_block'):
        reject('Время изменилось во время проверки; прерывание не выполнено.')
    fresh = session._fresh()
    if fresh['quote'] != snap['quote'] or fresh['account'] != snap['account']:
        reject('Котировка изменилась во время проверки; подтвердите прерывание заново.')
    _commit(session, expected, expected_exchange, updated, planned.p, settlement)
    # No fallible freshness/read step after the committed APPLIED receipt.
    session.snapshot = dict(snap, orders=[], positions=[], deals=deepcopy(exchange.p['deals']),
                            order_history=deepcopy(exchange.p['order_history']))


def _commit(session, expected, expected_exchange, state, exchange_state, settlement):
    store = session.store
    encode = lambda value: json.dumps(value, sort_keys=True, allow_nan=False)
    try:
        store.db.execute('BEGIN IMMEDIATE')
        stored_exchange = json.loads(store.db.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()[0])
        if store.load() != expected or stored_exchange != expected_exchange:
            raise DurabilityError('Stale paper interruption snapshot; reload before confirmation')
        store.verify_deal_ledger(expected)
        store.db.execute('UPDATE paper_exchange SET payload=? WHERE id=1', (encode(exchange_state),))
        if store.fault:
            store.fault('paper_interrupt_exchange_written')
        store.db.execute('UPDATE snapshot SET payload=? WHERE id=1', (encode(state),))
        if store.fault:
            store.fault('paper_interrupt_snapshot_written')
        store.db.execute('INSERT INTO audit(event,payload) VALUES(?,?)', ('paper_cycle_interrupted', encode(settlement)))
        if store.fault:
            store.fault('paper_interrupt_before_commit')
        store.db.execute('COMMIT')
        if store.fault:
            store.fault('paper_interrupt_after_commit')
    except BaseException as exc:
        if store.db.in_transaction:
            store.db.execute('ROLLBACK')
        if isinstance(exc, DurabilityError):
            raise
        raise DurabilityError('Atomic paper interruption failed; stop and reload durable state') from exc
    finally:
        session.controller.state = store.load()
        session.controller._durable_state = deepcopy(session.controller.state)
        session.exchange.p = json.loads(store.db.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()[0])
