"""PAPER-only candle-following recovery. No EA, terminal writes or live adapter.

Existing controller remains available to read legacy tests/contracts; PaperSession
selects this implementation. In-flight legacy groups are reconciled, never erased.
"""
from copy import deepcopy
from decimal import Decimal as D, ROUND_CEILING
from .recovery_v3 import ControlledRecoveryController
from .recovery_profiles import selected_snapshot
from .basket import BasketController
from .basket_math import BasketSettings
from .broker import Spec
from .control import ActionRejected
from .engine import TERMINAL
from .settings import number

PROFILES = {
 2: ('.30','.70'), 3: ('.30','.70','1'), 4: ('.50','1','1.50','2'),
 5: ('.70','1.30','1.90','2.40','2.70'),
 6: ('.70','1.30','2','2.80','3.30','3.90'),
 7: ('.70','1.30','2.30','3','3.50','4.30','4.90'),
 8: ('.70','1.30','2.30','3.30','4','4.70','5.10','5.60'),
 9: ('.70','1.30','2.30','3.30','4','4.70','5.50','6.30','6.90'),
 10: ('.70','1.30','2.30','3.30','4','4.70','5.50','6.50','7.50','8.20'),
}

def profile_defaults(sl='1.50'):
    return dict(selected_profile_id='volume_3',recovery_sl_distance=sl,profiles=[
        dict(id='volume_'+str(n),name=f'{n} × 0.01',stages=[dict(volume='0.01',tp_distance=v) for v in values])
        for n,values in PROFILES.items()])

