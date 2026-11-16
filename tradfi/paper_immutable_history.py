"""Owner-local read-only history rows; persistence and public snapshots stay mutable."""
from copy import deepcopy
from .paper_cache_equality import fingerprint


class FrozenHistoryRow(dict):
    def _immutable(self, *args, **kwargs):
        raise TypeError('Execution history is read-only')
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


def freeze(value):
    if isinstance(value, dict):
        return FrozenHistoryRow((key, freeze(item)) for key, item in value.items())
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze(item) for item in value)
    return deepcopy(value)


class ImmutableHistory:
    def __init__(self):
        self.source = []
        self.rows = []
        self.encoded = fingerprint([])

    def snapshot(self, rows):
        # Full equality detects corrections, not just append count/newest ID.
        encoded=fingerprint(rows)
        if encoded != self.encoded:
            length = len(self.source)
            if len(rows) >= length and fingerprint(rows[:length]) == self.encoded:
                fresh = deepcopy(rows[length:])
                self.source.extend(fresh)
                self.rows.extend(freeze(row) for row in fresh)
            else:
                self.source = deepcopy(rows)
                self.rows = [freeze(row) for row in self.source]
            self.encoded=encoded
        return list(self.rows)
