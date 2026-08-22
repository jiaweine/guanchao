import time
from threading import Event

import pytest

from guanchao.harness import AgentHarness, RunCapacityError
from guanchao.run_lease import shutdown_heartbeats
from guanchao.store import Store


def _target(handle: str) -> dict:
    return {
        "platform": "weibo",
        "handle": handle,
        "posts": [{"id": f"{handle}-p1", "text": "今天散步"}],
    }


def _running_total(store: Store) -> int:
    conn = store._connect()
    try:
        row = conn.execute("SELECT COUNT(*) FROM runs WHERE status = 'running'").fetchone()
        return int(row[0])
    finally:
        store._close(conn)


def _fail_running(store: Store) -> None:
    conn = store._connect()
    try:
        rows = conn.execute("SELECT id FROM runs WHERE status = 'running'").fetchall()
    finally:
        store._close(conn)
    for row in rows:
        run = store.get_run(row["id"])
        store.update_run(run["id"], run["state"], "failed")


def test_global_inflight_limit_is_shared_by_multiple_harnesses(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_MAX_WORKERS", "2")
    monkeypatch.setenv("GUANCHAO_MAX_INFLIGHT", "2")
    db = str(tmp_path / "global-capacity.sqlite")
    first_store = Store(db)
    second_store = Store(db)
    cases = [
        first_store.create_case(f"容量 {index}", "核查", [_target(f"cap-{index}")])
        for index in range(3)
    ]
    first = AgentHarness(first_store)
    second = AgentHarness(second_store)
    release = Event()
    monkeypatch.setattr(first, "_execute", lambda run_id: release.wait(3))
    monkeypatch.setattr(second, "_execute", lambda run_id: release.wait(3))

    try:
        first.start(cases[0]["id"], "第一条")
        second.start(cases[1]["id"], "第二条")
        assert _running_total(first_store) == 2

        # Each Harness still has local semaphore room. Only the SQLite-wide
        # invariant can reject this third run, which proves worker counts do not
        # multiply GUANCHAO_MAX_INFLIGHT.
        with pytest.raises(RunCapacityError):
            first.start(cases[2]["id"], "不应越过全局上限")
        assert _running_total(first_store) == 2
        assert not [
            message
            for message in first_store.get_case(cases[2]["id"])["messages"]
            if message["role"] == "user"
        ]
    finally:
        _fail_running(first_store)
        release.set()
        first.close()
        second.close()
        shutdown_heartbeats()


def test_expired_run_on_other_case_is_reclaimed_before_capacity_check(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_MAX_WORKERS", "2")
    monkeypatch.setenv("GUANCHAO_MAX_INFLIGHT", "2")
    db = str(tmp_path / "global-capacity-reclaim.sqlite")
    store = Store(db)
    cases = [
        store.create_case(f"回收 {index}", "核查", [_target(f"reclaim-{index}")])
        for index in range(3)
    ]
    first = AgentHarness(store)
    second = AgentHarness(Store(db))
    release = Event()
    monkeypatch.setattr(first, "_execute", lambda run_id: release.wait(3))
    monkeypatch.setattr(second, "_execute", lambda run_id: release.wait(3))

    try:
        live_id = first.start(cases[0]["id"], "仍活着")
        stale_id = second.start(cases[1]["id"], "模拟崩溃")
        assert _running_total(store) == 2

        conn = store._connect()
        try:
            conn.execute(
                "UPDATE run_leases SET worker_id = 'dead-capacity-worker', lease_until = ? WHERE run_id = ?",
                ("2000-01-01T00:00:00+00:00", stale_id),
            )
            conn.commit()
        finally:
            store._close(conn)

        replacement = first.start(cases[2]["id"], "应先回收别案僵尸再占槽")
        assert replacement
        assert store.get_run(live_id)["status"] == "running"
        assert store.get_run(stale_id)["status"] == "failed"
        assert _running_total(store) == 2
        assert any(
            event["event_type"] == "run_lease_expired" and event["run_id"] == stale_id
            for event in store.audit_events(case_id=cases[1]["id"], limit=100)
        )
    finally:
        _fail_running(store)
        release.set()
        first.close()
        second.close()
        shutdown_heartbeats()
