"""Read-only position history from the complete persisted PAPER execution ledger."""
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation


def _number(value):
    if isinstance(value,bool) or value is None:raise ValueError()
    result=Decimal(str(value))
    if not result.is_finite():raise ValueError()
    return result


def _price(rows):
    if not rows:return None
    total=sum((_number(x['volume']) for x in rows),Decimal(0))
    if total<=0:raise ValueError()
    values=[(_number(x['price']),_number(x['volume'])) for x in rows]
    if any(price<=0 or volume<=0 for price,volume in values):raise ValueError()
    return str(sum((price*volume for price,volume in values),Decimal(0))/total)


def _time(rows,last=False):
    if not rows:return None
    values=[_number(x['time']) for x in rows]
    if any(t<=0 or t>253402300799 for t in values):raise ValueError()
    return float((max if last else min)(values))


def position_history(exchange,currency,has_completed=False,commands=None):
    deals=exchange.get('deals',[]);groups=defaultdict(list);complete=True
    identities=Counter(str(d.get('id')) for d in deals)
    live={p.get('id') for p in exchange.get('positions',[])}
    for deal in deals:
        if not deal.get('position_id') or deal.get('entry') not in {'IN','OUT'}:
            complete=False;continue
        groups[deal['position_id']].append(deal)
    if commands is not None:
        observed={d.get('command_id') for d in deals if d.get('entry')=='IN'}
        filled={cid for cid,cmd in commands.items() if cmd.get('kind') in {'STOP','MARKET'} and cmd.get('status')=='FILLED'}
        if not filled<=observed:complete=False
    records=[];total=Decimal(0)
    for pid,rows in groups.items():
        ins=[d for d in rows if d.get('entry')=='IN'];outs=[d for d in rows if d.get('entry')=='OUT']
        row=dict(side=None,opened_at=None,closed_at=None,entry_price=None,exit_price=None,
                 reason=None,net=None,currency=currency,status='incomplete',complete=True)
        invalid=any(not d.get('id') or identities[str(d['id'])]!=1 for d in rows)
        sides={d.get('side') for d in rows}
        if len(sides)==1 and next(iter(sides)) in {'BUY','SELL'}:row['side']=next(iter(sides))
        else:invalid=True
        for key,part,fn in [('entry_price',ins,_price),('exit_price',outs,_price),('opened_at',ins,_time),('closed_at',outs,lambda r:_time(r,True))]:
            try:row[key]=fn(part)
            except (KeyError,TypeError,ValueError,InvalidOperation):row['complete']=False
        if outs and any(not d.get('reason') for d in outs):row['complete']=False
        if row['opened_at'] is not None and row['closed_at'] is not None and row['closed_at']<row['opened_at']:row['complete']=False
        reasons={d.get('reason') for d in outs}
        if len(reasons)==1:row['reason']=next(iter(reasons))
        elif len(reasons)>1:row['reason']='MIXED'
        try:
            vin=sum((_number(d['volume']) for d in ins),Decimal(0));vout=sum((_number(d['volume']) for d in outs),Decimal(0))
            if not ins or vin<=0 or any(_number(d['volume'])<=0 for d in rows):raise ValueError()
            if not outs and pid in live:row['status']='open'
            elif outs and vout==vin and pid not in live:row['status']='closed'
            elif outs and 0<vout<vin and pid in live:row['status']='partial'
            else:raise ValueError()
            if invalid:raise ValueError()
            if row['status']=='closed':
                # Both sides' costs count exactly once. Carry/waivers never enter here.
                net=sum((_number(d[k]) for d in rows for k in ('profit','commission','swap','fee')),Decimal(0))
                row['net']=str(net);total+=net
        except (KeyError,TypeError,ValueError,InvalidOperation):
            row['status']='incomplete';row['complete']=False;row['net']=None
        if not row['complete']:complete=False
        records.append(row)
    if live-set(groups) or (has_completed and not deals):complete=False
    records.sort(key=lambda r:r['opened_at'] or 0,reverse=True)
    return dict(position_history=records,metrics=dict(all_closed_net=str(total) if complete else None,
                history_complete=complete,history_note='' if complete else 'Часть сохранённых данных исполнений неполна. Общий результат недоступен; неизвестные значения показаны как —.'))
