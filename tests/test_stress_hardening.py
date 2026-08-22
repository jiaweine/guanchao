import asyncio
import math
import time
from threading import Event

import httpx
import pytest

from guanchao.api import create_app
from guanchao.detection import MarketingDetector
from guanchao.domain import (
    MAX_POSTS_PER_ACCOUNT,
    MAX_POST_TEXT_CHARS,
    AccountSnapshot,
    FeatureVector,
)
from guanchao.evolution import EvolutionEngine, LabeledExample
from guanchao.harness import AgentHarness, RunCapacityError
from guanchao.semantic import SemanticEvidenceGateway, SemanticSignal
from guanchao.store import Store
from guanchao.tools import MAX_MEDIA_EVIDENCE_CHARS, ToolRegistry


def _target(handle: str) -> dict:
    return {
        "platform": "weibo",
        "handle": handle,
        "bio": "日常记录，也会标注品牌合作",
        "posts": [
            {
                "id": f"{handle}-{index}",
                "text": "今天记录真实体验，不过也有人问同款入口。",
                "created_at": f"2026-08-{index + 1:02d}T08:00:00+00:00",
            }
            for index in range(6)
        ],
    }


def _wait_run(client, run_id: str) -> dict:
    for _ in range(300):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def test_import_normalization_bounds_hostile_nested_input_and_boolean_strings():
    raw = {
        "platform": "weibo",
        "handle": "h" * 1000,
        "bio": "b" * 50_000,
        "verified": "false",
        "posts": [
            {
                "id": str(index),
                "text": "x" * 50_000,
                "created_at": "2026-08-01T08:00:00+00:00",
            }
            for index in range(MAX_POSTS_PER_ACCOUNT + 50)
        ],
    }
    account = AccountSnapshot.from_dict(raw)
    assert account.verified is False
    assert len(account.posts) == MAX_POSTS_PER_ACCOUNT
    assert len(account.posts[0].text) == MAX_POST_TEXT_CHARS
    assert account.posts[0].published_at == "2026-08-01T08:00:00+00:00"
    assert AccountSnapshot.from_dict({**raw, "posts": [], "verified": "yes"}).verified is True


def test_mixed_naive_and_aware_timestamps_do_not_crash_cadence_analysis():
    account = AccountSnapshot.from_dict(
        {
            "platform": "weibo",
            "handle": "mixed-time",
            "posts": [
                {"text": "新品体验", "published_at": "2026-08-01T08:00:00"},
                {"text": "今天散步", "published_at": "2026-08-02T08:00:00Z"},
                {"text": "周末做饭", "published_at": "2026-08-03T16:00:00+08:00"},
            ],
        }
    )
    result = MarketingDetector().analyze(account)
    assert math.isfinite(result.features.cadence_burst)
    assert 0.0 <= result.features.cadence_burst <= 1.0


def test_evolution_never_splits_the_same_group_across_folds():
    examples = [
        LabeledExample(FeatureVector(commercial_language=0.1), 0, "same-case"),
        LabeledExample(FeatureVector(commercial_language=0.2), 0, "same-case"),
        LabeledExample(FeatureVector(commercial_language=0.8), 1, "positive-a"),
        LabeledExample(FeatureVector(commercial_language=0.9), 1, "positive-b"),
        LabeledExample(FeatureVector(commercial_language=0.05), 0, "negative-b"),
    ]
    folds = EvolutionEngine._folds(examples, 2)
    locations = {
        fold_index
        for fold_index, fold in enumerate(folds)
        if any(item.group == "same-case" for item in fold)
    }
    assert len(locations) == 1
    assert sum(item.group == "same-case" for fold in folds for item in fold) == 2


def test_semantic_cache_is_lru_bounded(monkeypatch):
    gateway = SemanticEvidenceGateway()
    gateway.endpoint = "http://unused"
    gateway._cache_limit = 2
    monkeypatch.setattr(
        gateway,
        "_request",
        lambda account, media: SemanticSignal(
            values={"commercial_language": 0.2}, grounded_fraction=1.0
        ),
    )
    for index in range(4):
        gateway.inspect(AccountSnapshot.from_dict(_target(f"cache-{index}")))
    assert len(gateway._cache) == 2


def test_media_evidence_aggregation_has_a_hard_total_bound():
    state = {
        "assets": [
            {"status": "ready", "extracted_text": "证据" * 20_000}
            for _ in range(20)
        ]
    }
    text = ToolRegistry._media_text(state)
    assert len(text) <= MAX_MEDIA_EVIDENCE_CHARS


