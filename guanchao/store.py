from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .detection import Calibration
from .domain import utcnow_iso
from .evolution import LabeledExample
from .domain import FeatureVector


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("GUANCHAO_DB", "guanchao.db")
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        if self.path == ":memory:":
            self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._memory_conn.row_factory = sqlite3.Row
        self._init()

    def _connect(self) -> sqlite3.Connection:
        if self._memory_conn is not None:
            return self._memory_conn
        conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _close(self, conn: sqlite3.Connection) -> None:
        if self._memory_conn is None:
            conn.close()

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
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    label INTEGER NOT NULL,
                    features_json TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.commit()
            self._close(conn)

    def create_case(self, title: str, goal: str, targets: list[dict[str, Any]]) -> dict[str, Any]:
        now = utcnow_iso()
        case_id = uuid.uuid4().hex[:12]
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO cases VALUES (?, ?, ?, ?, ?, ?)",
                (case_id, title, goal, json.dumps(targets, ensure_ascii=False), now, now),
            )
            conn.commit()
            self._close(conn)
        return self.get_case(case_id)

    def list_cases(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute("SELECT * FROM cases ORDER BY updated_at DESC").fetchall()
            self._close(conn)
        return [self._case_row(row) for row in rows]

    def get_case(self, case_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
            if not row:
                self._close(conn)
                raise KeyError(case_id)
            messages = conn.execute("SELECT * FROM messages WHERE case_id = ? ORDER BY created_at", (case_id,)).fetchall()
            runs = conn.execute("SELECT * FROM runs WHERE case_id = ? ORDER BY created_at DESC", (case_id,)).fetchall()
            self._close(conn)
        payload = self._case_row(row)
        payload["messages"] = [dict(item) for item in messages]
        payload["runs"] = [self._run_row(item) for item in runs]
        return payload

    def add_message(self, case_id: str, role: str, content: str) -> dict[str, Any]:
        now = utcnow_iso()
        message = {"id": uuid.uuid4().hex[:12], "case_id": case_id, "role": role, "content": content, "created_at": now}
        with self._lock:
            conn = self._connect()
            conn.execute("INSERT INTO messages VALUES (?, ?, ?, ?, ?)", tuple(message.values()))
            conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (now, case_id))
            conn.commit()
            self._close(conn)
        return message

    def create_run(self, case_id: str, state: dict[str, Any]) -> dict[str, Any]:
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
        return self.get_run(run_id)

    def update_run(self, run_id: str, state: dict[str, Any], status: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE runs SET status = ?, state_json = ?, updated_at = ? WHERE id = ?",
                (status, json.dumps(state, ensure_ascii=False), utcnow_iso(), run_id),
            )
            conn.commit()
            self._close(conn)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            self._close(conn)
        if not row:
            raise KeyError(run_id)
        return self._run_row(row)

    def add_feedback(self, case_id: str, label: int, features: dict[str, float], note: str = "") -> dict[str, Any]:
        item = {
            "id": uuid.uuid4().hex[:12],
            "case_id": case_id,
            "label": int(label),
            "features_json": json.dumps(features, ensure_ascii=False),
            "note": note,
            "created_at": utcnow_iso(),
        }
        with self._lock:
            conn = self._connect()
            conn.execute("INSERT INTO feedback VALUES (?, ?, ?, ?, ?, ?)", tuple(item.values()))
            conn.commit()
            self._close(conn)
        return item

    def labeled_examples(self) -> list[LabeledExample]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute("SELECT label, features_json FROM feedback ORDER BY created_at").fetchall()
            self._close(conn)
        return [LabeledExample(FeatureVector(**json.loads(row["features_json"])), int(row["label"])) for row in rows]

    def get_calibration(self) -> Calibration:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT value_json FROM settings WHERE key = 'calibration'").fetchone()
            self._close(conn)
        return Calibration.from_dict(json.loads(row["value_json"]) if row else None)

    def save_calibration(self, calibration: Calibration) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES('calibration', ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (json.dumps(calibration.to_dict(), ensure_ascii=False), utcnow_iso()),
            )
            conn.commit()
            self._close(conn)

    @staticmethod
    def _case_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "goal": row["goal"],
            "targets": json.loads(row["targets_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

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
