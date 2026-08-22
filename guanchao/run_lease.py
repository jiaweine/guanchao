from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any


class RunLeaseLostError(RuntimeError):
    """The worker no longer owns the durable running row."""


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


def lease_deadline(seconds: int) -> str:
    seconds = max(5, min(7200, int(seconds)))
    return _iso(_utcnow() + timedelta(seconds=seconds))


def ensure_schema(conn: sqlite3.Connection, legacy_grace_seconds: int = 300) -> None:
    """Install run ownership state without changing the historical runs schema."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS run_leases (
            run_id TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            lease_until TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_run_leases_until ON run_leases(lease_until);
        """
    )
    deadline = lease_deadline(legacy_grace_seconds)
    # A rolling upgrade may encounter a running row created by an older worker
    # that knows nothing about leases. Give it a grace period instead of killing
    # it immediately; if the old worker remains deployed it should be drained or
    # upgraded before the grace window ends.
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


def insert_lease(
    conn: sqlite3.Connection,
    run_id: str,
    worker_id: str,
    lease_seconds: int,
) -> None:
    conn.execute(
        "INSERT INTO run_leases(run_id, worker_id, lease_until) VALUES (?, ?, ?)",
        (run_id, worker_id, lease_deadline(lease_seconds)),
    )


def assert_and_renew(
    conn: sqlite3.Connection,
    run_id: str,
    worker_id: str,
    lease_seconds: int,
) -> None:
    row = conn.execute(
        """
        SELECT r.status, l.worker_id
        FROM runs r LEFT JOIN run_leases l ON l.run_id = r.id
        WHERE r.id = ?
        """,
        (run_id,),
    ).fetchone()
    if not row or row["status"] != "running" or row["worker_id"] != worker_id:
        raise RunLeaseLostError(run_id)
    changed = conn.execute(
        "UPDATE run_leases SET lease_until = ? WHERE run_id = ? AND worker_id = ?",
        (lease_deadline(lease_seconds), run_id, worker_id),
    ).rowcount
    if changed != 1:
        raise RunLeaseLostError(run_id)


def release_lease(conn: sqlite3.Connection, run_id: str, worker_id: str | None = None) -> None:
    if worker_id is None:
        conn.execute("DELETE FROM run_leases WHERE run_id = ?", (run_id,))
        return
    changed = conn.execute(
        "DELETE FROM run_leases WHERE run_id = ? AND worker_id = ?",
        (run_id, worker_id),
    ).rowcount
    if changed != 1:
        raise RunLeaseLostError(run_id)


def active_or_reclaim(
    conn: sqlite3.Connection,
    case_id: str,
    now_iso: str,
) -> tuple[sqlite3.Row | None, bool]:
    """Return a live running row or atomically fail an expired owner.

    `lease_until` is compared in Python to tolerate legacy formatting, then the
    exact value is repeated in the UPDATE predicate. A concurrent heartbeat that
    renews the lease therefore wins cleanly and cannot be expired using stale
    information.
    """
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
        # Runtime corruption/mixed-version row with no ownership state: install a
        # short legacy grace lease rather than guessing that the worker is dead.
        grace = lease_deadline(30)
        conn.execute(
            "INSERT OR IGNORE INTO run_leases(run_id, worker_id, lease_until) VALUES (?, 'legacy-worker', ?)",
            (row["id"], grace),
        )
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
