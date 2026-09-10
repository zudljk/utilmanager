"""Serialize photo deletion and backups across web threads and CLI processes."""
import fcntl
from contextlib import contextmanager


@contextmanager
def photo_lock(data_dir):
    with (data_dir / '.photos.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
