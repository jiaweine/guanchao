from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from .detection import Calibration
from .domain import AssetSnapshot, FeatureVector, utcnow_iso
from .evolution import LabeledExample
from .policy import PolicyProfile

_REVIEW_DECISIONS = {"confirm_ordinary", "uncertain", "confirm_marketing"}
_MEMBER_ROLES = {"admin", "analyst", "reviewer"}
_CASE_PRIORITIES = {"low", "normal", "high"}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("GUANCHAO_DB", "guanchao.db")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.asset_dir = Path(os.getenv("GUANCHAO_ASSET_DIR", ".guanchao-assets"))
        if self.path != ":memory:":
            self.asset_dir.mkdir(parents=True, exist_ok=True)
        self._memory_conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._metrics_lock = threading.Lock()
        self._metrics_cache: dict[str, Any] | None = None
        self._metrics_cache_at = 0.0
        if self.path == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row
            self._memory_conn.execute("PRAGMA foreign_keys=ON")
        self._init()

    def _connect(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        conn = sqlite3.connect(self.path, timeout=15, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _close(self, conn: sqlite3.Connection) -> None:
        if self._memory_conn is None:
            conn.close()

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, name: str, definition: str) -> None:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _init(self) -> None:
        with self._lock:
            conn = self._connect()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    owner TEXT NOT NULL DEFAULT 'local',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    monitoring_enabled INTEGER NOT NULL DEFAULT 0,
                    monitoring_interval_hours INTEGER NOT NULL DEFAULT 168,
                    next_check_at TEXT,
                    last_source_refresh_at TEXT,
                    batch_id TEXT
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS comments (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    author TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE,
                    decision TEXT NOT NULL CHECK(decision IN ('confirm_ordinary','uncertain','confirm_marketing')),
                    reason TEXT NOT NULL,
                    note TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    features_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE,
                    FOREIGN KEY(run_id) REFERENCES runs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    storage_path TEXT NOT NULL,
                    extracted_text TEXT NOT NULL,
                    note TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(case_id) REFERENCES cases(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    item_count INTEGER NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS members (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','analyst','reviewer')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    case_id TEXT,
                    run_id TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            for name, definition in (
                ("status", "TEXT NOT NULL DEFAULT 'open'"),
                ("priority", "TEXT NOT NULL DEFAULT 'normal'"),
                ("owner", "TEXT NOT NULL DEFAULT 'local'"),
                ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("monitoring_enabled", "INTEGER NOT NULL DEFAULT 0"),
                ("monitoring_interval_hours", "INTEGER NOT NULL DEFAULT 168"),
                ("next_check_at", "TEXT"),
                ("last_source_refresh_at", "TEXT"),
                ("batch_id", "TEXT"),
            ):
                self._ensure_column(conn, "cases", name, definition)
            conn.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_case_created ON messages(case_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_comments_case_created ON comments(case_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_runs_case_created ON runs(case_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_assets_case_created ON assets(case_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_reviews_case_created ON reviews(case_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_reviews_decision ON reviews(decision, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cases_status_updated ON cases(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cases_owner_updated ON cases(owner, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_cases_monitoring ON cases(monitoring_enabled, next_check_at);
                CREATE INDEX IF NOT EXISTS idx_events_case_created ON events(case_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_events_type_created ON events(event_type, created_at DESC);
                """
            )
            now = utcnow_iso()
            conn.execute(
                """
                INSERT INTO members(id, display_name, role, active, created_at, updated_at)
                VALUES('local', '本机管理员', 'admin', 1, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (now, now),
            )
            conn.commit()
            self._close(conn)

    def record_event(
        self,
        event_type: str,
        actor: str = "local",
        case_id: str | None = None,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex[:12],
            "actor": actor or "local",
            "event_type": event_type,
            "case_id": case_id,
            "run_id": run_id,
            "metadata_json": json.dumps(metadata or {}, ensure_ascii=False),
            "created_at": utcnow_iso(),
        }
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(item.values()),
            )
            conn.commit()
            self._close(conn)
        self._invalidate_metrics()
        return {**item, "metadata": metadata or {}}

    def _invalidate_metrics(self) -> None:
        self._metrics_cache = None
        self._metrics_cache_at = 0.0

    def audit_events(self, case_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._lock:
            conn = self._connect()
            if case_id:
                rows = conn.execute(
                    "SELECT * FROM events WHERE case_id = ? ORDER BY created_at DESC LIMIT ?",
                    (case_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            self._close(conn)
        return [self._event_row(row) for row in rows]

    def list_members(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM members WHERE active = 1 ORDER BY display_name, id"
            ).fetchall()
            self._close(conn)
        return [dict(row) for row in rows]

    def get_member(self, member_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM members WHERE id = ? AND active = 1", (member_id,)
            ).fetchone()
            self._close(conn)
        if not row:
            raise KeyError(member_id)
        return dict(row)

    def save_member(self, member_id: str, display_name: str, role: str, actor: str = "local") -> dict[str, Any]:
        member_id = member_id.strip().lower()
        if not member_id or len(member_id) > 64:
            raise ValueError("invalid member id")
        if role not in _MEMBER_ROLES:
            raise ValueError("invalid member role")
        if member_id == "local" and role != "admin":
            raise ValueError("local admin must remain an admin")
        now = utcnow_iso()
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO members(id, display_name, role, active, created_at, updated_at)
                VALUES(?, ?, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    role = excluded.role,
                    active = 1,
                    updated_at = excluded.updated_at
                """,
                (member_id, display_name.strip() or member_id, role, now, now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
            self._close(conn)
        self.record_event("member_saved", actor=actor, metadata={"member": member_id, "role": role})
        assert row is not None
        return dict(row)

    def deactivate_member(self, member_id: str, actor: str = "local") -> None:
        if member_id == "local":
            raise ValueError("local admin cannot be deactivated")
        with self._lock:
            conn = self._connect()
            exists = conn.execute("SELECT id FROM members WHERE id = ? AND active = 1", (member_id,)).fetchone()
            if not exists:
                self._close(conn)
                raise KeyError(member_id)
            reassigned = conn.execute(
                "UPDATE cases SET owner = 'local', updated_at = ? WHERE owner = ? AND status = 'open'",
                (utcnow_iso(), member_id),
            ).rowcount
            conn.execute(
                "UPDATE members SET active = 0, updated_at = ? WHERE id = ?",
                (utcnow_iso(), member_id),
            )
            conn.commit()
            self._close(conn)
        self.record_event(
            "member_deactivated", actor=actor, metadata={"member": member_id, "reassigned_open_cases": reassigned}
        )

    def _ensure_case_owner(self, member_id: str) -> dict[str, Any]:
        member = self.get_member(member_id)
        if member["role"] not in {"admin", "analyst"}:
            raise ValueError("case owner must be an analyst or admin")
        return member

    def create_case(
        self,
        title: str,
        goal: str,
        targets: list[dict[str, Any]],
        owner: str = "local",
        priority: str = "normal",
        tags: list[str] | None = None,
        batch_id: str | None = None,
        actor: str = "local",
    ) -> dict[str, Any]:
        if priority not in _CASE_PRIORITIES:
            raise ValueError("invalid priority")
        self._ensure_case_owner(owner)
        now = utcnow_iso()
        case_id = uuid.uuid4().hex[:12]
        clean_tags = self._clean_tags(tags or [])
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO cases(
                    id, title, goal, targets_json, created_at, updated_at, status, priority, owner,
                    tags_json, monitoring_enabled, monitoring_interval_hours, next_check_at,
                    last_source_refresh_at, batch_id
                ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 0, 168, NULL, NULL, ?)
                """,
                (
                    case_id,
                    title,
                    goal,
                    json.dumps(targets, ensure_ascii=False),
                    now,
                    now,
                    priority,
                    owner,
                    json.dumps(clean_tags, ensure_ascii=False),
                    batch_id,
                ),
            )
            conn.commit()
            self._close(conn)
        self.record_event(
            "case_created",
            actor=actor,
            case_id=case_id,
            metadata={"owner": owner, "priority": priority, "batch_id": batch_id},
        )
        return self.get_case(case_id)

    def create_batch(
        self,
        title: str,
        goal: str,
        targets: list[dict[str, Any]],
        owner: str = "local",
        priority: str = "normal",
        tags: list[str] | None = None,
        actor: str = "local",
    ) -> dict[str, Any]:
        if not targets:
            raise ValueError("batch is empty")
        if len(targets) > 200:
            raise ValueError("batch is too large")
        self._ensure_case_owner(owner)
        if priority not in _CASE_PRIORITIES:
            raise ValueError("invalid priority")
        batch_id = uuid.uuid4().hex[:12]
        now = utcnow_iso()
        clean_tags = self._clean_tags(tags or [])
        case_ids: list[str] = []
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO batches VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, title, goal, owner, priority, len(targets), actor, now),
            )
            for target in targets:
                case_id = uuid.uuid4().hex[:12]
                case_ids.append(case_id)
                name = str(target.get("display_name") or target.get("handle") or "账号").strip()
                conn.execute(
                    """
                    INSERT INTO cases(
                        id, title, goal, targets_json, created_at, updated_at, status, priority, owner,
                        tags_json, monitoring_enabled, monitoring_interval_hours, next_check_at,
                        last_source_refresh_at, batch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, 0, 168, NULL, NULL, ?)
                    """,
                    (
                        case_id,
                        f"{name} · 内容调查",
                        goal,
                        json.dumps([target], ensure_ascii=False),
                        now,
                        now,
                        priority,
                        owner,
                        json.dumps(clean_tags, ensure_ascii=False),
                        batch_id,
                    ),
                )
                conn.execute(
                    "INSERT INTO events VALUES (?, ?, 'case_created', ?, NULL, ?, ?)",
                    (
                        uuid.uuid4().hex[:12],
                        actor,
                        case_id,
                        json.dumps({"owner": owner, "priority": priority, "batch_id": batch_id}, ensure_ascii=False),
                        now,
                    ),
                )
            conn.execute(
                "INSERT INTO events VALUES (?, ?, 'batch_created', NULL, NULL, ?, ?)",
                (
                    uuid.uuid4().hex[:12],
                    actor,
                    json.dumps({"batch_id": batch_id, "count": len(case_ids), "title": title}, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
            self._close(conn)
        self._invalidate_metrics()
        cases = self.list_cases(status="open", batch_id=batch_id)
        return {
            "id": batch_id,
            "title": title,
            "goal": goal,
            "owner": owner,
            "priority": priority,
            "count": len(case_ids),
            "created_at": now,
            "cases": cases,
        }

    def list_cases(
        self,
        query: str = "",
        platform: str = "",
        status: str = "open",
        owner: str = "",
        priority: str = "",
        sort: str = "updated_desc",
        batch_id: str = "",
    ) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT c.*,
                       r.id AS latest_run_id, r.status AS latest_run_status,
                       r.state_json AS latest_state_json, r.updated_at AS latest_run_updated_at,
                       rv.decision AS latest_review_decision
                FROM cases c
                LEFT JOIN runs r ON r.id = (
                    SELECT r2.id FROM runs r2 WHERE r2.case_id = c.id
                    ORDER BY r2.created_at DESC LIMIT 1
                )
                LEFT JOIN reviews rv ON rv.run_id = r.id
                """
            ).fetchall()
            self._close(conn)
        items = [self._case_summary(row) for row in rows]
        normalized = query.strip().lower()
        if normalized:
            items = [
                item for item in items
                if normalized in item["title"].lower()
                or normalized in item["goal"].lower()
                or any(
                    normalized in str(target.get("handle") or "").lower()
                    or normalized in str(target.get("display_name") or "").lower()
                    for target in item["targets"]
                )
            ]
        if platform:
            items = [item for item in items if any(t.get("platform") == platform for t in item["targets"])]
        if status and status != "all":
            items = [item for item in items if item["status"] == status]
        if owner:
            items = [item for item in items if item["owner"] == owner]
        if priority:
            items = [item for item in items if item["priority"] == priority]
        if batch_id:
            items = [item for item in items if item["batch_id"] == batch_id]
        if sort == "updated_asc":
            items.sort(key=lambda item: item["updated_at"])
        elif sort == "created_desc":
            items.sort(key=lambda item: item["created_at"], reverse=True)
        elif sort == "risk_desc":
            items.sort(key=lambda item: item.get("review_priority") or -1, reverse=True)
        else:
            items.sort(key=lambda item: item["updated_at"], reverse=True)
        return items

    def get_case(self, case_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
            if not row:
                self._close(conn)
                raise KeyError(case_id)
            messages = conn.execute(
                "SELECT * FROM messages WHERE case_id = ? ORDER BY created_at", (case_id,)
            ).fetchall()
            comments = conn.execute(
                "SELECT * FROM comments WHERE case_id = ? ORDER BY created_at DESC", (case_id,)
            ).fetchall()
            runs = conn.execute(
                "SELECT * FROM runs WHERE case_id = ? ORDER BY created_at DESC", (case_id,)
            ).fetchall()
            assets = conn.execute(
                "SELECT * FROM assets WHERE case_id = ? ORDER BY created_at", (case_id,)
            ).fetchall()
            reviews = conn.execute(
                "SELECT * FROM reviews WHERE case_id = ? ORDER BY created_at DESC", (case_id,)
            ).fetchall()
            self._close(conn)
        payload = self._case_row(row)
        payload["messages"] = [dict(item) for item in messages]
        payload["comments"] = [dict(item) for item in comments]
        payload["runs"] = [self._run_row(item) for item in runs]
        payload["assets"] = [self._asset_row(item, include_text=False) for item in assets]
        payload["reviews"] = [self._review_row(item) for item in reviews]
        payload["monitoring_due"] = self._monitoring_due(payload)
        latest = payload["runs"][0] if payload["runs"] else None
        payload["review_priority"] = self._review_priority(
            (latest or {}).get("state", {}).get("primary_result", {}), payload["priority"]
        ) if latest and latest["status"] == "completed" else None
        return payload

    def update_case(
        self,
        case_id: str,
        *,
        owner: str | None = None,
        priority: str | None = None,
        tags: list[str] | None = None,
        archived: bool | None = None,
        monitoring_enabled: bool | None = None,
        monitoring_interval_hours: int | None = None,
        actor: str = "local",
    ) -> dict[str, Any]:
        current = self.get_case(case_id)
        changes: dict[str, Any] = {}
        new_owner = current["owner"] if owner is None else owner
        new_priority = current["priority"] if priority is None else priority
        new_tags = current["tags"] if tags is None else self._clean_tags(tags)
        new_status = current["status"] if archived is None else ("archived" if archived else "open")
        monitor = current["monitoring_enabled"] if monitoring_enabled is None else bool(monitoring_enabled)
        interval = current["monitoring_interval_hours"] if monitoring_interval_hours is None else int(monitoring_interval_hours)
        if new_priority not in _CASE_PRIORITIES:
            raise ValueError("invalid priority")
        self._ensure_case_owner(new_owner)
        if not 1 <= interval <= 24 * 365:
            raise ValueError("invalid monitoring interval")
        next_check = current.get("next_check_at")
        if monitor and (not current["monitoring_enabled"] or monitoring_interval_hours is not None):
            next_check = (datetime.now(timezone.utc) + timedelta(hours=interval)).isoformat()
        if not monitor:
            next_check = None
        for key, before, after in (
            ("owner", current["owner"], new_owner),
            ("priority", current["priority"], new_priority),
            ("tags", current["tags"], new_tags),
            ("status", current["status"], new_status),
            ("monitoring_enabled", current["monitoring_enabled"], monitor),
            ("monitoring_interval_hours", current["monitoring_interval_hours"], interval),
        ):
            if before != after:
                changes[key] = {"from": before, "to": after}
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                UPDATE cases SET owner = ?, priority = ?, tags_json = ?, status = ?,
                    monitoring_enabled = ?, monitoring_interval_hours = ?, next_check_at = ?,
                    updated_at = ? WHERE id = ?
                """,
                (
                    new_owner,
                    new_priority,
                    json.dumps(new_tags, ensure_ascii=False),
                    new_status,
                    int(monitor),
                    interval,
                    next_check,
                    utcnow_iso(),
                    case_id,
                ),
            )
            conn.commit()
            self._close(conn)
        if changes:
            self.record_event("case_updated", actor=actor, case_id=case_id, metadata=changes)
        return self.get_case(case_id)

    def update_target(self, case_id: str, target: dict[str, Any], actor: str = "local") -> dict[str, Any]:
        current = self.get_case(case_id)
        targets = current["targets"][:]
        if targets:
            targets[0] = target
        else:
            targets = [target]
        now = datetime.now(timezone.utc)
        next_check = None
        if current["monitoring_enabled"]:
            next_check = (now + timedelta(hours=current["monitoring_interval_hours"])).isoformat()
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                UPDATE cases SET targets_json = ?, last_source_refresh_at = ?, next_check_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(targets, ensure_ascii=False), now.isoformat(), next_check, now.isoformat(), case_id),
            )
            conn.commit()
            self._close(conn)
        self.record_event("source_refreshed", actor=actor, case_id=case_id, metadata={"target": target.get("handle")})
        return self.get_case(case_id)

    def delete_case(self, case_id: str, actor: str = "local") -> None:
        case = self.get_case(case_id)
        with self._lock:
            conn = self._connect()
            paths = [
                item["storage_path"] for item in conn.execute(
                    "SELECT storage_path FROM assets WHERE case_id = ?", (case_id,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
            conn.commit()
            self._close(conn)
        for path in paths:
            self._remove_asset_file(path)
        self.record_event(
            "case_deleted",
            actor=actor,
            case_id=case_id,
            metadata={"title": case["title"], "asset_count": len(paths)},
        )

    def add_message(self, case_id: str, role: str, content: str) -> dict[str, Any]:
        now = utcnow_iso()
        item = {
            "id": uuid.uuid4().hex[:12],
            "case_id": case_id,
            "role": role,
            "content": content,
            "created_at": now,
        }
        with self._lock:
            conn = self._connect()
            conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", tuple(item.values()))
            conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (now, case_id))
            conn.commit()
            self._close(conn)
        return item

    def add_comment(self, case_id: str, author: str, content: str) -> dict[str, Any]:
        member = self.get_member(author)
        if not member.get("active"):
            raise KeyError(author)
        self.get_case(case_id)
        text = content.strip()
        if not text:
            raise ValueError("comment cannot be empty")
        item = {
            "id": uuid.uuid4().hex[:12],
            "case_id": case_id,
            "author": author,
            "content": text,
            "created_at": utcnow_iso(),
        }
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO comments(id, case_id, author, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (item["id"], case_id, author, text, item["created_at"]),
            )
            conn.commit()
            self._close(conn)
        self.record_event(
            "comment_added",
            actor=author,
            case_id=case_id,
            metadata={"comment_id": item["id"]},
        )
        return item

    def create_run(self, case_id: str, state: dict[str, Any], actor: str = "local") -> dict[str, Any]:
        now = utcnow_iso()
        run_id = uuid.uuid4().hex[:12]
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, case_id, "running", json.dumps(state, ensure_ascii=False), now, now),
            )
            conn.commit()
            self._close(conn)
        self.record_event("run_started", actor=actor, case_id=case_id, run_id=run_id)
        return self.get_run(run_id)

    def update_run(self, run_id: str, state: dict[str, Any], status: str) -> None:
        with self._lock:
            conn = self._connect()
            previous = conn.execute("SELECT case_id, status FROM runs WHERE id = ?", (run_id,)).fetchone()
            conn.execute(
                "UPDATE runs SET status = ?, state_json = ?, updated_at = ? WHERE id = ?",
                (status, json.dumps(state, ensure_ascii=False), utcnow_iso(), run_id),
            )
            conn.commit()
            self._close(conn)
        if previous and status != previous["status"] and status in {"completed", "failed"}:
            self.record_event(
                f"run_{status}",
                case_id=previous["case_id"],
                run_id=run_id,
                metadata={"has_result": bool(state.get("primary_result"))},
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            self._close(conn)
        if not row:
            raise KeyError(run_id)
        return self._run_row(row)

    def active_run_for_case(self, case_id: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT * FROM runs WHERE case_id = ? AND status = 'running' ORDER BY created_at DESC LIMIT 1",
                (case_id,),
            ).fetchone()
            self._close(conn)
        return self._run_row(row) if row else None

    def create_asset(
        self,
        case_id: str,
        name: str,
        kind: str,
        content_type: str,
        size: int,
        storage_path: str,
        actor: str = "local",
    ) -> AssetSnapshot:
        asset = AssetSnapshot(
            uuid.uuid4().hex[:12], case_id, name, kind, content_type, max(0, int(size))
        )  # type: ignore[arg-type]
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    asset.id,
                    case_id,
                    name,
                    kind,
                    content_type,
                    asset.size,
                    asset.status,
                    storage_path,
                    "",
                    "",
                    "",
                    asset.created_at,
                ),
            )
            conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (utcnow_iso(), case_id))
            conn.commit()
            self._close(conn)
        self.record_event(
            "asset_added", actor=actor, case_id=case_id, metadata={"asset_id": asset.id, "kind": kind, "name": name}
        )
        return asset

    def update_asset(
        self,
        asset_id: str,
        status: str,
        extracted_text: str = "",
        error: str = "",
        note: str = "",
    ) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE assets SET status = ?, extracted_text = ?, error = ?, note = ? WHERE id = ?",
                (status, extracted_text, error, note, asset_id),
            )
            conn.commit()
            self._close(conn)

    def get_asset(self, asset_id: str, include_text: bool = False) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
            self._close(conn)
        if not row:
            raise KeyError(asset_id)
        return self._asset_row(row, include_text)

    def list_assets(self, case_id: str, include_text: bool = True) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM assets WHERE case_id = ? ORDER BY created_at", (case_id,)
            ).fetchall()
            self._close(conn)
        return [self._asset_row(row, include_text) for row in rows]

    def delete_asset(self, case_id: str, asset_id: str, actor: str = "local") -> None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT storage_path, name FROM assets WHERE id = ? AND case_id = ?", (asset_id, case_id)
            ).fetchone()
            if not row:
                self._close(conn)
                raise KeyError(asset_id)
            conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
            conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (utcnow_iso(), case_id))
            conn.commit()
            self._close(conn)
        self._remove_asset_file(row["storage_path"])
        self.record_event(
            "asset_deleted", actor=actor, case_id=case_id, metadata={"asset_id": asset_id, "name": row["name"]}
        )

    def add_review(
        self,
        case_id: str,
        run_id: str,
        decision: str,
        reason: str = "",
        note: str = "",
        reviewer: str = "local",
    ) -> dict[str, Any]:
        if decision not in _REVIEW_DECISIONS:
            raise ValueError("invalid review decision")
        self.get_member(reviewer)
        run = self.get_run(run_id)
        if run["case_id"] != case_id:
            raise ValueError("run does not belong to case")
        if run["status"] != "completed":
            raise RuntimeError("run is not completed")
        features = (run.get("state") or {}).get("primary_result", {}).get("features")
        if not features:
            raise RuntimeError("run has no reviewable feature snapshot")

        now = utcnow_iso()
        review_id = uuid.uuid4().hex[:12]
        with self._lock:
            conn = self._connect()
            existing = conn.execute("SELECT id FROM reviews WHERE run_id = ?", (run_id,)).fetchone()
            if existing:
                review_id = existing["id"]
                conn.execute(
                    """
                    UPDATE reviews SET decision = ?, reason = ?, note = ?, reviewer = ?,
                        features_json = ?, updated_at = ? WHERE run_id = ?
                    """,
                    (
                        decision,
                        reason.strip(),
                        note.strip(),
                        reviewer,
                        json.dumps(features, ensure_ascii=False),
                        now,
                        run_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO reviews VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        review_id,
                        case_id,
                        run_id,
                        decision,
                        reason.strip(),
                        note.strip(),
                        reviewer,
                        json.dumps(features, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (now, case_id))
            conn.commit()
            row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
            self._close(conn)
        self.record_event(
            "review_submitted",
            actor=reviewer,
            case_id=case_id,
            run_id=run_id,
            metadata={"decision": decision},
        )
        assert row is not None
        return self._review_row(row)

    def review_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute("SELECT * FROM reviews ORDER BY created_at").fetchall()
            self._close(conn)
        return [self._review_row(row) for row in rows]

    def review_queue(
        self,
        reviewed: bool | None = False,
        query: str = "",
        platform: str = "",
        owner: str = "",
        priority: str = "",
        sort: str = "priority_desc",
    ) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT c.*,
                       r.id AS run_id, r.state_json, r.updated_at AS run_updated_at,
                       rv.id AS review_id, rv.decision, rv.reason, rv.note, rv.reviewer,
                       rv.created_at AS review_created_at, rv.updated_at AS review_updated_at
                FROM cases c
                JOIN runs r ON r.id = (
                    SELECT r2.id FROM runs r2 WHERE r2.case_id = c.id
                    ORDER BY r2.created_at DESC LIMIT 1
                )
                LEFT JOIN reviews rv ON rv.run_id = r.id
                WHERE c.status = 'open' AND r.status = 'completed'
                ORDER BY r.updated_at DESC
                """
            ).fetchall()
            self._close(conn)

        items: list[dict[str, Any]] = []
        normalized = query.strip().lower()
        for row in rows:
            has_review = bool(row["review_id"])
            if reviewed is True and not has_review:
                continue
            if reviewed is False and has_review:
                continue
            state = json.loads(row["state_json"])
            result = state.get("primary_result") or {}
            if not result:
                continue
            targets = json.loads(row["targets_json"])
            if normalized:
                haystack = " ".join(
                    [row["title"], row["goal"]]
                    + [str(t.get("handle") or "") for t in targets]
                    + [str(t.get("display_name") or "") for t in targets]
                ).lower()
                if normalized not in haystack:
                    continue
            if platform and not any(t.get("platform") == platform for t in targets):
                continue
            if owner and row["owner"] != owner:
                continue
            if priority and row["priority"] != priority:
                continue
            review = None
            if has_review:
                review = {
                    "id": row["review_id"],
                    "case_id": row["id"],
                    "run_id": row["run_id"],
                    "decision": row["decision"],
                    "reason": row["reason"],
                    "note": row["note"],
                    "reviewer": row["reviewer"],
                    "created_at": row["review_created_at"],
                    "updated_at": row["review_updated_at"],
                }
            items.append(
                {
                    "case_id": row["id"],
                    "title": row["title"],
                    "goal": row["goal"],
                    "targets": targets,
                    "updated_at": row["updated_at"],
                    "run_id": row["run_id"],
                    "run_updated_at": row["run_updated_at"],
                    "result": result,
                    "review": review,
                    "owner": row["owner"],
                    "priority": row["priority"],
                    "tags": json.loads(row["tags_json"] or "[]"),
                    "batch_id": row["batch_id"],
                    "review_priority": self._review_priority(result, row["priority"]),
                }
            )
        if sort == "risk_desc":
            items.sort(key=lambda item: float(item["result"].get("marketing_likelihood") or 0), reverse=True)
        elif sort == "newest":
            items.sort(key=lambda item: item["run_updated_at"], reverse=True)
        else:
            items.sort(key=lambda item: item["review_priority"], reverse=True)
        return items

    def monitoring_queue(self, due_only: bool = True) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        items = []
        for case in self.list_cases(status="open"):
            if not case["monitoring_enabled"]:
                continue
            next_check = _parse_time(case.get("next_check_at"))
            due = next_check is None or next_check <= now
            if due_only and not due:
                continue
            items.append({**case, "monitoring_due": due})
        items.sort(key=lambda item: item.get("next_check_at") or "")
        return items

    def workspace_settings(self) -> dict[str, Any]:
        raw = self._setting("workspace") or {}
        return {
            "retention_days": max(0, int(raw.get("retention_days") or 0)),
        }

    def save_workspace_settings(self, retention_days: int, actor: str = "local") -> dict[str, Any]:
        retention_days = int(retention_days)
        if not 0 <= retention_days <= 3650:
            raise ValueError("invalid retention days")
        settings = {"retention_days": retention_days}
        self._save_setting("workspace", settings)
        self.record_event("workspace_settings_updated", actor=actor, metadata=settings)
        return settings

    def purge_retention(self, actor: str = "local") -> dict[str, Any]:
        days = self.workspace_settings()["retention_days"]
        if days <= 0:
            return {"deleted": 0, "retention_days": 0}
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        candidates = [
            case for case in self.list_cases(status="archived")
            if (_parse_time(case["updated_at"]) or datetime.now(timezone.utc)) < cutoff
        ]
        for case in candidates:
            self.delete_case(case["id"], actor=actor)
        self.record_event("retention_purge", actor=actor, metadata={"deleted": len(candidates), "days": days})
        return {"deleted": len(candidates), "retention_days": days}

    def product_metrics(self) -> dict[str, Any]:
        now_tick = time.monotonic()
        if self._metrics_cache is not None and now_tick - self._metrics_cache_at < 2.0:
            return dict(self._metrics_cache)
        with self._metrics_lock:
            now_tick = time.monotonic()
            if self._metrics_cache is not None and now_tick - self._metrics_cache_at < 2.0:
                return dict(self._metrics_cache)
            result = self._compute_product_metrics()
            self._metrics_cache = dict(result)
            self._metrics_cache_at = time.monotonic()
            return result

    def _compute_product_metrics(self) -> dict[str, Any]:
        cases = self.list_cases(status="all")
        queue = self.review_queue(reviewed=False)
        reviews = self.review_rows()
        decisive = [row for row in reviews if row["decision"] != "uncertain"]
        accepted = 0
        comparable = 0
        review_delays: list[float] = []
        active_review_seconds: list[float] = []
        sufficient = 0
        now = datetime.now(timezone.utc)
        verified_recent = 0
        for row in decisive:
            try:
                run = self.get_run(row["run_id"])
            except KeyError:
                continue
            result = (run.get("state") or {}).get("primary_result", {})
            label = str(result.get("label") or "")
            predicted_marketing: bool | None
            if label in {"高度营销化", "明显营销倾向"}:
                predicted_marketing = True
            elif label == "更像普通创作者":
                predicted_marketing = False
            else:
                predicted_marketing = None
            if predicted_marketing is not None:
                human_marketing = row["decision"] == "confirm_marketing"
                accepted += int(predicted_marketing == human_marketing)
                comparable += 1
            sufficient += int(not (result.get("missing") or []))
            run_time = _parse_time(run.get("updated_at"))
            review_time = _parse_time(row.get("updated_at") or row.get("created_at"))
            if run_time and review_time and review_time >= run_time:
                review_delays.append((review_time - run_time).total_seconds())
            if review_time and review_time >= now - timedelta(days=7):
                verified_recent += 1
            opened = self._latest_open_event(row["case_id"], row["run_id"], row["reviewer"], row["updated_at"])
            opened_time = _parse_time(opened.get("created_at")) if opened else None
            if opened_time and review_time and review_time >= opened_time:
                active_review_seconds.append(min(1800.0, max(10.0, (review_time - opened_time).total_seconds())))
        verified_rate = None
        if len(active_review_seconds) >= 3 and sum(active_review_seconds) > 0:
            verified_rate = round(len(active_review_seconds) / (sum(active_review_seconds) / 3600), 2)
        return {
            "cases": len(cases),
            "open_cases": sum(1 for case in cases if case["status"] == "open"),
            "archived_cases": sum(1 for case in cases if case["status"] == "archived"),
            "reviewed": len(reviews),
            "pending_review": len(queue),
            "monitoring_due": len(self.monitoring_queue(due_only=True)),
            "verified_last_7_days": verified_recent,
            "acceptance_rate": round(accepted / comparable, 4) if comparable else None,
            "overturn_rate": round(1 - accepted / comparable, 4) if comparable else None,
            "uncertain_rate": round(
                sum(1 for row in reviews if row["decision"] == "uncertain") / len(reviews), 4
            ) if reviews else None,
            "evidence_sufficiency_rate": round(sufficient / len(decisive), 4) if decisive else None,
            "median_time_to_review_seconds": round(median(review_delays), 1) if review_delays else None,
            "verified_per_active_review_hour": verified_rate,
        }

    def labeled_examples(self) -> list[LabeledExample]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT run_id, decision, features_json FROM reviews WHERE decision != 'uncertain' ORDER BY created_at"
            ).fetchall()
            self._close(conn)
        return [
            LabeledExample(
                FeatureVector(**json.loads(row["features_json"])),
                1 if row["decision"] == "confirm_marketing" else 0,
                row["run_id"],
            )
            for row in rows
        ]

    def get_calibration(self) -> Calibration:
        return Calibration.from_dict(self._setting("calibration"))

    def save_calibration(self, calibration: Calibration) -> None:
        self._save_setting("calibration", calibration.to_dict())

    def get_policy_profile(self) -> PolicyProfile:
        return PolicyProfile.from_dict(self._setting("policy_profile"))

    def save_policy_profile(self, profile: PolicyProfile) -> None:
        self._save_setting("policy_profile", profile.to_dict())

    def _latest_open_event(self, case_id: str, run_id: str, actor: str, before: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                """
                SELECT * FROM events
                WHERE event_type = 'case_opened' AND case_id = ? AND run_id = ? AND actor = ? AND created_at <= ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (case_id, run_id, actor, before),
            ).fetchone()
            self._close(conn)
        return self._event_row(row) if row else None

    def _setting(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
            self._close(conn)
        return json.loads(row["value_json"]) if row else None

    def _save_setting(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                """
                INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), utcnow_iso()),
            )
            conn.commit()
            self._close(conn)

    def _remove_asset_file(self, storage_path: str) -> None:
        try:
            candidate = Path(storage_path).resolve()
            root = self.asset_dir.resolve()
            if candidate.is_relative_to(root):
                candidate.unlink(missing_ok=True)
        except (OSError, RuntimeError):
            return

    @staticmethod
    def _clean_tags(tags: list[str]) -> list[str]:
        result: list[str] = []
        for raw in tags:
            tag = str(raw).strip()[:32]
            if tag and tag not in result:
                result.append(tag)
            if len(result) >= 12:
                break
        return result

    @staticmethod
    def _monitoring_due(case: dict[str, Any]) -> bool:
        if not case.get("monitoring_enabled"):
            return False
        next_check = _parse_time(case.get("next_check_at"))
        return next_check is None or next_check <= datetime.now(timezone.utc)

    @staticmethod
    def _review_priority(result: dict[str, Any], priority: str) -> float:
        if not result:
            return 0.0
        marketing = float(result.get("marketing_likelihood") or 0.0)
        covert = float(result.get("covert_promotion_risk") or 0.0)
        confidence = float(result.get("confidence") or 0.0)
        stability = float(result.get("stability") or 0.0)
        missing = len(result.get("missing") or [])
        score = 0.45 * marketing + 0.25 * covert + 0.15 * confidence + 0.15 * (1 - stability)
        score += min(0.09, missing * 0.02)
        score += 0.12 if priority == "high" else -0.04 if priority == "low" else 0.0
        return round(max(0.0, min(1.0, score)), 4)

    @staticmethod
    def _case_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "goal": row["goal"],
            "targets": json.loads(row["targets_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "status": row["status"],
            "priority": row["priority"],
            "owner": row["owner"],
            "tags": json.loads(row["tags_json"] or "[]"),
            "monitoring_enabled": bool(row["monitoring_enabled"]),
            "monitoring_interval_hours": int(row["monitoring_interval_hours"]),
            "next_check_at": row["next_check_at"],
            "last_source_refresh_at": row["last_source_refresh_at"],
            "batch_id": row["batch_id"],
        }

    @classmethod
    def _case_summary(cls, row: sqlite3.Row) -> dict[str, Any]:
        payload = cls._case_row(row)
        state = json.loads(row["latest_state_json"]) if row["latest_state_json"] else {}
        result = state.get("primary_result") or {}
        payload.update(
            {
                "latest_run_id": row["latest_run_id"],
                "latest_run_status": row["latest_run_status"],
                "latest_run_updated_at": row["latest_run_updated_at"],
                "latest_result": result or None,
                "latest_review_decision": row["latest_review_decision"],
                "monitoring_due": cls._monitoring_due(payload),
                "review_priority": cls._review_priority(result, payload["priority"])
                if row["latest_run_status"] == "completed" and result else None,
            }
        )
        return payload

    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "status": row["status"],
            "state": json.loads(row["state_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _asset_row(row: sqlite3.Row, include_text: bool) -> dict[str, Any]:
        payload = {
            "id": row["id"],
            "case_id": row["case_id"],
            "name": row["name"],
            "kind": row["kind"],
            "content_type": row["content_type"],
            "size": row["size"],
            "status": row["status"],
            "note": row["note"],
            "error": row["error"],
            "created_at": row["created_at"],
        }
        if include_text:
            payload["extracted_text"] = row["extracted_text"]
        return payload

    @staticmethod
    def _review_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "run_id": row["run_id"],
            "decision": row["decision"],
            "reason": row["reason"],
            "note": row["note"],
            "reviewer": row["reviewer"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "actor": row["actor"],
            "event_type": row["event_type"],
            "case_id": row["case_id"],
            "run_id": row["run_id"],
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "created_at": row["created_at"],
        }
