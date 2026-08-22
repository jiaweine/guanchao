import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
from fastapi.testclient import TestClient

from guanchao.api import create_app
from guanchao.harness import ActiveRunError, AgentHarness
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
