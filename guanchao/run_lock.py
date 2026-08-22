from __future__ import annotations

import asyncio
import contextvars
import errno
import hashlib
import os
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import AsyncIterator, Iterator

from .run_lease import observe_running_case, prepare_case_claim

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}
_HELD_CLAIMS: contextvars.ContextVar[frozenset[str]] = contextvars.ContextVar(
    "guanchao_held_case_claims", default=frozenset()
)


def _thread_lock(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            # A plain Lock is intentional. Async acquisition may happen in one
            # task/thread and release in another; logical re-entrancy is handled
            # by the ContextVar below rather than by thread ownership.
            lock = threading.Lock()
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


def _identity(db_path: str, case_id: str) -> tuple[str, Path | None]:
    path = _lock_path(db_path, case_id)
    # Independent :memory: databases do not share durable state. Serializing a
    # same-named case across them is harmless and, importantly, protects multiple
    # Harness objects that share one in-memory Store from racing in different
    # worker threads.
    key = str(path) if path is not None else f"memory:{case_id}"
    return key, path


def _fcntl_module():
    try:
        import fcntl
    except ImportError:
        return None
    return fcntl


def _acquire_file_blocking(path: Path) -> int | None:
    fcntl = _fcntl_module()
    if fcntl is None:
        return None
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _try_acquire_file(path: Path) -> int | None:
    """Try one non-blocking flock attempt; None means another process owns it."""
    fcntl = _fcntl_module()
    if fcntl is None:
        return -1
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return None
        raise


def _release_file(descriptor: int | None) -> None:
    if descriptor is None or descriptor < 0:
        return
    fcntl = _fcntl_module()
    try:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _lease_case(case_id: str) -> bool:
    # Internal global claims (currently learning reconciliation) are serialized
    # by the same primitive but are not case/run ownership claims.
    return bool(case_id) and not case_id.startswith("__guanchao_")


def _before_case_claim(db_path: str, case_id: str) -> None:
    if _lease_case(case_id):
        prepare_case_claim(db_path, case_id)


def _after_case_claim(db_path: str, case_id: str) -> None:
    if _lease_case(case_id):
        observe_running_case(db_path, case_id)


@contextmanager
def run_claim(db_path: str, case_id: str) -> Iterator[None]:
    """Serialize all evidence-snapshot mutations for one case.

    Besides mutual exclusion, the outermost durable case claim now reconciles run
    ownership: an expired crashed worker is failed before the caller inspects the
    active row, and a run created inside the claim is adopted by this process and
    heartbeated after the durable snapshot is committed.
    """

    key, path = _identity(db_path, case_id)
    held = _HELD_CLAIMS.get()
    if key in held:
        yield
        return

    lock = _thread_lock(key)
    lock.acquire()
    descriptor: int | None = None
    token: contextvars.Token[frozenset[str]] | None = None
    completed = False
    try:
        if path is not None:
            descriptor = _acquire_file_blocking(path)
        _before_case_claim(db_path, case_id)
        token = _HELD_CLAIMS.set(held | {key})
        yield
        completed = True
    finally:
        # Only adopt on a normally completed claim. A rejected/failed mutation may
        # have observed somebody else's live run and must never claim its lease.
        if completed:
            _after_case_claim(db_path, case_id)
        if token is not None:
            _HELD_CLAIMS.reset(token)
        _release_file(descriptor)
        lock.release()


@asynccontextmanager
async def async_run_claim(db_path: str, case_id: str) -> AsyncIterator[None]:
    """Async, cancellation-safe form of :func:`run_claim`.

    Acquisition uses non-blocking polling instead of parking a thread in flock or
    Lock.acquire. Lease reconciliation itself is deliberately tiny SQLite work and
    runs while the cross-process case claim is held, so a cancelled request cannot
    leave a background adoption task racing after the lock has been released.
    """

    key, path = _identity(db_path, case_id)
    held = _HELD_CLAIMS.get()
    if key in held:
        yield
        return

    lock = _thread_lock(key)
    acquired_thread_lock = False
    descriptor: int | None = None
    token: contextvars.Token[frozenset[str]] | None = None
    completed = False
    try:
        while not lock.acquire(blocking=False):
            await asyncio.sleep(0.01)
        acquired_thread_lock = True

        if path is not None:
            while descriptor is None:
                descriptor = _try_acquire_file(path)
                if descriptor is None:
                    await asyncio.sleep(0.01)

        _before_case_claim(db_path, case_id)
        token = _HELD_CLAIMS.set(held | {key})
        yield
        completed = True
    finally:
        if completed:
            _after_case_claim(db_path, case_id)
        if token is not None:
            _HELD_CLAIMS.reset(token)
        _release_file(descriptor)
        if acquired_thread_lock:
            lock.release()
