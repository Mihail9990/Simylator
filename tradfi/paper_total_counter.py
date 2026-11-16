"""Display-only PAPER P/L baseline. Never changes trades or recovery accounting."""
from .control import ActionRejected
from .paper_position_history import _number
from .sl_overlap_recovery import VARIANT


def counter_metrics(history_metrics, state):
    result=dict(history_metrics)
    lifetime=result.get('all_closed_net')
    result.update(lifetime_closed_net=lifetime,total_reset_available=False,total_reset_at=None,total_reset_note='')
    if state.get('simulator_variant') != VARIANT:return result
    try:
        if not result.get('history_complete'):raise ValueError()
        total=_number(lifetime)
        baseline=state.get('paper_total_baseline')
        amount=0
        if baseline is not None:
            if not isinstance(baseline,dict) or baseline.get('currency') != state.get('currency'):raise ValueError()
            amount=_number(baseline['amount'])
            stamp=_number(baseline['reset_at'])
            if stamp<=0:raise ValueError()
            result['total_reset_at']=float(stamp)
        result['all_closed_net']=str(total-amount)
        result['total_reset_available']=True
    except (KeyError,TypeError,ValueError,ArithmeticError):
        result['all_closed_net']=None
        result['total_reset_note']='Счётчик недоступен: история или сохранённая точка отсчёта требуют проверки.'
    return result


def reset_total(session, action):
    if set(action) != {'type','confirmed'} or action.get('confirmed') is not True:
        raise ActionRejected('Нужно подтвердить сброс только отображаемого общего результата.')
    state=session.controller.state
    history=session._history_cache.view(session.exchange.p,state.get('currency'),bool(state.get('completed')),state.get('commands',{}))
    metrics=counter_metrics(history['metrics'],state)
    if not metrics['total_reset_available']:
        raise ActionRejected('Общий результат не подтверждён.',public_message=metrics['total_reset_note'])
    active=getattr(session.controller,'_active_control_id',None)
    receipt=state.get('control_receipts',{}).get(active) if active else None
    if active and (not receipt or not receipt.get('paper_total_reset_atomic')):
        raise ActionRejected('Не подтверждена атомарная команда сброса счётчика.')
    stamp=_number(session.clock())
    if stamp<=0:raise ActionRejected('Недоступно время сохранения точки отсчёта.')
    state['paper_total_baseline']=dict(amount=metrics['lifetime_closed_net'],currency=state['currency'],reset_at=float(stamp))
    # Baseline and successful receipt share one normal controller transaction.
    # A crash cannot leave an applied reset marked UNKNOWN on restart.
    if receipt is not None:receipt['status']='APPLIED'
    session.controller.save('paper_total_counter_reset')


def recover_receipts(session):
    changed=False
    for receipt in session.controller.state.get('control_receipts',{}).values():
        if receipt.get('status')=='UNKNOWN' and receipt.get('paper_total_reset_atomic'):
            receipt.update(status='REJECTED',reason='Сброс счётчика не был сохранён. Общий результат, история и RECOVERY не изменены этой командой; подтвердите заново.')
            changed=True
    if changed:session.controller.save('paper_total_reset_reservation_reconciled')
