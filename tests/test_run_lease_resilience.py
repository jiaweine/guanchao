import sqlite3
import time

import guanchao.run_lease as lease_module
from guanchao.run_lease import ensure_schema, insert_lease, worker_id
from guanchao.store import Store


def _target() -> dict:
    return {
        "platform": "weibo",
        "handle": "lease-resilience",
        "posts": [{"id": "p1", "text": "今天散步"}],
    }


def test_transient_heartbeat_database_error_keeps_registration_for_retry(tmp_path, monkeypatch):
    db = str(tmp_path / "lease-retry.sqlite")
    store = Store(db)
    case = store.create_case("租约重试", "核查", [_target()])

    conn = store._connect()
    try:
        ensure_schema(conn)
        conn.commit()
    finally:
        store._close(conn)

    run = store.create_run(case["id"], {"goal": "核查", "targets": [_target()]})
    owner = worker_id()
    conn = store._connect()
    try:
        insert_lease(conn, run["id"], owner, 5)
        conn.commit()
        before = conn.execute(
            "SELECT lease_until FROM run_leases WHERE run_id = ?", (run["id"],)
        ).fetchone()["lease_until"]
    finally:
        store._close(conn)

    registry = lease_module._HeartbeatRegistry()
    key = (lease_module._db_key(db), run["id"])
    item = lease_module._Registration(db, run["id"], 5, 0.0)
    registry._items[key] = item
    original_connect = lease_module._connect

    def locked_once(path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(lease_module, "_connect", locked_once)
    registry._renew(item)
    assert registry._items.get(key) is item
    assert item.next_renew_at > time.monotonic()

    monkeypatch.setattr(lease_module, "_connect", original_connect)
    time.sleep(0.02)
    registry._renew(item)
    assert registry._items.get(key) is item

    conn = store._connect()
    try:
        after = conn.execute(
            "SELECT worker_id, lease_until FROM run_leases WHERE run_id = ?", (run["id"],)
        ).fetchone()
        assert after["worker_id"] == owner
        assert after["lease_until"] >= before
    finally:
        store._close(conn)
        current = store.get_run(run["id"])
        store.update_run(run["id"], current["state"], "failed")
