from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

GLOBAL_CAPACITY_ERROR = "global run capacity reached"


class RunLeaseLostError(RuntimeError):
    """The worker no longer owns the durable running row."""


_SCHEMA_GUARD = threading.Lock()
_SCHEMA_READY: dict[str, tuple[int, int]] = {}
_WORKER_GUARD = threading.Lock()
_WORKER_PID = -1
_WORKER_TOKEN = ""


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def configured_global_inflight() -> int:
    workers = _env_int("GUANCHAO_MAX_WORKERS", 8, 2, 128)
    configured = _env_int("GUANCHAO_MAX_INFLIGHT", 256, 1, 100_000)
    return max(workers, configured)


def is_global_capacity_error(exc: BaseException) -> bool:
    return GLOBAL_CAPACITY_ERROR in str(exc).lower()


def lease_seconds() -> int:
    return _env_int("GUANCHAO_RUN_LEASE_SECONDS", 30, 5, 7200)


def legacy_grace_seconds() -> int:
    return _env_int("GUANCHAO_LEGACY_RUN_GRACE_SECONDS", 300, 30, 86400)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _deadline(seconds: int) -> str:
    seconds = max(5, min(86400, int(seconds)))
    return _iso(_utcnow() + timedelta(seconds=seconds))


def _db_key(db_path: str) -> str:
    return str(Path(db_path).expanduser().resolve())


def _db_identity(db_path: str) -> tuple[int, int] | None:
    try:
        stat = Path(db_path).stat()
    except OSError:
        return None
    identity = (int(stat.st_dev), int(stat.st_ino))
    return None if identity == (0, 0) else identity


