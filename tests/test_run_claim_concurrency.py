import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from guanchao.api import create_app
from guanchao.harness import ActiveRunError, AgentHarness
from guanchao.run_lease import ensure_schema, shutdown_heartbeats, worker_id
from guanchao.run_lock import run_claim
from guanchao.store import Store


def _target() -> dict:
    return {
        "platform": "weibo",
        "handle": "shared-case",
        "posts": [
            {"id": "p1", "text": "今天散步"},
            {"id": "p2", "text": "今天看书"},
        ],
    }


def _running_count(store: Store, case_id: str) -> int:
    conn = store._connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE case_id = ? AND status = 'running'",
            (case_id,),
        ).fetchone()
        return int(row[0])
    finally:
        store._close(conn)


def test_two_harness_instances_cannot_claim_the_same_case_simultaneously(tmp_path, monkeypatch):
    db = str(tmp_path / "shared.sqlite")
    first_store = Store(db)
    case = first_store.create_case("共享任务", "核查", [_target()])
    second_store = Store(db)
    first = AgentHarness(first_store)
    second = AgentHarness(second_store)

    release = Event()
    monkeypatch.setattr(first, "_execute", lambda run_id: release.wait(2))
    monkeypatch.setattr(second, "_execute", lambda run_id: release.wait(2))

    first_active = first_store.active_run_for_case
    second_active = second_store.active_run_for_case

    def delayed_first(case_id):
        result = first_active(case_id)
        time.sleep(0.08)
        return result

    def delayed_second(case_id):
        result = second_active(case_id)
        time.sleep(0.08)
        return result

    monkeypatch.setattr(first_store, "active_run_for_case", delayed_first)
    monkeypatch.setattr(second_store, "active_run_for_case", delayed_second)

    def attempt(harness):
        try:
            return ("ok", harness.start(case["id"], "并发核查"))
        except ActiveRunError as exc:
            return ("active", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, (first, second)))

    assert sorted(kind for kind, _ in outcomes) == ["active", "ok"]
    active = first_store.active_run_for_case(case["id"])
    assert active is not None
    assert _running_count(first_store, case["id"]) == 1

    first_store.update_run(active["id"], active["state"], "failed")
    release.set()
    first.close()
    second.close()


