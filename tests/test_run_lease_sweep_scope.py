from threading import Event

import guanchao.harness as harness_module
import guanchao.run_lease as lease_module
from guanchao.harness import AgentHarness
from guanchao.run_lock import run_claim
from guanchao.store import Store


def _target(handle: str) -> dict:
    return {
        "platform": "weibo",
        "handle": handle,
        "posts": [{"id": f"{handle}-p1", "text": "今天散步"}],
    }


def _fail_running(store: Store) -> None:
    conn = store._connect()
    try:
        rows = conn.execute("SELECT id FROM runs WHERE status = 'running'").fetchall()
    finally:
        store._close(conn)
    for row in rows:
        run = store.get_run(row["id"])
        store.update_run(run["id"], run["state"], "failed")


def test_generic_case_claim_does_not_scan_all_running_runs(tmp_path, monkeypatch):
    db = str(tmp_path / "case-only-reclaim.sqlite")
    store = Store(db)
    case = store.create_case("普通写锁", "核查", [_target("case-only")])

    lease_module.prepare_case_claim(db, case["id"])

    def forbidden_global_sweep(*args, **kwargs):
        raise AssertionError("ordinary case claims must not scan the workspace running set")

    monkeypatch.setattr(lease_module, "reclaim_expired_runs", forbidden_global_sweep)
    with run_claim(db, case["id"]):
        assert store.get_case(case["id"])["id"] == case["id"]


def test_stale_same_case_unleased_run_is_recovered_without_global_sweep(tmp_path):
    db = str(tmp_path / "same-case-orphan.sqlite")
    store = Store(db)
    case = store.create_case("同案无租约恢复", "核查", [_target("same-case-orphan")])

    # Install the new runtime schema first, then emulate a creator that committed a
    # running row but died before explicit lease confirmation.
    lease_module.prepare_case_claim(db, case["id"])
    run = store.create_run(case["id"], {"goal": "崩溃窗口", "targets": [_target("same-case-orphan")]})
    conn = store._connect()
    try:
        conn.execute(
            "UPDATE runs SET created_at = ?, updated_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", run["id"]),
        )
        conn.commit()
        assert conn.execute(
            "SELECT 1 FROM run_leases WHERE run_id = ?", (run["id"],)
        ).fetchone() is None
    finally:
        store._close(conn)

    with run_claim(db, case["id"]):
        assert store.active_run_for_case(case["id"]) is None

    assert store.get_run(run["id"])["status"] == "failed"
    assert any(
        event["event_type"] == "run_lease_expired"
        and event["run_id"] == run["id"]
        and event["metadata"].get("worker_id") == "orphaned-worker"
        for event in store.audit_events(case_id=case["id"], limit=50)
    )


def test_batch_start_runs_one_global_reclaim_sweep_for_the_whole_batch(tmp_path, monkeypatch):
    db = str(tmp_path / "batch-sweep.sqlite")
    store = Store(db)
    cases = [
        store.create_case(f"批量 {index}", "核查", [_target(f"batch-{index}")])
        for index in range(4)
    ]
    harness = AgentHarness(store)
    release = Event()
    calls = 0
    original_prepare = harness_module.prepare_run_start

    def counted_prepare(db_path):
        nonlocal calls
        calls += 1
        return original_prepare(db_path)

    monkeypatch.setattr(harness_module, "prepare_run_start", counted_prepare)
    monkeypatch.setattr(harness, "_execute", lambda run_id: release.wait(3))

    try:
        started = harness.start_many([case["id"] for case in cases], "批量开始")
        assert len(started) == len(cases)
        assert calls == 1
    finally:
        _fail_running(store)
        release.set()
        harness.close()
        lease_module.shutdown_heartbeats()
