from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .detection import Calibration
from .domain import AssetSnapshot, FeatureVector, utcnow_iso
from .evolution import LabeledExample
from .policy import PolicyProfile

_REVIEW_DECISIONS = {"confirm_ordinary", "uncertain", "confirm_marketing"}


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
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_case_created ON messages(case_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_runs_case_created ON runs(case_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_assets_case_created ON assets(case_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_reviews_case_created ON reviews(case_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_reviews_decision ON reviews(decision, created_at DESC);
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
            messages = conn.execute(
                "SELECT * FROM messages WHERE case_id = ? ORDER BY created_at", (case_id,)
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
        payload["runs"] = [self._run_row(item) for item in runs]
        payload["assets"] = [self._asset_row(item, include_text=False) for item in assets]
        payload["reviews"] = [self._review_row(item) for item in reviews]
        return payload

    def delete_case(self, case_id: str) -> None:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
            if not row:
                self._close(conn)
                raise KeyError(case_id)
            paths = [
                item["storage_path"]
                for item in conn.execute(
                    "SELECT storage_path FROM assets WHERE case_id = ?", (case_id,)
                ).fetchall()
            ]
            conn.execute("DELETE FROM cases WHERE id = ?", (case_id,))
            conn.commit()
            self._close(conn)
        for path in paths:
            self._remove_asset_file(path)

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

    def delete_asset(self, case_id: str, asset_id: str) -> None:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT storage_path FROM assets WHERE id = ? AND case_id = ?", (asset_id, case_id)
            ).fetchone()
            if not row:
                self._close(conn)
                raise KeyError(asset_id)
            conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
            conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (utcnow_iso(), case_id))
            conn.commit()
            self._close(conn)
        self._remove_asset_file(row["storage_path"])

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
                    UPDATE reviews
                    SET decision = ?, reason = ?, note = ?, reviewer = ?, features_json = ?, updated_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        decision,
                        reason.strip(),
                        note.strip(),
                        reviewer.strip() or "local",
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
                        reviewer.strip() or "local",
                        json.dumps(features, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (now, case_id))
            conn.commit()
            row = conn.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone()
            self._close(conn)
        assert row is not None
        return self._review_row(row)

    def review_rows(self) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute("SELECT * FROM reviews ORDER BY created_at").fetchall()
            self._close(conn)
        return [self._review_row(row) for row in rows]

    def review_queue(self, reviewed: bool | None = False) -> list[dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                """
                SELECT c.id AS case_id, c.title, c.targets_json, c.updated_at,
                       r.id AS run_id, r.state_json, r.updated_at AS run_updated_at,
                       rv.id AS review_id, rv.decision, rv.reason, rv.note, rv.reviewer,
                       rv.created_at AS review_created_at, rv.updated_at AS review_updated_at
                FROM cases c
                JOIN runs r ON r.id = (
                    SELECT r2.id FROM runs r2
                    WHERE r2.case_id = c.id
                    ORDER BY r2.created_at DESC LIMIT 1
                )
                LEFT JOIN reviews rv ON rv.run_id = r.id
                WHERE r.status = 'completed'
                  AND NOT EXISTS (
                    SELECT 1 FROM runs active
                    WHERE active.case_id = c.id AND active.status = 'running'
                )
                ORDER BY r.updated_at DESC
                """
            ).fetchall()
            self._close(conn)

        items: list[dict[str, Any]] = []
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
            review = None
            if has_review:
                review = {
                    "id": row["review_id"],
                    "case_id": row["case_id"],
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
                    "case_id": row["case_id"],
                    "title": row["title"],
                    "targets": targets,
                    "updated_at": row["updated_at"],
                    "run_id": row["run_id"],
                    "run_updated_at": row["run_updated_at"],
                    "result": result,
                    "review": review,
                }
            )
        return items

    def product_metrics(self) -> dict[str, Any]:
        queue = self.review_queue(reviewed=False)
        reviews = self.review_rows()
        decisive = [row for row in reviews if row["decision"] != "uncertain"]
        accepted = 0
        for row in decisive:
            try:
                run = self.get_run(row["run_id"])
            except KeyError:
                continue
            score = float(
                (run.get("state") or {})
                .get("primary_result", {})
                .get("marketing_likelihood")
                or 0.0
            )
            predicted_marketing = score >= 0.5
            human_marketing = row["decision"] == "confirm_marketing"
            accepted += int(predicted_marketing == human_marketing)
        return {
            "cases": len(self.list_cases()),
            "reviewed": len(reviews),
            "pending_review": len(queue),
            "acceptance_rate": round(accepted / len(decisive), 4) if decisive else None,
            "uncertain_rate": round(
                sum(1 for row in reviews if row["decision"] == "uncertain") / len(reviews), 4
            )
            if reviews
            else None,
        }

    def labeled_examples(self) -> list[LabeledExample]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT run_id, decision, features_json FROM reviews WHERE decision != 'uncertain' ORDER BY created_at"
            ).fetchall()
            self._close(conn)
        examples: list[LabeledExample] = []
        for row in rows:
            label = 1 if row["decision"] == "confirm_marketing" else 0
            examples.append(
                LabeledExample(
                    FeatureVector(**json.loads(row["features_json"])),
                    label,
                    row["run_id"],
                )
            )
        return examples

    def get_calibration(self) -> Calibration:
        return Calibration.from_dict(self._setting("calibration"))

    def save_calibration(self, calibration: Calibration) -> None:
        self._save_setting("calibration", calibration.to_dict())

    def get_policy_profile(self) -> PolicyProfile:
        return PolicyProfile.from_dict(self._setting("policy_profile"))

    def save_policy_profile(self, profile: PolicyProfile) -> None:
        self._save_setting("policy_profile", profile.to_dict())

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