def worker_id() -> str:
    global _WORKER_PID, _WORKER_TOKEN
    pid = os.getpid()
    with _WORKER_GUARD:
        if _WORKER_PID != pid or not _WORKER_TOKEN:
            _WORKER_PID = pid
            _WORKER_TOKEN = f"{pid}-{uuid.uuid4().hex[:16]}"
        return _WORKER_TOKEN


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def ensure_schema(conn: sqlite3.Connection, grace_seconds: int | None = None) -> None:
    """Install run ownership, global capacity, and terminal-state invariants."""
    lease_table_existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'run_leases'"
    ).fetchone() is not None

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_leases (
            run_id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            lease_until TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_run_leases_until ON run_leases(lease_until)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_runtime_limits (
            id INTEGER PRIMARY KEY CHECK(id = 1),
            max_inflight INTEGER NOT NULL CHECK(max_inflight >= 1),
            updated_at TEXT NOT NULL
        )
        """
    )

    now = _iso(_utcnow())
    conn.execute(
        """
        INSERT INTO run_runtime_limits(id, max_inflight, updated_at) VALUES(1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            max_inflight = excluded.max_inflight,
            updated_at = excluded.updated_at
        """,
        (configured_global_inflight(), now),
    )

    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_runs_global_capacity_insert
        BEFORE INSERT ON runs
        WHEN NEW.status = 'running'
        BEGIN
            SELECT CASE WHEN
                (SELECT COUNT(*) FROM runs WHERE status = 'running') >=
                COALESCE((SELECT max_inflight FROM run_runtime_limits WHERE id = 1), 256)
            THEN RAISE(ABORT, 'global run capacity reached') END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_runs_global_capacity_update
        BEFORE UPDATE OF status ON runs
        WHEN NEW.status = 'running' AND OLD.status != 'running'
        BEGIN
            SELECT CASE WHEN
                (SELECT COUNT(*) FROM runs WHERE status = 'running') >=
                COALESCE((SELECT max_inflight FROM run_runtime_limits WHERE id = 1), 256)
            THEN RAISE(ABORT, 'global run capacity reached') END;
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_runs_terminal_immutable
        BEFORE UPDATE ON runs
        WHEN OLD.status IN ('completed', 'failed')
             AND (NEW.status != OLD.status OR NEW.state_json != OLD.state_json)
        BEGIN
            SELECT RAISE(ABORT, 'terminal run is immutable');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_runs_terminal_release_lease
        AFTER UPDATE OF status ON runs
        WHEN NEW.status != 'running'
        BEGIN
            DELETE FROM run_leases WHERE run_id = NEW.id;
        END
        """
    )

    if not lease_table_existed:
        deadline = _deadline(grace_seconds or legacy_grace_seconds())
        conn.execute(
            """
            INSERT OR IGNORE INTO run_leases(run_id, worker_id, lease_until)
            SELECT id, 'legacy-worker', ? FROM runs WHERE status = 'running'
            """,
            (deadline,),
        )
    conn.execute(
        "DELETE FROM run_leases WHERE run_id IN (SELECT id FROM runs WHERE status != 'running')"
    )


def _ensure_path_schema(db_path: str) -> None:
    if not db_path or db_path == ":memory:":
        return
    key = _db_key(db_path)
    identity = _db_identity(db_path)
    with _SCHEMA_GUARD:
        if identity is not None and _SCHEMA_READY.get(key) == identity:
            return
        conn = _connect(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            ensure_schema(conn)
            conn.commit()
            current_identity = _db_identity(db_path)
            if current_identity is not None:
                _SCHEMA_READY[key] = current_identity
            else:
                _SCHEMA_READY.pop(key, None)
        except BaseException:
            try:
                conn.rollback()
            finally:
                _SCHEMA_READY.pop(key, None)
            raise
        finally:
            conn.close()


def insert_lease(
    conn: sqlite3.Connection,
    run_id: str,
    owner: str,
    seconds: int,
) -> None:
    conn.execute(
        "INSERT INTO run_leases(run_id, worker_id, lease_until) VALUES (?, ?, ?)",
        (run_id, owner, _deadline(seconds)),
    )


def assert_and_renew(
    conn: sqlite3.Connection,
    run_id: str,
    owner: str,
    seconds: int,
) -> None:
    row = conn.execute(
        """
        SELECT r.status, l.worker_id
        FROM runs r LEFT JOIN run_leases l ON l.run_id = r.id
        WHERE r.id = ?
        """,
        (run_id,),
    ).fetchone()
    if not row or row["status"] != "running" or row["worker_id"] != owner:
        raise RunLeaseLostError(run_id)
    changed = conn.execute(
        "UPDATE run_leases SET lease_until = ? WHERE run_id = ? AND worker_id = ?",
        (_deadline(seconds), run_id, owner),
    ).rowcount
    if changed != 1:
        raise RunLeaseLostError(run_id)


def release_lease(conn: sqlite3.Connection, run_id: str, owner: str | None = None) -> None:
    if owner is None:
        conn.execute("DELETE FROM run_leases WHERE run_id = ?", (run_id,))
        return
    changed = conn.execute(
        "DELETE FROM run_leases WHERE run_id = ? AND worker_id = ?",
        (run_id, owner),
    ).rowcount
    if changed != 1:
        raise RunLeaseLostError(run_id)


def active_or_reclaim(
    conn: sqlite3.Connection,
    case_id: str,
    now_iso: str,
) -> tuple[sqlite3.Row | None, bool]:
    row = conn.execute(
        """
        SELECT r.*, l.worker_id AS lease_worker_id, l.lease_until
        FROM runs r
        LEFT JOIN run_leases l ON l.run_id = r.id
        WHERE r.case_id = ? AND r.status = 'running'
        ORDER BY r.created_at DESC, r.id DESC LIMIT 1
        """,
        (case_id,),
    ).fetchone()
    if not row:
        return None, False

    now = _parse(now_iso) or _utcnow()
    deadline = _parse(row["lease_until"])
    if deadline is not None and deadline > now:
        return row, False

    old_deadline = row["lease_until"]
    if old_deadline is None:
        return row, False

    changed = conn.execute(
        """
        UPDATE runs SET status = 'failed', updated_at = ?
        WHERE id = ? AND status = 'running'
          AND EXISTS (
              SELECT 1 FROM run_leases l
              WHERE l.run_id = runs.id AND l.lease_until = ?
          )
        """,
        (now_iso, row["id"], old_deadline),
    ).rowcount
    if changed != 1:
        current = conn.execute(
            "SELECT * FROM runs WHERE id = ? AND status = 'running'", (row["id"],)
        ).fetchone()
        return current, False

    conn.execute(
        "DELETE FROM run_leases WHERE run_id = ? AND lease_until = ?",
        (row["id"], old_deadline),
    )
    conn.execute(
        "INSERT INTO events VALUES (?, 'system', 'run_lease_expired', ?, ?, ?, ?)",
        (
            uuid.uuid4().hex[:12],
            case_id,
            row["id"],
            json.dumps(
                {
                    "worker_id": row["lease_worker_id"],
                    "lease_until": old_deadline,
                    "reason": "worker_lease_expired",
                },
                ensure_ascii=False,
            ),
            now_iso,
        ),
    )
    return None, True


def reclaim_expired_runs(conn: sqlite3.Connection, now_iso: str) -> int:
    """Sweep crashed owners globally so stale cases do not consume capacity."""
    now = _parse(now_iso) or _utcnow()
    missing_cutoff = now - timedelta(seconds=legacy_grace_seconds())
    rows = conn.execute(
        """
        SELECT DISTINCT r.case_id, r.created_at, l.lease_until
        FROM runs r LEFT JOIN run_leases l ON l.run_id = r.id
        WHERE r.status = 'running'
        """
    ).fetchall()
    candidates: set[str] = set()
    for row in rows:
        case_id = str(row["case_id"])
        deadline = _parse(row["lease_until"])
        if row["lease_until"] is not None:
            if deadline is None or deadline <= now:
                candidates.add(case_id)
            continue
        created = _parse(row["created_at"])
        if created is not None and created <= missing_cutoff:
            conn.execute(
                "INSERT OR IGNORE INTO run_leases(run_id, worker_id, lease_until) "
                "SELECT id, 'orphaned-worker', ? FROM runs "
                "WHERE case_id = ? AND status = 'running' AND id NOT IN (SELECT run_id FROM run_leases)",
                (now_iso, case_id),
            )
            candidates.add(case_id)

    reclaimed = 0
    for case_id in candidates:
        _, changed = active_or_reclaim(conn, case_id, now_iso)
        reclaimed += int(changed)
    return reclaimed


def prepare_case_claim(db_path: str, case_id: str) -> bool:
    """Reclaim only this case before a serialized evidence mutation.

    Ordinary target/asset/archive writes should not scan every running task in the
    workspace. Global stale-owner cleanup is reserved for `prepare_run_start`,
    because only a new run needs to free capacity consumed by other cases.
    """
    if not db_path or db_path == ":memory:":
        return False
    _ensure_path_schema(db_path)
    conn = _connect(db_path)
    try:
        _, changed = active_or_reclaim(conn, case_id, _iso(_utcnow()))
        conn.commit()
        return bool(changed)
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def prepare_run_start(db_path: str) -> int:
    """Free globally expired owners immediately before creating a new run."""
    if not db_path or db_path == ":memory:":
        return 0
    _ensure_path_schema(db_path)
    conn = _connect(db_path)
    try:
        reclaimed = reclaim_expired_runs(conn, _iso(_utcnow()))
        conn.commit()
        return reclaimed
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@dataclass
class _Registration:
    db_path: str
    run_id: str
    seconds: int
    next_renew_at: float


class _HeartbeatRegistry:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._items: dict[tuple[str, str], _Registration] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pid = os.getpid()

    def _ensure_process(self) -> None:
        pid = os.getpid()
        if self._pid == pid:
            return
        self._pid = pid
        self._items = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    @staticmethod
    def _normal_interval(seconds: int) -> float:
        return max(1.0, min(30.0, seconds / 3.0))

    @staticmethod
    def _retry_interval(seconds: int) -> float:
        return max(0.25, min(2.0, seconds / 10.0))

    def register(self, db_path: str, run_id: str, seconds: int) -> None:
        with self._guard:
            self._ensure_process()
            self._items[(_db_key(db_path), run_id)] = _Registration(
                db_path=db_path,
                run_id=run_id,
                seconds=seconds,
                next_renew_at=time.monotonic() + self._normal_interval(seconds),
            )
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._run,
                    name="guanchao-run-lease",
                    daemon=True,
                )
                self._thread.start()
            self._wake.set()

    def discard(self, db_path: str, run_id: str) -> None:
        with self._guard:
            self._ensure_process()
            self._items.pop((_db_key(db_path), run_id), None)

    def _run(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            with self._guard:
                self._ensure_process()
                due = [item for item in self._items.values() if item.next_renew_at <= now]
            for item in due:
                try:
                    self._renew(item)
                except Exception:
                    self._reschedule(item, retry=True)
            self._wake.wait(0.25)
            self._wake.clear()

    def _reschedule(self, item: _Registration, *, retry: bool) -> None:
        with self._guard:
            key = (_db_key(item.db_path), item.run_id)
            current = self._items.get(key)
            if current is not item:
                return
            interval = (
                self._retry_interval(item.seconds)
                if retry
                else self._normal_interval(item.seconds)
            )
            current.next_renew_at = time.monotonic() + interval

    def _renew(self, item: _Registration) -> None:
        owner = worker_id()
        keep = False
        retry = False
        conn: sqlite3.Connection | None = None
        try:
            conn = _connect(item.db_path)
            row = conn.execute(
                """
                SELECT r.status, l.worker_id
                FROM runs r LEFT JOIN run_leases l ON l.run_id = r.id
                WHERE r.id = ?
                """,
                (item.run_id,),
            ).fetchone()
            if row and row["status"] == "running" and row["worker_id"] == owner:
                assert_and_renew(conn, item.run_id, owner, item.seconds)
                conn.commit()
                keep = True
            elif row and row["status"] != "running" and row["worker_id"] == owner:
                conn.execute(
                    "DELETE FROM run_leases WHERE run_id = ? AND worker_id = ?",
                    (item.run_id, owner),
                )
                conn.commit()
        except RunLeaseLostError:
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
        except sqlite3.Error:
            retry = True
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
        finally:
            if conn is not None:
                conn.close()

        with self._guard:
            key = (_db_key(item.db_path), item.run_id)
            current = self._items.get(key)
            if current is not item:
                return
            if keep:
                current.next_renew_at = time.monotonic() + self._normal_interval(item.seconds)
            elif retry:
                current.next_renew_at = time.monotonic() + self._retry_interval(item.seconds)
            else:
                self._items.pop(key, None)

    def shutdown(self) -> None:
        with self._guard:
            self._ensure_process()
            thread = self._thread
            self._stop.set()
            self._wake.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        with self._guard:
            self._items.clear()
            self._thread = None


_HEARTBEATS = _HeartbeatRegistry()


def observe_running_case(db_path: str, case_id: str) -> str | None:
    """Adopt a newly-created unleased run and keep its lease alive."""
    if not db_path or db_path == ":memory:":
        return None
    _ensure_path_schema(db_path)
    owner = worker_id()
    seconds = lease_seconds()
    conn = _connect(db_path)
    run_id: str | None = None
    try:
        row = conn.execute(
            """
            SELECT r.id, r.status, l.worker_id, l.lease_until
            FROM runs r LEFT JOIN run_leases l ON l.run_id = r.id
            WHERE r.case_id = ? AND r.status = 'running'
            ORDER BY r.created_at DESC, r.id DESC LIMIT 1
            """,
            (case_id,),
        ).fetchone()
        if not row:
            conn.commit()
            return None

        run_id = str(row["id"])
        lease_owner = row["worker_id"]
        if lease_owner is None:
            insert_lease(conn, run_id, owner, seconds)
        elif lease_owner == owner:
            assert_and_renew(conn, run_id, owner, seconds)
        else:
            deadline = _parse(row["lease_until"])
            if deadline is None or deadline <= _utcnow():
                conn.commit()
                return None
            conn.commit()
            return None
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    if run_id:
        _HEARTBEATS.register(db_path, run_id, seconds)
    return run_id


def shutdown_heartbeats() -> None:
    _HEARTBEATS.shutdown()
