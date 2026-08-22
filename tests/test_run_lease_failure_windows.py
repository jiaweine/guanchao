import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

import guanchao.harness as harness_module
import guanchao.run_lease as lease_module
from guanchao.harness import ActiveRunError, AgentHarness
from guanchao.run_lock import run_claim
from guanchao.store import Store


def _target(handle: str) -> dict:
    return {
        "platform": "weibo",
        "handle": handle,
        "posts": [{"id": f"{handle}-p1", "text": "今天散步"}],
    }


def _lease_row(store: Store, run_id: str):
    conn = store._connect()
    try:
        return conn.execute(
            "SELECT worker_id, lease_until FROM run_leases WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        store._close(conn)


def test_lease_confirmation_failure_fails_durable_run_before_executor_submit(tmp_path, monkeypatch):
    db = str(tmp_path / "confirm-failure.sqlite")
    store = Store(db)
    case = store.create_case("租约确认失败", "核查", [_target("confirm-failure")])
    harness = AgentHarness(store)
    submitted = False

    def fail_confirmation(db_path, case_id):
        raise sqlite3.OperationalError("database is locked")

    def forbidden_submit(run_id):
        nonlocal submitted
        submitted = True
        raise AssertionError("executor must not receive a run without durable ownership")

    monkeypatch.setattr(harness_module, "observe_running_case", fail_confirmation)
    monkeypatch.setattr(harness, "_submit", forbidden_submit)

    try:
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            harness.start(case["id"], "这次不能进入执行器")

        assert submitted is False
        snapshot = store.get_case(case["id"])
        assert len(snapshot["runs"]) == 1
        failed = snapshot["runs"][0]
        assert failed["status"] == "failed"
        assert store.active_run_for_case(case["id"]) is None
        assert _lease_row(store, failed["id"]) is None
        assert any(
            event["event_type"] == "run_started" and event["run_id"] == failed["id"]
            for event in store.audit_events(case_id=case["id"], limit=50)
        )
        assert any(
            event["event_type"] == "run_failed" and event["run_id"] == failed["id"]
            for event in store.audit_events(case_id=case["id"], limit=50)
        )
    finally:
        harness.close()
        lease_module.shutdown_heartbeats()


def test_generic_case_claim_cannot_hijack_fresh_unleased_run(tmp_path, monkeypatch):
    db = str(tmp_path / "fresh-unleased.sqlite")
    first_store = Store(db)
    second_store = Store(db)
    case = first_store.create_case("新 run 认领窗口", "核查", [_target("fresh-window")])
    first = AgentHarness(first_store)
    second = AgentHarness(second_store)

    confirmation_entered = Event()
    release_confirmation = Event()
    release_execution = Event()
    original_confirm = first._confirm_run_lease

    def delayed_confirm(case_id, run_id):
        confirmation_entered.set()
        assert release_confirmation.wait(3), "test did not release lease confirmation"
        return original_confirm(case_id, run_id)

    monkeypatch.setattr(first, "_confirm_run_lease", delayed_confirm)
    monkeypatch.setattr(first, "_execute", lambda run_id: release_execution.wait(3))
    monkeypatch.setattr(second, "_execute", lambda run_id: release_execution.wait(3))

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_start = executor.submit(first.start, case["id"], "第一个 worker")
            assert confirmation_entered.wait(1), "first worker did not reach confirmation window"

            active = first_store.active_run_for_case(case["id"])
            assert active is not None
            run_id = active["id"]
            assert _lease_row(first_store, run_id) is None

            with run_claim(db, case["id"]):
                observed = second_store.active_run_for_case(case["id"])
                assert observed is not None and observed["id"] == run_id
            assert _lease_row(first_store, run_id) is None

            with pytest.raises(ActiveRunError):
                second.start(case["id"], "并发第二个 worker")
            assert _lease_row(first_store, run_id) is None

            release_confirmation.set()
            assert first_start.result(timeout=3) == run_id

        lease = _lease_row(first_store, run_id)
        assert lease is not None
        assert lease["worker_id"] == lease_module.worker_id()
        assert first_store.get_run(run_id)["status"] == "running"
    finally:
        release_confirmation.set()
        active = first_store.active_run_for_case(case["id"])
        if active:
            first_store.update_run(active["id"], active["state"], "failed")
        release_execution.set()
        first.close()
        second.close()
        lease_module.shutdown_heartbeats()


def test_schema_cache_reinstalls_runtime_schema_after_database_file_replacement(tmp_path):
    db_path = tmp_path / "replace.sqlite"
    db = str(db_path)
    first = Store(db)
    case = first.create_case("旧文件", "核查", [_target("old-db")])
    lease_module.prepare_case_claim(db, case["id"])

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_leases'"
        ).fetchone()
    finally:
        conn.close()

    for suffix in ("-wal", "-shm"):
        Path(db + suffix).unlink(missing_ok=True)
    old_path = tmp_path / "replace-old.sqlite"
    db_path.rename(old_path)

    replacement = Store(db)
    fresh_case = replacement.create_case("新文件", "核查", [_target("new-db")])
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_leases'"
        ).fetchone() is None
    finally:
        conn.close()

    lease_module.prepare_case_claim(db, fresh_case["id"])
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_leases'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_runs_global_capacity_insert'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='trg_runs_terminal_release_lease'"
        ).fetchone()
    finally:
        conn.close()
    lease_module.shutdown_heartbeats()