def test_db_running_conflict_rolls_back_user_message_atomically(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "atomic-run.sqlite"))
    case = store.create_case("原子启动", "核查", [_target()])
    existing = store.create_run(case["id"], {"goal": "占位", "targets": [_target()]})
    harness = AgentHarness(store)
    original_active = store.active_run_for_case
    calls = 0

    def stale_then_real(case_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original_active(case_id)

    monkeypatch.setattr(store, "active_run_for_case", stale_then_real)
    before = [
        item["content"]
        for item in store.get_case(case["id"])["messages"]
        if item["role"] == "user"
    ]
    try:
        try:
            harness._prepare_run(case["id"], "不应残留的消息", "local")
        except ActiveRunError as exc:
            assert str(exc) == existing["id"]
        else:
            raise AssertionError("DB-level running collision was not translated")

        after = [
            item["content"]
            for item in store.get_case(case["id"])["messages"]
            if item["role"] == "user"
        ]
        assert after == before
        assert _running_count(store, case["id"]) == 1
    finally:
        current = store.get_run(existing["id"])
        store.update_run(existing["id"], current["state"], "failed")
        harness.close()


def test_async_http_case_claim_reenters_harness_start_without_deadlock(tmp_path):
    app = create_app(str(tmp_path / "reentrant.sqlite"))
    setup = TestClient(app)
    case = setup.post(
        "/api/cases",
        json={"title": "可重入锁", "goal": "核查", "targets": [_target()]},
    ).json()

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await asyncio.wait_for(
                client.post(
                    f"/api/cases/{case['id']}/messages",
                    json={"content": "开始核查"},
                ),
                timeout=2,
            )
            assert response.status_code == 200
            run_id = response.json()["run_id"]
            await asyncio.to_thread(app.state.harness.wait, run_id, 10)
            assert app.state.store.get_run(run_id)["status"] in {"completed", "failed"}

    try:
        asyncio.run(scenario())
    finally:
        app.state.harness.close()


def test_cross_app_run_waits_for_same_case_asset_perception_snapshot(tmp_path, monkeypatch):
    db = str(tmp_path / "shared-api.sqlite")
    first_app = create_app(db)
    setup = TestClient(first_app)
    case = setup.post(
        "/api/cases",
        json={"title": "跨 worker 素材", "goal": "核查", "targets": [_target()]},
    ).json()
    second_app = create_app(db)

    perception_entered = Event()
    release_perception = Event()

    def slow_extract(path, kind, content_type):
        perception_entered.set()
        assert release_perception.wait(3), "test did not release perception"
        return "跨 worker 可核对素材事实", "ready"

    monkeypatch.setattr(first_app.state.perception, "extract", slow_extract)

    async def scenario():
        first_transport = httpx.ASGITransport(app=first_app)
        second_transport = httpx.ASGITransport(app=second_app)
        async with (
            httpx.AsyncClient(transport=first_transport, base_url="http://first") as first,
            httpx.AsyncClient(transport=second_transport, base_url="http://second") as second,
        ):
            upload_task = asyncio.create_task(
                first.post(
                    f"/api/cases/{case['id']}/assets",
                    files={"file": ("evidence.txt", b"evidence", "text/plain")},
                )
            )
            assert await asyncio.to_thread(perception_entered.wait, 1)

            run_task = asyncio.create_task(
                second.post(
                    f"/api/cases/{case['id']}/messages",
                    json={"content": "素材稳定后开始"},
                )
            )
            await asyncio.sleep(0.15)
            assert not run_task.done(), "another worker crossed the in-flight asset snapshot"

            release_perception.set()
            uploaded, started = await asyncio.wait_for(
                asyncio.gather(upload_task, run_task), timeout=4
            )
            assert uploaded.status_code == 200
            assert uploaded.json()["status"] == "ready"
            assert started.status_code == 200

            run_id = started.json()["run_id"]
            run = second_app.state.store.get_run(run_id)
            assert run["state"]["assets"]
            assert run["state"]["assets"][0]["status"] == "ready"
            assert "跨 worker 可核对素材事实" in run["state"]["assets"][0]["extracted_text"]
            await asyncio.to_thread(second_app.state.harness.wait, run_id, 10)

    try:
        asyncio.run(scenario())
    finally:
        release_perception.set()
        first_app.state.harness.close()
        second_app.state.harness.close()


def test_learning_read_modify_write_is_serialized_across_harnesses(tmp_path, monkeypatch):
    db = str(tmp_path / "shared-learning.sqlite")
    first_store = Store(db)
    second_store = Store(db)
    first = AgentHarness(first_store)
    second = AgentHarness(second_store)

    first_read = Event()
    second_read = Event()
    release_first = Event()
    original_first_get = first_store.get_policy_profile
    original_second_get = second_store.get_policy_profile

    def blocked_first_get():
        profile = original_first_get()
        first_read.set()
        assert release_first.wait(3), "test did not release first learner"
        return profile

    def observed_second_get():
        second_read.set()
        return original_second_get()

    monkeypatch.setattr(first_store, "get_policy_profile", blocked_first_get)
    monkeypatch.setattr(second_store, "get_policy_profile", observed_second_get)

    trajectory = [
        {
            "action": "verdict.compose",
            "features": [1.0] * 11,
            "alternative": "evidence.challenge",
            "alternative_features": [0.5] * 11,
            "reward": 0.5,
            "duration_ms": 1.0,
        }
    ]

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_job = executor.submit(
                first._apply_trajectory_learning, "first-learning", trajectory
            )
            assert first_read.wait(1)
            second_job = executor.submit(
                second._apply_trajectory_learning, "second-learning", trajectory
            )
            time.sleep(0.15)
            assert not second_read.is_set(), "second worker read stale learning state concurrently"
            release_first.set()
            first_job.result(timeout=3)
            second_job.result(timeout=3)

        profile = Store(db).get_policy_profile()
        assert profile.steps == 2
        assert len(profile.experiences) == 2
    finally:
        release_first.set()
        first.close()
        second.close()


def test_expired_worker_lease_is_reclaimed_and_terminal_run_cannot_resurrect(tmp_path, monkeypatch):
    db = str(tmp_path / "lease-reclaim.sqlite")
    store = Store(db)
    case = store.create_case("崩溃恢复", "核查", [_target()])
    stale = store.create_run(case["id"], {"goal": "旧 worker", "targets": [_target()]})

    conn = store._connect()
    try:
        ensure_schema(conn, grace_seconds=30)
        conn.execute(
            "UPDATE run_leases SET worker_id = 'dead-worker', lease_until = ? WHERE run_id = ?",
            ("2000-01-01T00:00:00+00:00", stale["id"]),
        )
        conn.commit()
    finally:
        store._close(conn)

    harness = AgentHarness(store)
    release = Event()
    monkeypatch.setattr(harness, "_execute", lambda run_id: release.wait(2))
    try:
        replacement = harness.start(case["id"], "接管旧任务")
        assert replacement != stale["id"]
        assert store.get_run(stale["id"])["status"] == "failed"
        assert store.active_run_for_case(case["id"])["id"] == replacement
        assert any(
            event["event_type"] == "run_lease_expired" and event["run_id"] == stale["id"]
            for event in store.audit_events(case_id=case["id"], limit=100)
        )

        with pytest.raises(sqlite3.IntegrityError):
            store.update_run(stale["id"], {"goal": "旧 worker 试图复活"}, "running")
        assert store.get_run(stale["id"])["status"] == "failed"
    finally:
        active = store.active_run_for_case(case["id"])
        if active:
            store.update_run(active["id"], active["state"], "failed")
        release.set()
        harness.close()
        shutdown_heartbeats()


def test_live_foreign_worker_lease_is_not_reclaimed(tmp_path):
    db = str(tmp_path / "lease-live.sqlite")
    store = Store(db)
    case = store.create_case("活租约", "核查", [_target()])
    run = store.create_run(case["id"], {"goal": "仍活着", "targets": [_target()]})

    conn = store._connect()
    try:
        ensure_schema(conn, grace_seconds=30)
        conn.execute(
            "UPDATE run_leases SET worker_id = 'other-live-worker', lease_until = ? WHERE run_id = ?",
            ("2099-01-01T00:00:00+00:00", run["id"]),
        )
        conn.commit()
    finally:
        store._close(conn)

    with run_claim(db, case["id"]):
        active = store.active_run_for_case(case["id"])
        assert active is not None and active["id"] == run["id"]

    conn = store._connect()
    try:
        lease = conn.execute(
            "SELECT worker_id FROM run_leases WHERE run_id = ?", (run["id"],)
        ).fetchone()
        assert lease is not None and lease["worker_id"] == "other-live-worker"
    finally:
        store._close(conn)
    store.update_run(run["id"], run["state"], "failed")
    shutdown_heartbeats()


def test_harness_claim_installs_owned_lease_and_heartbeat_renews_it(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_RUN_LEASE_SECONDS", "5")
    db = str(tmp_path / "lease-heartbeat.sqlite")
    store = Store(db)
    case = store.create_case("心跳续租", "核查", [_target()])
    harness = AgentHarness(store)
    release = Event()
    monkeypatch.setattr(harness, "_execute", lambda run_id: release.wait(4))

    try:
        run_id = harness.start(case["id"], "保持运行")
        conn = store._connect()
        try:
            first = conn.execute(
                "SELECT worker_id, lease_until FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert first is not None
            assert first["worker_id"] == worker_id()
            first_deadline = first["lease_until"]
        finally:
            store._close(conn)

        time.sleep(2.1)
        conn = store._connect()
        try:
            second = conn.execute(
                "SELECT worker_id, lease_until FROM run_leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert second is not None
            assert second["worker_id"] == worker_id()
            assert second["lease_until"] > first_deadline
        finally:
            store._close(conn)
    finally:
        active = store.active_run_for_case(case["id"])
        if active:
            store.update_run(active["id"], active["state"], "failed")
        release.set()
        harness.close()
        shutdown_heartbeats()
