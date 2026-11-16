"""Ephemeral PAPER evidence lookup. Never cached, filtered, or persisted."""
from collections import defaultdict


class PaperEvidenceIndex:
    def __init__(self, snap):
        self.orders = snap['orders'] + snap['order_history']
        self.order_command = defaultdict(list)
        self.order_id = defaultdict(list)
        for i, row in enumerate(self.orders):
            self.order_command[row.get('command_id')].append(i)
            self.order_id[row['id']].append(i)
        self.ins_command = defaultdict(list)
        self.ins_order = defaultdict(list)
        self.outs_position = defaultdict(list)
        for row in snap['deals']:
            if row['entry'] == 'IN':
                self.ins_command[row.get('command_id')].append(row)
                self.ins_order[row.get('order_id')].append(row)
            elif row['entry'] == 'OUT':
                self.outs_position[row.get('position_id')].append(row)
        self.live = defaultdict(list)
        for row in snap['positions']:
            self.live[row['id']].append(row)
        self.terminal_orders = {row['id'] for row in snap['order_history']
                                if row['status'] in {'CANCELLED', 'FILLED', 'REJECTED'}}

    def entry_orders(self, cid, order_id):
        # OR selection preserves original list order, even overlapping keys.
        indices = self.order_command.get(cid, ())
        if order_id:
            indices = sorted(set(indices).union(self.order_id.get(order_id, ())))
        return [self.orders[i] for i in indices]


def paper_index(controller, snap):
    if getattr(controller.broker, 'backend_id', None) == 'paper_mt5_quotes_v1':
        # Malformed legacy/imported evidence must keep the original validation
        # path, not become an absent indexed match and appear reconciled.
        try:
            if any(not isinstance(row,dict) or not {'id','status'} <= row.keys()
                   for row in snap['orders']+snap['order_history']):return None
            if any(not isinstance(row,dict) or not {'entry','position_id','order_id'} <= row.keys()
                   for row in snap['deals']):return None
            if any(not isinstance(row,dict) or 'id' not in row for row in snap['positions']):return None
            return PaperEvidenceIndex(snap)
        except (KeyError,TypeError):
            return None
    return None
