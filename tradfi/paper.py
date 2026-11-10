"""Persistent paper exchange. Receives immutable market facts, never an MT5 object."""
from copy import deepcopy
from decimal import Decimal as D
import hashlib,json,uuid,time
from .recovery_v3 import ControlledRecoveryController
from .control import ActionRejected,ActionGateway,OwnerMailbox
from .broker import Spec
from .settings import number
from .engine import TERMINAL
from .storage import Store,DurabilityError

class PaperExchange:
    backend_id='paper_mt5_quotes_v1'
    read_only=False
    symbol='XAUUSD.s'
    def __init__(self,store,*,clock):
        self.store=store;self.clock=clock;self.market=None;self.available=False
        store.db.execute('CREATE TABLE IF NOT EXISTS paper_exchange (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        row=store.db.execute('SELECT payload FROM paper_exchange WHERE id=1').fetchone()
        self.p=json.loads(row[0]) if row else dict(identity='PAPER-'+uuid.uuid4().hex,orders=[],positions=[],deals=[],order_history=[],results={},cursor=None,cursor_keys=[],commission_per_lot_side='0',paused_gap=False)
        self.account=self.p['identity'];self.boot=True
        self.command_baseline=None;self.command_quote=None
        if row and (self.p['orders'] or self.p['positions']):self.p['paused_gap']=True
        self.persist()
    def persist(self):
        try:self.store.db.execute('INSERT OR REPLACE INTO paper_exchange VALUES(1,?)',(json.dumps(self.p,sort_keys=True,allow_nan=False),))
        except Exception as exc:raise DurabilityError('Paper persistence failed; stop and reload') from exc
    def profit(self,side,size,entry,close):
        market=self.p.get('last_verified_market') if self.command_baseline is not None else self.market
        if market is None:raise OSError('No verified MT5 specification')
        spec=Spec.load(market['spec']);movement=(number(close)-number(entry))*(1 if side=='BUY' else -1)
        # Frozen current native tick-value model; never claimed actual execution P&L.
        value=spec.tick_value_profit if movement>=0 else spec.tick_value_loss
        return movement/spec.tick_size*number(size)*value
    def _fee(self,size):return str(-number(size)*number(self.p['commission_per_lot_side']))
    def _close(self,p,reason,price,close_id=None):
        if self.command_quote is not None:
            from .paper_commands import quote_info
            self.command_quote=quote_info(self)
        self.p['positions'].remove(p)
        self.p['deals'].append(dict(id='out-'+p['id'],position_id=p['id'],order_id='exit-'+p['id'],command_id='',close_command_id=close_id,
            entry='OUT',side=p['side'],volume=p['size'],price=str(price),reason=reason,profit=str(self.profit(p['side'],p['size'],p['entry'],price)),
            commission=self._fee(p['size']),swap='0',fee='0',sl=p['sl'],tp=p['tp'],time=self.clock()))
        if self.command_quote is not None:self.p['deals'][-1]['paper_quote']=deepcopy(self.command_quote)
    def _fill(self,o,price):
        if self.command_quote is not None:
            from .paper_commands import quote_info
            self.command_quote=quote_info(self)
        self.p['orders'].remove(o);o['status']='FILLED';self.p['order_history'].append(deepcopy(o));pid='paper-p-'+o['id']
        self.p['positions'].append(dict(id=pid,side=o['side'],entry=str(price),size=o['size'],sl=o['sl'],tp=o['tp'],owned=True))
        self.p['deals'].append(dict(id='in-'+pid,position_id=pid,order_id=o['id'],command_id=o['command_id'],entry='IN',side=o['side'],volume=o['size'],price=str(price),reason='EXPERT',profit='0',commission=self._fee(o['size']),swap='0',fee='0',time=self.clock()))
        if self.command_quote is not None:self.p['deals'][-1]['paper_quote']=deepcopy(self.command_quote)
    def observe(self,market):
        if market.get('timestamp_semantics')!='VERIFIED':
            self.available=False
            raise ValueError('Paper requires verified timestamp semantics')
        if market['symbol']!=self.symbol:raise ValueError('Wrong paper symbol')
        Spec.load(market['spec'])
        bid,ask=number(market['quote']['bid']),number(market['quote']['ask'])
        if bid<=0 or ask<bid or number(market['timestamp'])<=0:
            raise ValueError('Invalid verified paper quote')
        if self.p['cursor'] is not None and market['tick_msc']<self.p['cursor']:return
        self.market=deepcopy(market)
        self.available=market['status']=='FRESH' and -2<=self.clock()-market['timestamp']<=5
        if not self.available:return
        self.p['last_verified_market']=deepcopy(market)
        stamp=market['tick_msc'];key=hashlib.sha256(json.dumps(market['quote'],sort_keys=True).encode()).hexdigest()
        previous=self.p['cursor']
        if previous is not None and (stamp<previous or (stamp==previous and key in self.p['cursor_keys'])):return
        if previous is not None and stamp-previous>5000 and (self.p['orders'] or self.p['positions']):self.p['paused_gap']=True
        # First sample after restart is a baseline, never an inferred historical fill.
        baseline=self.boot;self.boot=False
        if previous!=stamp:self.p['cursor_keys']=[]
        self.p['cursor']=stamp;self.p['cursor_keys'].append(key)
        if baseline or self.p['paused_gap']:self.persist();return
        q=market['quote']
        def close_crossed():
            for p in list(self.p['positions']):
                price=number(q['bid' if p['side']=='BUY' else 'ask']);sign=1 if p['side']=='BUY' else -1
                if sign*(price-number(p['sl']))<=0:self._close(p,'SL',price)
                elif sign*(price-number(p['tp']))>=0:self._close(p,'TP',p['tp'])  # no favorable gap windfall at TP
        close_crossed()
        for o in list(self.p['orders']):
            price=number(q['ask' if o['side']=='BUY' else 'bid']);sign=1 if o['side']=='BUY' else -1
            if sign*(price-number(o['price']))>=0:self._fill(o,price)
        close_crossed()  # gap may cross a newly filled ticket's unchanged SL
        self.persist()
    def snapshot(self):
        if self.command_baseline is not None:
            from .paper_commands import quote_info, reject
            if not quote_info(self)['available']:reject('Нет последней проверенной котировки PAPER. Цена не выдумана; дождитесь первого проверенного снимка.')
            market=self.p['last_verified_market']
            return dict(account=self.account,hedging=True,history_complete=True,spec=deepcopy(market['spec']),now=self.clock(),quote=deepcopy(market['quote']),candle=deepcopy(market['candle']),
                **{k:deepcopy(self.p[k]) for k in ('orders','positions','deals','order_history')})
        if not self.available or self.market is None or not -2<=self.clock()-self.market['timestamp']<=5:raise OSError('Paper waits for verified fresh MT5 data')
        return dict(account=self.account,hedging=True,history_complete=True,spec=deepcopy(self.market['spec']),now=self.clock(),quote=deepcopy(self.market['quote']),candle=deepcopy(self.market['candle']),
            **{k:deepcopy(self.p[k]) for k in ('orders','positions','deals','order_history')})
    def result(self,cid):return deepcopy(self.p['results'].get(cid,{'status':'UNKNOWN'}))
    def command_permitted(self,cid):
        return self.command_baseline is None or cid not in self.command_baseline
    def submit(self,cmd):
        if cmd['id'] in self.p['results']:return  # durable idempotency, no second paper effect
        kind=cmd['kind'];result={'status':'REJECTED','never_sent':True}
        try:
            snap=self.snapshot();spec=Spec.load(snap['spec']);q=snap['quote']
            if self.p['paused_gap'] and kind not in {'CANCEL'} and self.command_baseline is None:raise ValueError('Paper gap needs confirmation')
            if kind in {'STOP','MARKET'}:
                if cmd['side'] not in {'BUY','SELL'}:raise ValueError('side')
                size=number(cmd['size']);price=spec.price(q['ask' if cmd['side']=='BUY' else 'bid'] if kind=='MARKET' else cmd['price']);sign=1 if cmd['side']=='BUY' else -1
                if not spec.volume_min<=size<=spec.volume_max or size%spec.volume_step:raise ValueError('volume')
                if kind=='STOP' and sign*(price-number(q['ask' if sign==1 else 'bid']))<max(spec.tick_size,spec.point*spec.stops_level):raise ValueError('crossed STOP')
                spec.protection(cmd['side'],price,number(cmd['sl']),number(cmd['tp']),q if kind=='MARKET' else None)
                oid='paper-o-'+cmd['id'];self.p['orders'].append(dict(id=oid,command_id=cmd['id'],side=cmd['side'],status='PENDING',size=str(size),price=str(price),sl=cmd['sl'],tp=cmd['tp'],owned=True))
                result=dict(status='ACCEPTED',order_id=oid)
                if kind=='MARKET':self._fill(self.p['orders'][-1],price)
            elif kind=='CANCEL':
                o=next((o for o in self.p['orders'] if o['id']==cmd['order_id']),None)
                if o:
                    reference=number(q['ask' if o['side']=='BUY' else 'bid'])
                    if self.command_baseline is None and abs(number(o['price'])-reference)<spec.point*spec.freeze_level:raise ValueError('freeze')
                    self.p['orders'].remove(o);o['status']='CANCELLED';self.p['order_history'].append(o)
                result=dict(status='DONE')
            elif kind in {'MODIFY','CLOSE'}:
                p=next(p for p in self.p['positions'] if p['id']==cmd['position_id'])
                if kind=='MODIFY':
                    spec.protection(p['side'],number(p['entry']),number(cmd['sl']),number(cmd['tp']),q,modify=True);p.update(sl=cmd['sl'],tp=cmd['tp']);result=dict(status='DONE')
                elif number(cmd['size'])==number(p['size']):
                    self._close(p,cmd['purpose'],q['bid' if p['side']=='BUY' else 'ask'],cmd['id']);result=dict(status='ACCEPTED')
        except (ValueError,StopIteration):pass
        self.p['results'][cmd['id']]=result;self.persist()

class PaperSession:
    def __init__(self,path,reader,*,clock,monotonic):
        self.store=Store(path);self.reader=reader;self.clock=clock;self.monotonic=monotonic
        from .market_time_state import bind_market_time_state
        try:
            existing=self.store.load()
            if existing and existing.get('backend')!=PaperExchange.backend_id:
                raise DurabilityError('Paper cannot adopt another controller database')
            identity=getattr(reader, '_expected_account', None)
            if identity is None and hasattr(reader, 'read_raw'):
                # Direct callers may omit expected_account. Establish the reader's
                # verified source identity before recording a persistent binding.
                reader.read_raw()
                identity=reader._expected_account
            login, server=(identity.split('@', 1) if identity is not None else (None, None))
            bind_market_time_state(self.store, dict(
                market_time_offset_seconds=getattr(reader, 'market_time_offset_seconds', 0),
                expected_login=int(login) if login is not None else None, expected_server=server))
        except BaseException:
            self.store.close()
            raise
        self.exchange=PaperExchange(self.store,clock=clock)
        self.controller=ControlledRecoveryController(self.store,self.exchange,clock=clock,monotonic=monotonic)
        from .paper_interrupt import recover_receipts
        recover_receipts(self)
        from .paper_commands import PaperCommandMailbox
        self.scope=self.exchange.account;self.gateway=ActionGateway(self.controller,execute=self.action);self.mailbox=PaperCommandMailbox(monotonic=monotonic,max_age_seconds=2,current_execution_view=self._execution_view,max_queue_age_seconds=3);self._execution_started=None;self._execution_ok=False;self.snapshot=None
        self.presentation=None
        from .detailed_diagnostics import DetailedDiagnostics
        self.diagnostics=DetailedDiagnostics(clock)
        self.gateway.diagnostics=self.diagnostics
        self._diagnostic_last_step=0
    def _fresh(self):
        try:return self.exchange.snapshot()
        except OSError as exc:raise ActionRejected('Ожидание данных MT5',public_message='Ожидаются проверенные свежие котировки MT5; бумажное действие не выполнено.') from exc
    def action(self,a):
        from .paper_commands import command_scope, advance_command, cancel_without_quote
        with command_scope(self,a):
            if cancel_without_quote(self,a):return
            self._action(a)
            advance_command(self,a)
            from .paper_commands import quote_info
            quote=quote_info(self.exchange)
            self.controller.state['last_paper_command_quote']=quote
            active=getattr(self.controller,'_active_control_id',None)
            if active:self.controller.state['control_receipts'][active]['paper_quote']=quote
            self.controller.save('paper_command_quote')
    def _action(self,a):
        if not isinstance(a,dict):raise ActionRejected('Неверная бумажная команда')
        if self.controller.state.get('migration_block'):raise ActionRejected(self.controller.state['migration_block'])
        if a.get('type')=='paper_interrupt':
            from .paper_interrupt import interrupt_cycle
            return interrupt_cycle(self,a)
        if a.get('type')=='paper_costs' and set(a)=={'type','commission_per_lot_side'}:
            try:value=number(a['commission_per_lot_side']);assert value>=0
            except (ValueError,AssertionError):raise ActionRejected('Нужна неотрицательная модель комиссии')
            self.exchange.p['commission_per_lot_side']=str(value);self.exchange.persist();return
        if a==dict(type='paper_resume'):
            self._fresh()
            if self.controller._deadline_guard():raise ActionRejected('Нужна ручная сверка: цикл ограничен временем или уже остановлен защитой')
            self.exchange.p['paused_gap']=False;self.exchange.persist();return
        if a==dict(type='paper_flatten',confirmed=True):
            snap=self._fresh()
            for o in list(self.exchange.p['orders']):self.exchange.p['orders'].remove(o);o['status']='CANCELLED';self.exchange.p['order_history'].append(o)
            for p in list(self.exchange.p['positions']):self.exchange._close(p,'MANUAL',snap['quote']['bid' if p['side']=='BUY' else 'ask'])
            self.exchange.persist();self.controller.state['stop']=True;self.controller.save('paper_flatten');return
        if a==dict(type='command',text='/reset_manual'):
            c=self.controller.state.get('cycle')
            if not c or not c.get('manual'):raise ActionRejected('Нет бумажного цикла для сверки')
            snap=self._fresh()
            if snap['orders'] or snap['positions']:raise ActionRejected('Сначала закройте бумажные обязательства')
            if any(cmd['status']=='UNKNOWN' and self.exchange.result(cid)['status']=='UNKNOWN' for cid,cmd in self.controller.state['commands'].items()):raise ActionRejected('Неизвестный исход сохранён')
            ids=set(c['entries']);pids={d['position_id'] for d in snap['deals'] if d['entry']=='IN' and d['command_id'] in ids}
            net=sum((sum((number(d[k]) for k in ('profit','commission','swap','fee')),D(0)) for d in snap['deals'] if d['position_id'] in pids),D(0))
            s=self.controller.state;carry=max(D(0),number(s.get('basket_carry','0'))-net-number(c.get('recovery_waived','0')));s['basket_carry']=str(carry)
            s['completed'].append(dict(id=c['id'],currency='UST',contract='ticket_basket_v1',recovery_retained=str(carry),completion='PAPER_MANUAL_RESET'))
            for cmd in s['commands'].values():
                if cmd['status'] not in TERMINAL:cmd['status']='OBSOLETE'
            s.update(cycle=None,phase='WAIT_START',stop=True);self.controller.save('paper_manual_reset');return
        if a==dict(type='command',text='/resume_time'):
            if self.reader.safety.check():raise ActionRejected('Ожидаются проверенные свежие данные MT5')
            return
        if self.exchange.p['paused_gap'] and a==dict(type='command',text='/stop'):
            self.controller.command('/stop')
            for o in list(self.exchange.p['orders']):
                self.exchange.p['orders'].remove(o);o['status']='CANCELLED';self.exchange.p['order_history'].append(o)
            self.exchange.persist();return
        if self.exchange.p['paused_gap'] and a!=dict(type='command',text='/stop') and self.exchange.command_baseline is None:raise ActionRejected('После перерыва подтвердите продолжение симуляции без восстановления пропущенных тиков')
        if a!=dict(type='command',text='/stop'):self._fresh()
        return self.controller.action(a)
    def _execution_view(self):
        projection_started = self.mailbox.monotonic()
        view = self.view()
        now = self.mailbox.monotonic()
        age = self.mailbox._nonnegative(now-self._execution_started) if self._execution_started is not None else None
        valid = age is not None and age <= 2
        view['snapshot_freshness'] = dict(fresh=valid, age_seconds=age, max_age_seconds=2)
        feed = view.get('feed') or {}
        quote_age = self.mailbox._finite(feed.get('age_seconds'))
        projection_age = self.mailbox._nonnegative(now-projection_started)
        quote_age = quote_age+projection_age if quote_age is not None and projection_age is not None else None
        feed['age_seconds'] = quote_age
        if quote_age is None or not -2 <= quote_age <= 5:
            feed['status'] = 'WAITING'
        return view

    def step(self):
        self._execution_started=self.mailbox.monotonic();self._execution_ok=False
        had_requests=not self.mailbox.requests.empty()
        started=time.perf_counter(); marks={}; error_type=None
        try:
            before=time.perf_counter(); market=self.reader.read(); marks['read_ms']=(time.perf_counter()-before)*1000
            before=time.perf_counter(); self.exchange.observe(market); marks['observe_ms']=(time.perf_counter()-before)*1000
            before=time.perf_counter()
            if not self.exchange.p['paused_gap']:self.controller.tick()
            elif (self.controller.state.get('cycle') or {}).get('manual_reason')=='TP1_OWN_ENTRY_SL_UNAVAILABLE':
                # Recover only this obsolete policy latch; the independent
                # restart/data-gap pause still requires explicit PAPER resume.
                self.controller._restore_paper_tp1_pause(self.controller.snapshot())
            marks['tick_ms']=(time.perf_counter()-before)*1000
            self.snapshot=self.exchange.snapshot()
            self._execution_ok=True
        except DurabilityError:raise
        except Exception as exc:
            error_type=type(exc).__name__;self.exchange.available=False;self.reader.reason=self.reader.reason or 'WAIT_FRESH_MT5'
        before=time.perf_counter(); self.mailbox.drain(self.gateway,self.view); marks['publish_ms']=(time.perf_counter()-before)*1000
        now=time.perf_counter()
        # Heartbeat rather than a raw quote stream; failures are always observed.
        if error_type or had_requests or now-started>=1 or now-self._diagnostic_last_step>=10:
            self._diagnostic_last_step=now
            self.diagnostics.record('owner_step', status='UNAVAILABLE' if error_type else 'OK', error_type=error_type,
                duration_ms=round((now-started)*1000,3), queue_depth=self.mailbox.requests.qsize(),
                positions_count=len(self.exchange.p['positions']), orders_count=len(self.exchange.p['orders']),
                **{k:round(v,3) for k,v in marks.items()})
    def view(self):
        from .web_control import PaperPresentation
        if self.presentation is None:
            self.presentation=object.__new__(PaperPresentation);self.presentation.controller=self.controller;self.presentation.scope=self.scope
        s=self.controller.state;c=s.get('cycle') or {};snap=deepcopy(self.snapshot) if self.snapshot else {}
        snap.update({k:deepcopy(self.exchange.p[k]) for k in ('orders','positions')})
        v=self.presentation.basket_view(s,c,snap,self.reader.view())
        from .paper_position_history import position_history
        history=position_history(self.exchange.p,s.get('currency'),bool(s.get('completed')),s.get('commands',{}))
        v['position_history']=history['position_history'];v['metrics'].update(history['metrics'])
        v.update(mode='PAPER',source='PAPER_ON_MT5_READ_ONLY',paper=dict(paused_gap=self.exchange.p['paused_gap'],commission_per_lot_side=self.exchange.p['commission_per_lot_side'],cost_model='modeled commission per lot per side; swap/fee=0',history=deepcopy(self.exchange.p['deals'][-100:]),cursor=self.exchange.p['cursor'],last_interrupt=deepcopy(s.get('last_paper_interrupt'))))
        from .paper_commands import quote_info
        v['paper']['command_quote']=quote_info(self.exchange)
        v['paper']['last_command_quote']=deepcopy(s.get('last_paper_command_quote'))
        for rid,receipt in s.get('control_receipts',{}).items():
            if 'paper_quote' in receipt:v['action_results'][rid]['paper_quote']=deepcopy(receipt['paper_quote'])
        return v
    def close(self):self.store.close()
