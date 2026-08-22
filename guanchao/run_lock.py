from __future__ import annotations

import hashlib
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock(key: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[key] = lock
        return lock


def _lock_path(db_path: str, case_id: str) -> Path | None:
    if not db_path or db_path == ":memory:":
        return None
    database = Path(db_path).expanduser().resolve()
    digest = hashlib.sha256(
        f"{database}\0{case_id}".encode("utf-8", errors="replace")
    ).hexdigest()[:32]
    root = database.parent / ".guanchao-run-locks"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{database.name}-{digest}.lock"


@contextmanager
def run_claim(db_path: str, case_id: str) -> Iterator[None]:
    """Serialize active-run check + insert across Harness instances/processes.

    The in-process RLock covers multiple Store/Harness objects in one interpreter.
    On POSIX, flock extends the same critical section across worker processes that
    share the SQLite file. In-memory stores have no cross-process identity and use
    the thread lock only.
    """

    path = _lock_path(db_path, case_id)
    key = str(path) if path is not None else f"memory:{id(threading.current_thread())}:{case_id}"
    with _thread_lock(key):
        if path is None:
            yield
            return

        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                import fcntl  # POSIX deployment path
            except ImportError:
                # Thread-level protection is still correct inside one process.
                yield
                return
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
