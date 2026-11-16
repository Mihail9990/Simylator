"""PAPER variant: fixed original-SL overlap with debt-selected ladders.

Pending ladders are reconciled to projected debt with cancel-confirm-rebuild.
Protection moves never move its entry. Confirmed accounting is never projected.
"""
from copy import deepcopy
from decimal import Decimal as D
from .candle_recovery import CandleRecoveryController, profile_defaults
from .basket import BasketController
from .broker import Spec
from .control import ActionRejected
from .settings import number
from .storage import DurabilityError

VARIANT = 'sl-overlap-recovery-v1'
DEFAULT_THRESHOLDS = [dict(upper=v, profile_id=f'volume_{i+3}') for i,v in enumerate(('3.5','6.5','10','15','21','28','36','50'))]
PROFILES = {
    3: ('.30', '.70', '1'), 4: ('.50', '1', '1.50', '2'),
    5: ('.70', '1.30', '1.90', '2.40', '2.70'),
    6: ('.70', '1.30', '2', '2.80', '3.30', '3.90'),
    7: ('.70', '1.30', '2.30', '3', '3.50', '4.30', '4.90'),
    8: ('.70', '1.30', '2', '3', '3.70', '4.70', '5.70', '5.90'),
    9: ('.70', '1.30', '1.70', '3.30', '4', '5', '5.70', '6.30', '7'),
    10: ('.70', '1.30', '1.70', '3.30', '4', '5', '5.70', '6.30', '7', '9'),
}


def volume_for_debt(debt):
    debt = number(debt)
    if debt > 50:
        return None
    for limit, count in (('3.5', 3), ('6.5', 4), ('10', 5), ('15', 6),
                         ('21', 7), ('28', 8), ('36', 9)):
        if debt < D(limit):
            return D(count) / 100
    return D('.10')


