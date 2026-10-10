"""Per-account locks."""

import threading
from contextlib import contextmanager
from typing import Generator


class LockManager:
    """One threading.Lock per account, created on first use.
    multi_lock() takes locks in sorted order, so two transfers can't deadlock.
    """

    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()

    def _get_lock(self, account_id: str) -> threading.Lock:
        """Get or create the lock for a given account."""
        with self._meta_lock:
            if account_id not in self._locks:
                self._locks[account_id] = threading.Lock()
            return self._locks[account_id]

    @contextmanager
    def lock(self, account_id: str) -> Generator[None, None, None]:
        """Context manager that acquires the lock for a single account."""
        lk = self._get_lock(account_id)
        lk.acquire()
        try:
            yield
        finally:
            lk.release()

    @contextmanager
    def multi_lock(self, *account_ids: str) -> Generator[None, None, None]:
        """Context manager that acquires locks for multiple accounts in sorted order."""
        sorted_ids = sorted(set(account_ids))
        locks = [self._get_lock(aid) for aid in sorted_ids]
        # Acquire all locks in sorted order
        for lk in locks:
            lk.acquire()
        try:
            yield
        finally:
            # Release in reverse order
            for lk in reversed(locks):
                lk.release()