def test_runtime_schema_migration_blocks_concurrent_writer_until_commit(tmp_path, monkeypatch):
    db = str(tmp_path / "atomic-schema.sqlite")
    store = Store(db)
    case = store.create_case("原子迁移", "核查", [_target("atomic-schema")])

    # Force the path through schema installation instead of a prior test's cache.
    lease_module._SCHEMA_READY.pop(lease_module._db_key(db), None)
    migration_entered = Event()
    release_migration = Event()
    writer_done = Event()
    original_ensure = lease_module.ensure_schema

    def blocked_ensure(conn, grace_seconds=None):
        # _ensure_path_schema acquires BEGIN IMMEDIATE before calling this hook.
        migration_entered.set()
        assert release_migration.wait(3), "test did not release runtime migration"
        return original_ensure(conn, grace_seconds)

    monkeypatch.setattr(lease_module, "ensure_schema", blocked_ensure)

    def install():
        lease_module.prepare_case_claim(db, case["id"])

    def writer():
        conn = sqlite3.connect(db, timeout=3)
        try:
            conn.execute(
                "UPDATE cases SET title = ? WHERE id = ?",
                ("迁移完成后写入", case["id"]),
            )
            conn.commit()
            writer_done.set()
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        install_future = executor.submit(install)
        assert migration_entered.wait(1), "runtime migration did not enter ensure_schema"
        writer_future = executor.submit(writer)
        time.sleep(0.15)
        assert not writer_done.is_set(), "writer crossed a partially-installed runtime schema"
        release_migration.set()
        install_future.result(timeout=3)
        writer_future.result(timeout=3)

    assert writer_done.is_set()
    assert store.get_case(case["id"])["title"] == "迁移完成后写入"
    lease_module.shutdown_heartbeats()


def test_terminal_transition_releases_lease_immediately(tmp_path, monkeypatch):
    db = str(tmp_path / "terminal-release.sqlite")
    store = Store(db)
    case = store.create_case("终态释放", "核查", [_target("terminal-release")])
    harness = AgentHarness(store)
    release_execution = Event()
    monkeypatch.setattr(harness, "_execute", lambda run_id: release_execution.wait(3))

    try:
        run_id = harness.start(case["id"], "开始")
        assert _lease_row(store, run_id) is not None
        run = store.get_run(run_id)
        store.update_run(run_id, run["state"], "failed")
        assert _lease_row(store, run_id) is None
    finally:
        release_execution.set()
        harness.close()
        lease_module.shutdown_heartbeats()