class CandleRecoveryController(ControlledRecoveryController):
    def __init__(self,store,broker,**kwargs):
        existing=store.load()
        super().__init__(store,broker,**kwargs)
        if not self.state.get('candle_recovery_version'):
            # Keep old user profiles separately, and retain all active snapshots.
            self.state['archived_recovery_profiles']=deepcopy(self.state['recovery_profiles'])
            self.state['recovery_profiles']=profile_defaults(self.state['basket_settings']['RECOVERY_SL_DISTANCE'])
            if not existing:self.state['basket_settings']['INITIAL_SIZE']='0.02'
            self.state['candle_recovery_version']=1
        self.state.setdefault('candle_recovery_settings',dict(trigger_1='0.25',trigger_2='0.40',offset_1='0.25'))
        self.save('candle_recovery_loaded')

    def _new(self,snap):
        super()._new(snap)
        self.state['cycle']['follow_volume']=self.state['cycle']['cfg']['INITIAL_SIZE']
        self.save('candle_recovery_cycle')

    def _profile_for_volume(self,volume,spec,profile_id=None):
        settings=deepcopy(self.state['recovery_profiles'])
        matches=[p for p in settings['profiles'] if sum((number(s['volume']) for s in p['stages']),D(0))==number(volume)]
        if profile_id:matches=[p for p in matches if p['id']==profile_id]
        else:matches.sort(key=lambda p:p['id']!=settings['selected_profile_id'])
        if not matches:raise ActionRejected('Для этого объёма настройте профиль TP')
        settings['selected_profile_id']=matches[0]['id']
        return selected_snapshot(settings,spec)

    def action(self,a):
        kind=a.get('type')
        if kind=='v3_entry':raise ActionRejected('Ручное выставление удалено; используйте следование за свечами')
        if kind in ('candle_recovery_settings','candle_recovery_resume','candle_close_all'):
            snap=self._guard();c=self.state.get('cycle')
            if a.get('cycle_id')!=(c or {}).get('id'):raise ActionRejected('Цикл изменился')
            if kind=='candle_recovery_settings':
                if set(a)!={'type','cycle_id','trigger_1','trigger_2','offset_1'}:raise ActionRejected('Неверные настройки SL')
                values={k:number(a[k]) for k in ('trigger_1','trigger_2','offset_1')}
                spec=Spec.load(snap['spec'])
                if not 0<values['trigger_1']<values['trigger_2'] or values['offset_1']<=0:raise ActionRejected('Пороги SL должны быть положительными и возрастающими')
                for v in values.values():spec.price(v)
                self.state['candle_recovery_settings']={k:str(v) for k,v in values.items()};self.save('candle_sl_settings');return
            if not c:raise ActionRejected('Нет цикла')
            if kind=='candle_close_all':
                if set(a)!={'type','cycle_id'}:raise ActionRejected('Неверная команда')
                c['candle_close_all']=True;self.state['stop']=True;self.save('candle_close_all_requested');self._close_all(snap);return
            if set(a)!={'type','cycle_id','volume','profile_id'} or snap['positions'] or snap['orders'] or self._unresolved():raise ActionRejected('Возобновление требует отсутствия позиций и ордеров')
            profile=self._profile_for_volume(a['volume'],Spec.load(snap['spec']),a['profile_id'])
            c.update(follow_volume=profile['total_volume'],manual_required=False,recovery_follow=True,follow_after=snap['candle']['id'])
            c.pop('follow_profile',None)
            self.state['recovery_profiles']['selected_profile_id']=profile['id'];self.state['stop']=False
            self.save('candle_recovery_resumed');return
        return super().action(a)

    def _observe(self,snap):
        if not super()._observe(snap):return False
        from .paper_evidence_index import paper_index
        index=paper_index(self,snap)
        c=self.state['cycle']
        for t in c['tickets'].values():
            original=self.state['commands'][t['command_id']]['sl']
            t.setdefault('original_sl',original)
            p=next(iter(index.live.get(t['id'],[])),None) if index else next((p for p in snap['positions'] if p['id']==t['id']),None)
            # Closing facts cover a move and stop between two controller polls.
            closes=index.outs_position.get(t['id'],[]) if index else [d for d in snap['deals'] if d.get('position_id')==t['id'] and d.get('entry')=='OUT']
            levels=([p['sl']] if p else [])+[d['sl'] for d in closes if d.get('sl') is not None]
            if any(number(sl)!=number(original) for sl in levels):t['support_sl']=True
        return True

    def _source(self,snap,c,expected):
        active=sorted([t for t in c['tickets'].values() if not t['closed']],key=lambda t:t['index'])
        if not active or len({t['group'] for t in active})!=1 or sorted(t['id'] for t in active)!=sorted(expected) or sorted(p['id'] for p in snap['positions'])!=sorted(expected):raise ActionRejected('Состав текущей корзины изменился')
        live={p['id']:p for p in snap['positions']}
        if len({number(live[t['id']]['sl']) for t in active})!=1:raise ActionRejected('Для связанных триггеров нужен общий уровень SL')
        group=next(g for g in c['groups'] if g['id']==active[0]['group'])
        return active[0],group,live[active[0]['id']]

    def _request_link(self,a,*,recovery_profile=None,legacy=False):
        if set(a)!={'type','cycle_id','expected_positions'} or not isinstance(a['expected_positions'],list):raise ActionRejected('Неверные поля связанных триггеров')
        snap,c=self._context(a)
        if snap['orders'] or self._unresolved() or c.get('v3_link') or c.get('v3_request'):raise ActionRejected('Сначала сверка прежних ордеров')
        source,g,p=self._source(snap,c,a['expected_positions'])
        tickets=[t for t in c['tickets'].values() if t['group']==g['id']]
        active=[t for t in tickets if not t['closed']];live={p['id']:p for p in snap['positions']}
        full=len(tickets)==len(g['commands']) and all(not t['closed'] and not t.get('support_sl') and number(live[t['id']]['sl'])==number(self.state['commands'][t['command_id']]['sl']) for t in tickets)
        volume=sum((number(self.state['commands'][cid]['size']) for cid in g['commands']),D(0))
        if full and volume>=D('.10'):raise ActionRejected('После полного SL 0.10 выберите следующий объём вручную')
        profile=self._profile_for_volume(volume+(D('.01') if full else D(0)),Spec.load(snap['spec']))
        net=number(c['realized'])+sum((number(self.broker.profit(t['side'],number(t['size']),number(t['entry']),number(p['sl']))) for t in active),D(0))
        if hasattr(self.broker,'_fee'):net+=sum((number(self.broker._fee(number(t['size']))) for t in active),D(0))
        side='SELL' if source['side']=='BUY' else 'BUY'
        group=self._arm_v3(snap,dict(order_type='STOP',recovery_profile=profile),side,p['sl'],max(D(0),-net),persist=False)
        group['linked_source']=g['id']
        c['v3_link']=dict(source_position=source['id'],source_group=g['id'],group=group['id'],ids=group['commands'][:],count=len(profile['stages']),side=side,price=p['sl'],cancel=False,recovery_profile=deepcopy(profile))
        self.save('candle_fixed_link_created')

    def _advance_link(self,snap):
        # Manual SL triggers are fixed-price instructions, never trailing.
        c=self.state['cycle'];link=c.get('v3_link')
        if not link:return False
        group=next(g for g in c['groups'] if g['id']==link['group'])
        if link.get('cancel'):
            self._cancel(snap,link['ids'])
            if self._pending(snap,link['ids']) or not self._resolved(link['ids']):return True
            group['retired']=not any(t['group']==group['id'] for t in c['tickets'].values())
            c.pop('v3_link');self.save('fixed_link_cancelled');return False
        if all(self.state['commands'][i]['status'] in TERMINAL for i in link['ids']):
            c.pop('v3_link');self.save('fixed_link_resolved')
        return False

    def _with_fixed_link(self,method,*args,**kwargs):
        c=self.state.get('cycle');link=c.pop('v3_link',None) if c else None
        try:return method(*args,**kwargs)
        finally:
            if link:c['v3_link']=link

    def edit(self,**kwargs):
        c=self.state.get('cycle') or {}
        if c.get('v3_link') and kwargs.get('protection') is not None:
            protection=kwargs['protection']
            if number(protection['new']['tp'])!=number(protection['old']['tp']) and number(protection['new']['sl'])==number(protection['old']['sl']):return self._edit_dragged_tp(protection)
            return BasketController.edit(self,**kwargs)
        return self._with_fixed_link(super().edit,**kwargs)
    def _adjust_sl(self,a):
        c=self.state.get('cycle') or {}
        if not c.get('v3_link'):return super()._adjust_sl(a)
        if set(a)!={'type','cycle_id','distance','expected_positions'}:raise ActionRejected('Неверные поля SL')
        snap,c=self._context(a);active=[t for t in c['tickets'].values() if not t['closed']]
        if sorted(a['expected_positions'])!=sorted(p['id'] for p in snap['positions']) or not active or len({t['group'] for t in active})!=1 or c.get('edit') or self._uncertain():raise ActionRejected('Состав корзины изменился или идёт правка')
        spec=Spec.load(snap['spec']);distance=spec.price(a['distance'])
        if distance<=0 or (distance-number(self._active_sl_distance(snap,c,active)))%D('.10'):raise ActionRejected('Недопустимый шаг SL')
        updates=[]
        for t in active:
            p=next(p for p in snap['positions'] if p['id']==t['id']);sl=spec.price(number(t['entry'])-(1 if t['side']=='BUY' else -1)*distance)
            spec.protection(t['side'],number(t['entry']),sl,number(p['tp']),snap['quote'],modify=True)
            updates.append(dict(position_id=t['id'],sl=str(sl),tp=p['tp']))
        cfg=deepcopy(c['cfg']);cfg['INITIAL_SL_DISTANCE' if active[0]['role']=='initial' else 'RECOVERY_SL_DISTANCE']=str(distance)
        c['edit']=dict(stage='CANCEL',settings=cfg,levels=None,updates=updates,cancel_ids=[],groups=[],fills=sorted(c['tickets']),closed=sorted(pid for pid,t in c['tickets'].items() if t['closed']),modify_ids=[],expected_protection={p['id']:{k:p[k] for k in ('sl','tp')} for p in snap['positions']},v3_sl_group=active[0]['group'],v3_sl_distance=str(distance))
        self.save('fixed_link_sl_adjust_requested')

    def _cancel_v3_entry(self,a):
        super()._cancel_v3_entry(a)
        self.state['cycle']['follow_after']=self.snapshot()['candle']['id']
        self.save('candle_cancel_wait_new_close')
    def _blocking_protection_orders(self,snap):
        c=self.state.get('cycle') or {}
        linked={cid for g in c.get('groups',[]) if g.get('linked_source') for cid in g['commands']}
        return [o for o in snap['orders'] if o.get('command_id') not in linked]

    def _plans_v3(self,snap,request,side,level=None,debt=None):
        plans=super()._plans_v3(snap,request,side,level,debt)
        if plans:
            targets=[dict(index=i,entry=p['price'],size=p['size'],tp=p['tp']) for i,p in enumerate(plans)]
            tp=self._last_target(Spec.load(snap['spec']),side,targets,number(self.state['cycle']['recovery'] if debt is None else debt))
            if tp is not None:plans[-1]['tp']=tp
        return plans

    def _last_target(self,spec,side,tickets,debt):
        """Other still-open projected TPs are deducted once, not realized twice."""
        last=tickets[-1];others=sum((number(self.broker.profit(side,number(t['size']),number(t['entry']),number(t['tp']))) for t in tickets[:-1]),D(0))
        reserve=number(self.state['cycle']['cfg']['COST_RESERVE'])
        future_cost=-sum((number(self.broker._fee(number(t['size']))) for t in tickets),D(0)) if hasattr(self.broker,'_fee') else D(0)
        goal=number(debt)+reserve+future_cost-others
        # A nonpositive residual never turns the last TP into a loss/market exit.
        if goal<=0:return None
        sign=1 if side=='BUY' else -1;entry=number(last['entry'])
        unit=number(self.broker.profit(side,number(last['size']),entry,entry+sign*spec.tick_size))
        if unit<=0:return None
        ticks=max(1,int((goal/unit).to_integral_value(rounding=ROUND_CEILING)))
        return str(spec.price(entry+sign*ticks*spec.tick_size))

    def _tp1_reverse_confirmed(self,c,group):
        """Threshold SL replaces the retired TP1 transfer transaction.

        Require actual TP evidence and read-back breakeven protection for every
        remaining position, not an intent or optimistic controller field.
        """
        first=[t for t in c.get('tickets',{}).values() if t.get('group')==group['id'] and t.get('index')==0 and t.get('role')!='initial']
        if len(first)!=1:return False
        first=first[0];facts=first.get('exit_facts') or []
        active=[t for t in c['tickets'].values() if t['group']==group['id'] and not t['closed']]
        try:
            if not first.get('closed') or first.get('reason')!='TP' or not facts or not active:return False
            if not all(f.get('reason')=='TP' and number(f['volume'])>0 for f in facts) or sum(number(f['volume']) for f in facts)!=number(first['size']):return False
            live={p['id']:p for p in self.snapshot()['positions']}
            for t in active:
                p=live[t['id']];sign=1 if t['side']=='BUY' else -1
                if p['side']!=t['side'] or number(p['entry'])!=number(t['entry']) or number(p['size'])!=number(t['size']) or sign*(number(p['sl'])-number(t['entry']))<0:return False
            return True
        except (KeyError,TypeError,ValueError,ArithmeticError,OSError,TimeoutError):return False

    def _manage_positions(self,snap):
        c=self.state['cycle'];spec=Spec.load(snap['spec']);cfg=self.state['candle_recovery_settings']
        for g in c['groups']:
            active=sorted([t for t in c['tickets'].values() if t['group']==g['id'] and not t['closed']],key=lambda t:t['index'])
            if not active:continue
            live={p['id']:p for p in snap['positions']}
            targets=[dict(t,tp=live[t['id']]['tp']) for t in active if t['id'] in live]
            last_tp=self._last_target(spec,active[-1]['side'],targets,number(c['recovery'])) if targets and g['role']!='initial' else None
            last=active[-1];manual=g.get('manual_tp_distances',{}).get(str(last['index']))
            if manual is not None and last['id'] in live and (1 if last['side']=='BUY' else -1)*(number(live[last['id']]['tp'])-number(last['entry']))==number(manual):last_tp=None
            for t in active:
                p=live.get(t['id'])
                if not p or any(cmd['kind']=='MODIFY' and cmd.get('position_id')==t['id'] and cmd['status'] not in TERMINAL for cmd in self.state['commands'].values()):continue
                sign=1 if t['side']=='BUY' else -1;entry=number(t['entry']);sl=number(p['sl']);tp=number(p['tp'])
                progress=sign*(number(snap['quote']['bid' if sign==1 else 'ask'])-entry)
                desired=entry if progress>=number(cfg['trigger_2']) else entry-sign*number(cfg['offset_1']) if progress>=number(cfg['trigger_1']) else sl
                if sign*(desired-sl)>0:sl=desired
                if t is active[-1] and last_tp is not None:tp=number(last_tp)
                if sl==number(p['sl']) and tp==number(p['tp']):continue
                try:spec.protection(t['side'],entry,sl,tp,snap['quote'],modify=True)
                except ValueError:
                    # An unreachable adjusted TP must not suppress a valid SL move.
                    tp=number(p['tp'])
                    if sl==number(p['sl']):continue
                    try:spec.protection(t['side'],entry,sl,tp,snap['quote'],modify=True)
                    except ValueError:continue
                self._intent('MODIFY',position_id=t['id'],sl=str(sl),tp=str(tp),purpose='CANDLE_PROTECTION')
        self.save('candle_protection_checked')

    def _close_all(self,snap):
        c=self.state['cycle'];self._cancel(snap,c['entries'])
        for t in c['tickets'].values():
            if not t['closed'] and not any(self.state['commands'][cid]['position_id']==t['id'] for cid in c.get('cleanup_ids',[])):
                c.setdefault('cleanup_ids',[]).append(self._intent('CLOSE',position_id=t['id'],size=t['size'],purpose='MANUAL'))
        if not snap['positions'] and not snap['orders'] and not self._unresolved():
            c.pop('candle_close_all',None);self.state['phase']='PAUSED_CYCLE'
        self.save('candle_close_all_staged')

    def _step(self,snap):
        c=self.state['cycle']
        if not self._observe(snap):return
        if c.get('manual'):return
        if c.get('candle_close_all'):
            self._close_all(snap)
            return
        if self._advance_entry_cancel(snap):return
        if self._advance_link(snap):return
        if c.get('edit') and self._advance_edit(snap):return
        active=[t for t in c['tickets'].values() if not t['closed']]
        if active:
            self.state['phase']='INITIAL_POSITION' if all(t['role']=='initial' for t in active) else 'RECOVERY_POSITION'
            self._manage_positions(snap);return
        # Retire each settled basket exactly once, including across restart.
        for g in c['groups']:
            tickets=[t for t in c['tickets'].values() if t['group']==g['id']]
            if not tickets or g.get('outcome_booked') or not self._resolved(g['commands']):continue
            if any(not t['closed'] for t in tickets):continue
            g['outcome_booked']=True;g['retired']=True
            c.pop('follow_profile',None)
            full=(len(tickets)==len(g['commands']) and all(t.get('reason')=='SL' and not t.get('support_sl') for t in tickets))
            volume=sum((number(t['size']) for t in tickets),D(0))
            c['follow_volume']=str(volume+D('.01') if full else volume)
            c['manual_required']=bool(full and volume>=D('.10'))
            c['recovery_follow']=True;c['follow_after']=snap['candle']['id']
            self.save('candle_group_outcome')
        if c['tickets'] and number(c['recovery'])<=0:
            self._finish(snap);return
        if self.state['stop']:
            self._cancel(snap,c['entries']);self.state['phase']='PAUSED_CYCLE';return
        if c.get('manual_required'):self.state['phase']='V3_WAIT_ENTRY';return
        if not c.get('recovery_follow'):
            # Original initial lifecycle incl. cancellation and manual movement.
            return BasketController._step(self,snap)
        if c.get('v3_link'):self.state['phase']='RECOVERY_PENDING';return
        self._follow_recovery(snap)

    def _follow_recovery(self,snap):
        c=self.state['cycle'];bar=snap['candle'];pending=[g for g in c['groups'] if not g.get('retired') and not g.get('linked_source')]
        direction='BUY' if number(bar['close'])>number(bar['open']) else 'SELL' if number(bar['close'])<number(bar['open']) else None
        if not direction:return
        latest=pending[-1] if pending else None
        if latest:
            old_side=next(iter(latest['levels']))
            if bar['id']<=c.get('follow_after',-1) or direction==old_side:return
            self._cancel(snap,latest['commands'])
            if self._pending(snap,latest['commands']) or not self._resolved(latest['commands']):return
            latest['retired']=True
        if snap['orders'] or self._unresolved() or bar['id']<=c.get('follow_after',-1):return
        try:
            profile=deepcopy(c.get('follow_profile')) or self._profile_for_volume(c['follow_volume'],Spec.load(snap['spec']))
            level=str(number(bar['close'])+(1 if direction=='BUY' else -1)*number(self.state['v3_settings']['entry_distance']))
            self._arm_v3(snap,dict(order_type='STOP',recovery_profile=profile),direction,level)
            c['follow_profile']=deepcopy(profile)
            c['follow_after']=bar['id']
        except (ActionRejected,ValueError):self.state['phase']='RECOVERY_WAIT'

    def v3_view(self,snap):
        result=super().v3_view(snap);c=self.state.get('cycle') or {}
        result['candle_recovery']=dict(settings=deepcopy(self.state['candle_recovery_settings']),volume=c.get('follow_volume',self.state['basket_settings']['INITIAL_SIZE']),manual_required=bool(c.get('manual_required')))
        result['v3_controls']['can_enter']=False
        if c and self._v3() and not getattr(self.broker,'read_only',False) and self.state.get('phase') not in ('SAFE_PAUSE','MANUAL','MIGRATION_BLOCK') and not self.state.get('time_block') and not c.get('edit') and not c.get('v3_edit') and not c.get('manual') and not self.state['stop'] and not c.get('v3_link') and not snap.get('orders') and not self._unresolved():
            try:self._source(snap,c,[p['id'] for p in snap.get('positions',[])]);result['v3_controls']['can_link']=True
            except (ActionRejected,KeyError):pass
        return result