def test_fast_executor_tasks_do_not_leave_completed_futures_registered(tmp_path, monkeypatch):
    harness = AgentHarness(Store(str(tmp_path / "future-race.sqlite")))
    monkeypatch.setattr(harness, "_execute", lambda run_id: None)
    for index in range(200):
        harness._submit(f"instant-{index}")
    deadline = time.time() + 2
    while harness._futures and time.time() < deadline:
        time.sleep(0.005)
    assert harness._futures == {}
    harness.close()


def test_background_learning_schedule_failure_does_not_flip_completed_run(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "learning-failure.sqlite"))
    harness = AgentHarness(store)
    case = store.create_case("学习调度故障", "仔细核查", [_target("learning-failure")])

    def fail_schedule(*args, **kwargs):
        raise RuntimeError("learning executor unavailable")

    monkeypatch.setattr(harness, "_schedule_trajectory_learning", fail_schedule)
    run = harness.execute_inline(case["id"], case["goal"])
    assert run["status"] == "completed"
    assert any(
        item["event_type"] == "harness_learning_schedule_failed"
        and item["run_id"] == run["id"]
        for item in store.audit_events(limit=100)
    )
    harness.close()


def test_bad_worker_environment_values_fall_back_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_MAX_WORKERS", "not-an-int")
    monkeypatch.setenv("GUANCHAO_MAX_INFLIGHT", "-999")
    monkeypatch.setenv("GUANCHAO_MODEL_TIMEOUT", "nonsense")
    harness = AgentHarness(Store(str(tmp_path / "bad-env.sqlite")))
    assert harness._max_inflight >= 2
    harness.close()
    with pytest.raises(RunCapacityError):
        harness.start("missing", "closed")


def test_latest_review_per_case_replaces_older_policy_and_calibration_supervision(tmp_path):
    from fastapi.testclient import TestClient

    app = create_app(str(tmp_path / "latest-review.sqlite"))
    client = TestClient(app)
    case = client.post(
        "/api/cases",
        json={"title": "最新复核", "goal": "仔细核查", "targets": [_target("latest-review")]},
    ).json()

    first_id = client.post(
        f"/api/cases/{case['id']}/messages", json={"content": "第一次核查"}
    ).json()["run_id"]
    _wait_run(client, first_id)
    assert client.post(
        "/api/reviews",
        json={"case_id": case["id"], "run_id": first_id, "decision": "confirm_ordinary"},
    ).status_code == 200
    app.state.harness.wait_learning(10)

    second_id = client.post(
        f"/api/cases/{case['id']}/messages", json={"content": "第二次核查"}
    ).json()["run_id"]
    second_run = _wait_run(client, second_id)
    assert client.post(
        "/api/reviews",
        json={"case_id": case["id"], "run_id": second_id, "decision": "confirm_marketing"},
    ).status_code == 200
    app.state.harness.wait_learning(10)

    profile = app.state.store.get_policy_profile()
    assert set(profile.review_feedback) == {case["id"]}
    assert profile.reviews == 1
    examples = app.state.harness.review_examples()
    assert len(examples) == 1
    assert examples[0].group == case["id"]
    assert examples[0].label == 1
    assert examples[0].features.asdict() == second_run["state"]["primary_result"]["features"]


def test_cancelling_one_idempotent_waiter_does_not_poison_other_retries(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "idempotent-cancel.sqlite"))
    entered = Event()
    release = Event()
    original = app.state.store.create_case

    def slow_create(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args, **kwargs)

    monkeypatch.setattr(app.state.store, "create_case", slow_create)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        payload = {"title": "取消等待者", "goal": "核查", "targets": [_target("cancel-waiter")]}
        headers = {"X-Guanchao-Request-Key": "cancel-shared-waiter"}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            owner = asyncio.create_task(client.post("/api/cases", headers=headers, json=payload))
            assert await asyncio.to_thread(entered.wait, 1)
            waiter = asyncio.create_task(client.post("/api/cases", headers=headers, json=payload))
            await asyncio.sleep(0.08)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            release.set()
            first = await asyncio.wait_for(owner, 2)
            assert first.status_code == 200
            replay = await client.post("/api/cases", headers=headers, json=payload)
            assert replay.status_code == 200
            assert replay.json()["id"] == first.json()["id"]

    asyncio.run(scenario())
