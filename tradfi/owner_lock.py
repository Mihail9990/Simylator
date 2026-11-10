"""Exclusive local state lock. No trading dependencies."""
from contextlib import contextmanager
import os

@contextmanager
def owner_lock(path):
    """OS lock released on crash; protects the whole state-read/intent/send owner lifecycle."""
    handle = open(path, 'a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        handle.close()
