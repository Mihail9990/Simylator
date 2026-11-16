"""In-memory change fingerprints only: bytes are never deserialized or persisted."""
import pickle


def fingerprint(value):
    # Protocol 5 preserves bool/int/float and signed zero. Alias/key-order changes
    # may cause harmless cache misses; no external pickle is ever consumed.
    return pickle.dumps(value, protocol=5)


def exact_equal(left, right):
    return left is right or fingerprint(left) == fingerprint(right)