class SLOverlapRecoveryController(CandleRecoveryController):
    def __init__(self, store, broker, **kwargs):
        existing = store.load()
        if existing and existing.get('simulator_variant') != VARIANT:
            raise DurabilityError('This variant requires a separate database; old simulator state is not migrated')
        super().__init__(store, broker, **kwargs)
        self.state.setdefault('overlap_thresholds', deepcopy(DEFAULT_THRESHOLDS))
        if not existing:
            self.state['simulator_variant'] = VARIANT
            self.state['basket_settings'].update(INITIAL_SIZE='0.02', INITIAL_ENTRY_DISTANCE='0.80',
                INITIAL_SL_DISTANCE='1.00', INITIAL_TP_DISTANCE='0.25', RECOVERY_SL_DISTANCE='1.00')
            profiles = profile_defaults('1.00')
            profiles['profiles'] = [dict(id=f'volume_{n}', name=f'{n} × 0.01',
                stages=[dict(volume='0.01', tp_distance=v) for v in values]) for n, values in PROFILES.items()]
            self.state['recovery_profiles'] = profiles
            self.state['v3_settings']['entry_distance'] = '0.80'
            self.save('sl_overlap_variant_created')

    def _projected_debt(self, snap, active):
        c = self.state['cycle']; live = {p['id']: p for p in snap['positions']}
        net = number(c['realized']) + number(c.get('recovery_waived', '0'))
        for t in active:
            p = live[t['id']]
            net += number(self.broker.profit(t['side'], number(t['size']), number(t['entry']), number(p['sl'])))
            if hasattr(self.broker, '_fee'):
                net += number(self.broker._fee(number(t['size'])))
        return max(D(0), -net)

    def _desired_profile(self, snap, debt):
        threshold = self._threshold(debt)
        if threshold is None:
            return None
        chosen = next(p for p in self.state['recovery_profiles']['profiles'] if p['id'] == threshold['profile_id'])
        volume = sum((number(stage['volume']) for stage in chosen['stages']), D(0))
        return self._profile_for_volume(volume, Spec.load(snap['spec']), threshold['profile_id'])

    def _auto_link(self, snap, active):
        c = self.state['cycle']
        if self.state['stop'] or len({t['group'] for t in active}) != 1:
            return
        source = active[0]; g = next(g for g in c['groups'] if g['id'] == source['group'])
        tickets = [t for t in c['tickets'].values() if t['group'] == g['id']]
        if len(tickets) != len(g['commands']) or not self._resolved(g['commands']):
            return
        logical = c.setdefault('overlap_ladders', {})
        key = g['id'] + ':reverse'
        if key not in logical:
            levels = {number(t['original_sl']) for t in tickets}
            if len(levels) != 1:
                c['manual_required'] = True
                c['overlap_wait_reason'] = 'У исходной корзины разные первоначальные SL'
                return
            logical[key] = dict(source_group=g['id'], source_position=source['id'],
                side='SELL' if source['side'] == 'BUY' else 'BUY', price=str(next(iter(levels))), kind='reverse')
            self.save('overlap_logical_ladder_created')

    def _support_reentry(self, snap):
        c = self.state['cycle']; logical = c.setdefault('overlap_ladders', {})
        for g in c['groups']:
            tickets = [t for t in c['tickets'].values() if t['group'] == g['id']]
            if not tickets or any(not t['closed'] for t in tickets) or not self._resolved(g['commands']):
                continue
            reverse = logical.get(g['id'] + ':reverse')
            if not reverse or reverse.get('disabled') or reverse.get('filled'):
                continue
            # No re-entry when the opposite batch already executed in this same
            # observation; its actual volume is immutable and owns continuation.
            reverse_group = reverse.get('group')
            if any(t['group'] == reverse_group for t in c['tickets'].values()):
                continue
            key = g['id'] + ':support'
            if key in logical:
                continue
            by_id = {t['id']: t for t in tickets}
            exits = [d for d in snap['deals'] if d.get('entry') == 'OUT' and d.get('position_id') in by_id]
            if not exits:
                continue
            latest = max(number(d['time']) for d in exits)
            support = [d for d in exits if number(d['time']) == latest and d.get('reason') == 'SL'
                       and d.get('sl') is not None
                       and (1 if by_id[d['position_id']]['side'] == 'BUY' else -1) *
                           (number(d['sl']) - number(by_id[d['position_id']]['original_sl'])) > 0]
            if not support:
                continue
            levels = {number(d['sl']) for d in support}
            if len(levels) != 1:
                c['overlap_wait_reason'] = 'Разные уровни SL поддержки: требуется ручное продолжение'
                continue
            source = tickets[0]; level = next(iter(levels)); sign = 1 if source['side'] == 'BUY' else -1
            distance = number(self.state['v3_settings']['entry_distance'])
            price = Spec.load(snap['spec']).price(level + sign * distance)
            logical[key] = dict(source_group=g['id'], source_position=source['id'], side=source['side'],
                price=str(price), kind='support', support_sl=str(level), distance=str(distance))
            self.save('overlap_support_reentry_created')

    def _reconcile_oco(self, snap):
        c = self.state['cycle']; ladders = c.get('overlap_ladders', {})
        sources = {}
        for key, link in ladders.items():
            if any(t['group'] == link.get('group') for t in c['tickets'].values()):
                link['filled'] = True
            if link.get('filled'):
                sources.setdefault(link['source_group'], []).append(key)
        if any(len(keys) > 1 for keys in sources.values()):
            self._halt(snap, 'OVERLAP_OCO_BOTH_FILLED_REQUIRES_REVIEW')
            return False
        for key, link in ladders.items():
            winners = sources.get(link['source_group'], [])
            if winners and key not in winners:
                link['disabled'] = True
                link['oco_cancelled_by'] = winners[0]
        self.save('overlap_oco_reconciled')
        return True

    def _sync_ladders(self, snap):
        """Cancel-confirm-rebuild only unfilled logical ladders; prices never move.

        A race that fills any physical command permanently freezes that basket's
        volume. Replacement never adds another batch after observing such a fill.
        """
        c = self.state['cycle']
        for key, link in c.get('overlap_ladders', {}).items():
            group = next((g for g in c['groups'] if g['id'] == link.get('group')), None)
            filled = [t for t in c['tickets'].values() if group and t['group'] == group['id']]
            if filled:
                link['filled'] = True
                continue
            if link.get('filled'):
                continue
            source_active = [t for t in c['tickets'].values() if t['group'] == link['source_group'] and not t['closed']]
            debt = self._projected_debt(snap, source_active)
            profile = None if link.get('disabled') else self._desired_profile(snap, debt)
            plans = None
            if profile is not None:
                try:
                    plans = self._plans_v3(snap, dict(order_type='STOP', recovery_profile=profile), link['side'], link['price'], debt)
                except (ActionRejected, ValueError):
                    # Existing crossed STOPs belong to the exchange. Do not
                    # replace them by a market order or cancel on a guessed fill.
                    c['overlap_wait_reason'] = 'Уровень ожидающей лестницы уже пересечён или недопустим'
                    continue
            signature = dict(profile=profile, targets=[p['tp'] for p in plans]) if plans else None
            if group and not link.get('replacing') and signature == link.get('signature'):
                continue
            if group:
                link['replacing'] = True
                self.save('overlap_replacement_requested')
                self._cancel(snap, group['commands'])
                if self._pending(snap, group['commands']) or not self._resolved(group['commands']):
                    continue
                group['retired'] = True
                if c.get('v3_link', {}).get('group') == group['id']:
                    c.pop('v3_link')
                link.pop('group', None); link.pop('replacing', None)
                self.save('overlap_replacement_cancel_confirmed')
            if profile is None:
                continue
            if self.state['stop'] or self._uncertain():
                continue
            # OCO replacement waits for read-back cancellation of its sibling,
            # not merely a successful CANCEL submit acknowledgement.
            settling = [other for other in c.get('overlap_ladders', {}).values()
                        if other is not link and (other.get('disabled') or other.get('replacing')) and other.get('group')]
            if any(not self._resolved(next(g for g in c['groups'] if g['id'] == other['group'])['commands']) for other in settling):
                continue
            group = self._arm_v3(snap, dict(order_type='STOP', recovery_profile=profile), link['side'], link['price'], debt, persist=False)
            group['linked_source'] = link['source_group']; group['overlap_key'] = key
            group['projected_recovery_at_placement'] = str(debt)
            link.update(group=group['id'], signature=signature)
            if link['kind'] == 'reverse':
                c['v3_link'] = dict(source_position=link['source_position'], source_group=link['source_group'], group=group['id'],
                    ids=group['commands'][:], count=len(profile['stages']), side=link['side'], price=link['price'],
                    cancel=False, recovery_profile=deepcopy(profile))
            c['follow_volume'] = profile['total_volume']; c['manual_required'] = False
            c.pop('overlap_wait_reason', None)
            self.save('overlap_pending_profile_reconciled')

    def _request_link(self, a, **kwargs):
        raise ActionRejected('В этой версии встречная лестница выставляется автоматически на первоначальный SL')

    def _step(self, snap):
        c = self.state['cycle']
        if not self._observe(snap) or c.get('manual'):
            return
        if not self._reconcile_oco(snap):
            return
        if c.get('candle_close_all'):
            self._close_all(snap); return
        if self._advance_entry_cancel(snap) or self._advance_link(snap):
            return
        if c.get('edit') and self._advance_edit(snap):
            return
        # Retire settled source baskets even while a reverse basket is active.
        # Manual candle continuation must never mistake an old initial group for
        # a still-pending same-direction trigger.
        for g in c['groups']:
            tickets = [t for t in c['tickets'].values() if t['group'] == g['id']]
            if tickets and all(t['closed'] for t in tickets) and self._resolved(g['commands']):
                g['retired'] = True
        active = [t for t in c['tickets'].values() if not t['closed']]
        if active and all(t['role'] != 'initial' for t in active) and (number(c['recovery']) <= 0 or c.get('cleanup')):
            c['cleanup'] = True
            self.state['phase'] = 'RECOVERY_CLEANUP'
            self._cancel(snap, c['entries'])
            if not snap['orders'] and not self._uncertain():
                existing = {self.state['commands'][cid]['position_id'] for cid in c.get('cleanup_ids', [])}
                for t in active:
                    if t['id'] not in existing:
                        c.setdefault('cleanup_ids', []).append(self._intent('CLOSE', position_id=t['id'], size=t['size'], purpose='MANUAL'))
                self.save('overlap_recovered_cleanup')
            return
        if not active:
            c.pop('cleanup', None)
        if active:
            c.pop('overlap_manual_resume', None)
            c.pop('recovery_follow', None)
            c.pop('follow_profile', None)
            self.state['phase'] = 'INITIAL_POSITION' if all(t['role'] == 'initial' for t in active) else 'RECOVERY_POSITION'
            self._auto_link(snap, active)
            self._sync_ladders(snap)
            self._manage_positions(snap)
            return
        if c['tickets'] and number(c['recovery']) <= 0:
            self._finish(snap); return
        if self.state['stop']:
            self._cancel(snap, c['entries']); self.state['phase'] = 'PAUSED_CYCLE'; return
        if self._threshold(number(c['recovery'])) is None and not c.get('overlap_manual_resume'):
            self._cancel(snap, c['entries'])
            c['manual_required'] = True; self.state['phase'] = 'V3_WAIT_ENTRY'; return
        self._support_reentry(snap)
        self._sync_ladders(snap)
        if not c.get('recovery_follow') and not snap['orders'] and not self._unresolved():
            self._follow_closed_gap(snap)
        if c.get('recovery_follow'):
            self.state['phase'] = 'RECOVERY_WAIT'
            self._follow_recovery(snap); return
        if c['tickets'] and (c.get('v3_link') or snap['orders'] or self._unresolved()):
            self.state['phase'] = 'RECOVERY_PENDING'; return
        if c['tickets']:
            c['manual_required'] = True
            c.setdefault('overlap_wait_reason', 'Нет встречных ордеров: выберите профиль продолжения вручную')
            self.state['phase'] = 'V3_WAIT_ENTRY'; return
        BasketController._step(self, snap)

    def _follow_closed_gap(self, snap):
        """Resume only a fully observed fill+close with no reverse ever created.

        A disabled reverse is an owner's cancellation, not a gap fallback. The
        latest filled group owns this decision so older groups cannot restart a
        later manually cancelled basket. Retain a durable one-shot marker.
        """
        c = self.state['cycle']
        groups = [(g, [t for t in c['tickets'].values() if t['group'] == g['id']])
                  for g in c['groups']]
        filled = [(g, tickets) for g, tickets in groups if tickets]
        if not filled:
            return
        g, tickets = filled[-1]
        if (g.get('gap_follow_started') or g['id'] + ':reverse' in c.get('overlap_ladders', {})
                or len(tickets) != len(g['commands']) or any(not t['closed'] for t in tickets)
                or not self._resolved(g['commands'])):
            return
        profile = self._desired_profile(snap, number(c['recovery']))
        if profile is None:
            return
        g['gap_follow_started'] = True
        c.update(recovery_follow=True, follow_after=snap['candle']['id'],
                 follow_volume=profile['total_volume'], follow_profile=deepcopy(profile),
                 manual_required=False)
        c.pop('overlap_wait_reason', None)
        self.save('overlap_closed_gap_candle_follow')

    def _threshold(self, debt):
        thresholds = self.state['overlap_thresholds']
        for i, row in enumerate(thresholds):
            if debt < number(row['upper']) or (i == len(thresholds)-1 and debt == number(row['upper'])):
                return row
        return None

    def action(self, a):
        if a.get('type') == 'v3_cancel_entry':
            c = self.state.get('cycle') or {}
            logical = [link for link in c.get('overlap_ladders', {}).values() if not link.get('filled') and not link.get('disabled')]
            if logical:
                if set(a) != {'type', 'cycle_id'} or a['cycle_id'] != c.get('id'):
                    raise ActionRejected('Цикл изменился или неверные поля')
                snap = self._guard()
                ids = []
                for link in logical:
                    link['disabled'] = True
                    if link.get('group'):
                        ids.extend(next(g for g in c['groups'] if g['id'] == link['group'])['commands'])
                if c.get('v3_link'):
                    c['v3_link']['cancel'] = True
                self.save('overlap_pair_cancel_requested')
                self._cancel(snap, ids)
                return
        if a.get('type') == 'v3_cancel_link':
            c = self.state.get('cycle') or {}
            group_id = c.get('v3_link', {}).get('group')
            result = super().action(a)
            for link in c.get('overlap_ladders', {}).values():
                if link.get('group') == group_id:
                    link['disabled'] = True
            self.save('overlap_logical_ladder_cancelled')
            return result
        if a.get('type') == 'recovery_profiles':
            rows = a.get('profiles', [])
            ids = {p.get('id') for p in rows if isinstance(p, dict)} if isinstance(rows, list) else set()
            if any(row['profile_id'] not in ids for row in self.state['overlap_thresholds']):
                raise ActionRejected('Сначала уберите профиль из диапазонов RECOVERY, затем удаляйте его')
        if a.get('type') == 'overlap_thresholds':
            c = self.state.get('cycle')
            if set(a) != {'type', 'cycle_id', 'thresholds'} or a['cycle_id'] != (c or {}).get('id'):
                raise ActionRejected('Цикл изменился или неверные поля')
            rows = a['thresholds']; profiles = {p['id'] for p in self.state['recovery_profiles']['profiles']}
            if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
                raise ActionRejected('Нужно от 1 до 20 диапазонов')
            previous = D(0); validated = []
            try:
                for row in rows:
                    if not isinstance(row, dict) or set(row) != {'upper', 'profile_id'} or row['profile_id'] not in profiles:
                        raise ValueError('Неизвестный профиль')
                    upper = number(row['upper'])
                    if upper <= previous:
                        raise ValueError('Границы должны быть положительными и возрастать')
                    validated.append(dict(upper=str(upper), profile_id=row['profile_id'])); previous = upper
            except (ValueError, TypeError, ArithmeticError) as exc:
                raise ActionRejected(str(exc)) from exc
            self.state['overlap_thresholds'] = validated
            self.save('overlap_thresholds_updated')
            return
        result = super().action(a)
        if a.get('type') == 'v3_cancel_entry' and self.state.get('cycle'):
            # An accepted owner cancellation also cancels candle continuation.
            self.state['cycle'].pop('recovery_follow', None)
            self.state['cycle'].pop('follow_profile', None)
            self.save('overlap_candle_follow_cancelled')
        if a.get('type') == 'candle_recovery_resume':
            self.state['cycle']['overlap_manual_resume'] = True
            self.save('overlap_manual_continuation')
        return result

    def v3_view(self, snap):
        view = super().v3_view(snap)
        view['v3_controls']['can_link'] = False
        c = self.state.get('cycle') or {}
        if c and not c.get('manual') and not c.get('edit') and self.state['phase'] not in {'SAFE_PAUSE', 'MANUAL'}:
            if any(not link.get('filled') and not link.get('disabled') for link in c.get('overlap_ladders', {}).values()):
                view['v3_controls']['can_cancel_entry'] = True
        view['candle_recovery']['variant'] = VARIANT
        view['candle_recovery']['thresholds'] = deepcopy(self.state['overlap_thresholds'])
        view['candle_recovery']['wait_reason'] = (self.state.get('cycle') or {}).get('overlap_wait_reason')
        return view
