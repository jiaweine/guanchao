import json

from fastapi.testclient import TestClient

from guanchao.api import create_app
from guanchao.domain import FeatureVector
from guanchao.post_training import PostTrainingCorpusBuilder
from guanchao.store import Store


def test_post_training_export_binds_review_to_exact_run():
    cases = [
        {
            "id": "c1",
            "goal": "核查",
            "runs": [
                {
                    "id": "r2",
                    "status": "completed",
                    "state": {
                        "goal": "第二次核查",
                        "targets": [{"handle": "a"}],
                        "assets": [],
                        "events": [{"kind": "tool", "tool": "content.scan", "status": "done", "detail": "完成"}],
                        "answer": "第二次判断",
                    },
                },
                {
                    "id": "r1",
                    "status": "completed",
                    "state": {
                        "goal": "第一次核查",
                        "targets": [{"handle": "a"}],
                        "assets": [],
                        "events": [],
                        "answer": "第一次判断",
                    },
                },
            ],
        }
    ]
    reviews = [
        {"case_id": "c1", "run_id": "r2", "decision": "confirm_marketing", "reason": "证据充分", "note": ""},
        {"case_id": "c1", "run_id": "r1", "decision": "uncertain", "reason": "资料不足", "note": ""},
    ]
    rows = [json.loads(line) for line in PostTrainingCorpusBuilder().build_jsonl(cases, reviews).splitlines()]
    assert len(rows) == 1
    assert rows[0]["run_id"] == "r2"
    assert rows[0]["human_label"] == 1
    assert rows[0]["trajectory"][0]["tool"] == "content.scan"
    assert rows[0]["review"]["reason"] == "证据充分"


def _reviewed_store(db_path: str) -> tuple[Store, dict, dict]:
    store = Store(db_path)
    target = {
        "platform": "weibo",
        "handle": "training-one-query",
        "posts": [{"id": "p1", "text": "今天散步"}],
    }
    case = store.create_case("训练导出", "核查", [target])
    state = {
        "goal": "精确复核",
        "targets": [target],
        "assets": [],
        "events": [
            {"kind": "tool", "tool": "content.scan", "status": "done", "detail": "完成"}
        ],
        "answer": "更像普通创作者",
        "primary_result": {
            "label": "更像普通创作者",
            "features": FeatureVector().asdict(),
        },
    }
    run = store.create_run(case["id"], state)
    store.update_run(run["id"], state, "completed")
    store.add_review(case["id"], run["id"], "confirm_ordinary", reason="人工确认")
    return store, case, run


def test_store_export_uses_one_query_and_never_loads_full_case_histories(tmp_path, monkeypatch):
    store, case, run = _reviewed_store(str(tmp_path / "post-training.sqlite"))

    monkeypatch.setattr(
        store,
        "get_case",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("optimized export must not load full case history")
        ),
    )
    calls = 0
    original_connect = store._connect

    def counted_connect():
        nonlocal calls
        calls += 1
        return original_connect()

    monkeypatch.setattr(store, "_connect", counted_connect)
    rows = [
        json.loads(line)
        for line in PostTrainingCorpusBuilder().build_jsonl_from_store(store).splitlines()
    ]
    assert calls == 1
    assert len(rows) == 1
    assert rows[0]["case_id"] == case["id"]
    assert rows[0]["run_id"] == run["id"]
    assert rows[0]["goal"] == "精确复核"
    assert rows[0]["human_label"] == 0
    assert rows[0]["review"]["reason"] == "人工确认"


def test_http_post_training_export_does_not_regress_to_get_case_n_plus_one(tmp_path, monkeypatch):
    db = str(tmp_path / "post-training-http.sqlite")
    app = create_app(db)
    store = app.state.store
    target = {
        "platform": "weibo",
        "handle": "training-http",
        "posts": [{"id": "p1", "text": "今天散步"}],
    }
    case = store.create_case("HTTP 训练导出", "核查", [target])
    state = {
        "goal": "HTTP 精确复核",
        "targets": [target],
        "assets": [],
        "events": [],
        "answer": "更像普通创作者",
        "primary_result": {
            "label": "更像普通创作者",
            "features": FeatureVector().asdict(),
        },
    }
    run = store.create_run(case["id"], state)
    store.update_run(run["id"], state, "completed")
    store.add_review(case["id"], run["id"], "confirm_ordinary", reason="HTTP 人工确认")

    monkeypatch.setattr(
        store,
        "get_case",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP export regressed to per-case loading")
        ),
    )
    try:
        response = TestClient(app).get("/api/post-training/export")
        assert response.status_code == 200
        rows = [json.loads(line) for line in response.text.splitlines()]
        assert len(rows) == 1
        assert rows[0]["run_id"] == run["id"]
        assert rows[0]["review"]["reason"] == "HTTP 人工确认"
    finally:
        app.state.harness.close()
