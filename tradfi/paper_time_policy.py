"""PAPER has no technical clock deadline; strategy/manual stops remain explicit."""


class PaperTimePolicy:
    def _deadline_guard(self):
        c = self.state.get('cycle')
        if not c:
            return False
        # These latches belonged to historical real-execution clock policy.
        # Do not erase strategy limits, an explicit stop, or accounting failures.
        obsolete = {'INVALID_CYCLE_CLOCK', 'INVALID_CYCLE_DEADLINE',
                    'UTC_CLOCK_MOVED_BACKWARDS', 'CYCLE_24H_LIMIT',
                    'MISSING_OR_INVALID_CYCLE_DEADLINE'}
        if c.get('manual') and c.get('manual_reason') in obsolete:
            c['manual'] = False
            c.pop('manual_reason', None)
            c.pop('manual_since_utc', None)
            if self.state.get('stop'):
                self.state['phase'] = 'PAUSED_CYCLE'
            else:
                self.state['phase'] = 'RECOVERY_WAIT' if c.get('tickets') else 'INITIAL_WAIT'
            self.save('paper_clock_latch_removed')
        if c.get('manual'):
            self.state['phase'] = 'MANUAL'
            return True
        return False

    def _time_guard(self):
        # Market timestamps are coordinates for the simulation, not permission.
        if self.state.get('time_block'):
            self.state['time_block'] = None
            self.save('paper_time_gate_removed')
        return False
