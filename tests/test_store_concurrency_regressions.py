import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from guanchao.domain import FeatureVector
from guanchao.harness import AgentHarness
from guanchao.store import Store


class _FetchBarrierCursor:
    def __init__(self, cursor, barrier: Barrier, method: str):
        self._cursor = cursor
        self._barrier = barrier
        self._method = method

    def fetchall(self):
        rows = self._cursor.fetchall()
        if self._method == "fetchall":
            self._barrier.wait(timeout=3)
        return rows

    def fetchone(self):
        row = self._cursor.fetchone()
        if self._method == "fetchone":
            self._barrier.wait(timeout=3)
        return row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _SchemaRaceConnection:
    def __init__(self, connection: sqlite3.Connection, barrier: Barrier):
        self._connection = connection
        self._barrier = barrier
        self._armed = True

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        if self._armed and sql.strip().startswith("PRAGMA table_info(legacy)"):
            self._armed = False
            return _FetchBarrierCursor(cursor, self._barrier, "fetchall")
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _ReviewRaceConnection:
    def __init__(self, connection: sqlite3.Connection, barrier: Barrier):
        self._connection = connection
        self._barrier = barrier

    def execute(self, sql, parameters=()):
        cursor = self._connection.execute(sql, parameters)
        # Old Store.add_review performed this check before INSERT. Force both old
        # workers to observe "missing" before either can insert; the atomic UPSERT
        # implementation no longer executes this query and therefore needs no
        # special synchronization here.
        if sql.strip().startswith("SELECT id FROM reviews WHERE run_id"):
            return _FetchBarrierCursor(cursor, self._barrier, "fetchone")
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


def _target(handle: str) -> dict:
    return {
        "platform": "weibo",
        "handle": handle,
        "posts": [{"id": f"{handle}-p1", "text": "今天散步"}],
    }


def _completed_reviewable_run(store: Store):
    case = store.create_case("并发复核", "核查", [_target("review-race")])
    state = {
        "goal": "核查",
        "targets": [_target("review-race")],
        "primary_result": {
            "label": "更像普通创作者",
            "features": FeatureVector().asdict(),
        },
        "trajectory": [],
    }
    run = store.create_run(case["id"], state)
    store.update_run(run["id"], state, "completed")
    return case, run


def test_ensure_column_tolerates_only_completed_competing_migration(tmp_path):
    db = str(tmp_path / "column-race.sqlite")
    setup = sqlite3.connect(db)
    setup.execute("CREATE TABLE legacy(id TEXT PRIMARY KEY)")
    setup.commit()
    setup.close()

    barrier = Barrier(2)
    first_raw = sqlite3.connect(db, timeout=3, check_same_thread=False, isolation_level=None)
    second_raw = sqlite3.connect(db, timeout=3, check_same_thread=False, isolation_level=None)
    first_raw.execute("PRAGMA busy_timeout=3000")
    second_raw.execute("PRAGMA busy_timeout=3000")
    first = _SchemaRaceConnection(first_raw, barrier)
    second = _SchemaRaceConnection(second_raw, barrier)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    Store._ensure_column,
                    connection,
                    "legacy",
                    "status",
                    "TEXT NOT NULL DEFAULT 'open'",
                )
                for connection in (first, second)
            ]
            for future in futures:
                future.result(timeout=4)

        verify = sqlite3.connect(db)
        try:
            columns = [row[1] for row in verify.execute("PRAGMA table_info(legacy)").fetchall()]
        finally:
            verify.close()
        assert columns.count("status") == 1
    finally:
        first_raw.close()
        second_raw.close()


def test_ensure_column_does_not_hide_unrelated_operational_error(tmp_path):
    db = str(tmp_path / "column-error.sqlite")
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE legacy(id TEXT PRIMARY KEY)")
    raw.commit()

    class _BrokenAlter:
        def execute(self, sql, parameters=()):
            if sql.strip().startswith("ALTER TABLE"):
                raise sqlite3.OperationalError("simulated disk I/O error")
            return raw.execute(sql, parameters)

    try:
        with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
            Store._ensure_column(_BrokenAlter(), "legacy", "status", "TEXT")
    finally:
        raw.close()


def test_concurrent_first_review_submissions_upsert_one_run_atomically(tmp_path, monkeypatch):
    db = str(tmp_path / "review-upsert.sqlite")
    setup = Store(db)
    case, run = _completed_reviewable_run(setup)
    first = Store(db)
    second = Store(db)
    old_select_barrier = Barrier(2)
    start_barrier = Barrier(2)

    def wrap_store(store: Store):
        original = store._connect

        def connect():
            return _ReviewRaceConnection(original(), old_select_barrier)

        monkeypatch.setattr(store, "_connect", connect)

    wrap_store(first)
    wrap_store(second)

    def submit(store: Store, decision: str):
        start_barrier.wait(timeout=3)
        return store.add_review(
            case["id"],
            run["id"],
            decision,
            reason=f"{decision}-reason",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        one = executor.submit(submit, first, "confirm_ordinary")
        two = executor.submit(submit, second, "confirm_marketing")
        first_result = one.result(timeout=5)
        second_result = two.result(timeout=5)

    rows = setup.review_rows()
    assert len(rows) == 1
    assert first_result["id"] == second_result["id"] == rows[0]["id"]
    assert rows[0]["run_id"] == run["id"]
    assert rows[0]["decision"] in {"confirm_ordinary", "confirm_marketing"}


def test_review_learning_exact_lookup_never_scans_review_rows(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "exact-review-lookup.sqlite"))
    harness = AgentHarness(store)

    monkeypatch.setattr(store, "review_for_run", lambda run_id: None)
    monkeypatch.setattr(
        store,
        "review_rows",
        lambda: (_ for _ in ()).throw(
            AssertionError("single review feedback must not scan the full review table")
        ),
    )

    try:
        harness._apply_review_learning(
            "missing-review",
            "confirm_ordinary",
            "local",
        )
    finally:
        harness.close()
