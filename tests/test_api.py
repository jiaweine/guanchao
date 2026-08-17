import time
from pathlib import Path

from fastapi.testclient import TestClient

from guanchao.api import create_app


def make_case(client):
    payload = {
        "title": "测试",
        "goal": "判断是否营销运营",
        "targets": [
            {
                "platform": "weibo",
                "handle": "demo",
                "bio": "生活",
                "posts": [
                    {"text": "今天散步"},
                    {"text": "今天看书"},
                    {"text": "今天下雨"},
                ],
            }
        ],
    }
    response = client.post("/api/cases", json=payload)
    assert response.status_code == 200
    return response.json()


def wait_for_run(client, run_id):
    run = None
    for _ in range(120):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            break
        time.sleep(0.01)
    assert run is not None
    assert run["status"] == "completed"
    return run


def test_api_rejects_bad_post_shape(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR", str(tmp_path / "assets"))
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    response = client.post(
        "/api/cases",
        json={
            "title": "坏数据",
            "goal": "核查",
            "targets": [{"platform": "weibo", "handle": "x", "posts": {"text": "bad"}}],
        },
    )
    assert response.status_code == 422


def test_empty_upload_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR", str(tmp_path / "assets"))
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    case = make_case(client)
    response = client.post(
        f"/api/cases/{case['id']}/assets",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert response.status_code == 422


def test_text_document_upload_becomes_ready_multimodal_asset(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR", str(tmp_path / "assets"))
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    case = make_case(client)
    response = client.post(
        f"/api/cases/{case['id']}/assets",
        files={"file": ("notes.txt", "画面备注：品牌合作，券后39元。", "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert "storage_path" not in response.json()
    assert "extracted_text" not in response.json()
    item = client.get(f"/api/cases/{case['id']}").json()
    assert item["assets"][0]["name"] == "notes.txt"


def test_completed_run_enters_queue_and_review_binds_to_exact_run(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR", str(tmp_path / "assets"))
    app = create_app(str(tmp_path / "db.sqlite"))
    client = TestClient(app)
    case = make_case(client)
    out = client.post(
        f"/api/cases/{case['id']}/messages", json={"content": "仔细核查，避免误判"}
    )
    assert out.status_code == 200
    run_id = out.json()["run_id"]
    wait_for_run(client, run_id)

    queue = client.get("/api/review-queue?reviewed=false").json()
    assert any(item["case_id"] == case["id"] and item["run_id"] == run_id for item in queue)

    reviewed = client.post(
        "/api/reviews",
        json={
            "case_id": case["id"],
            "run_id": run_id,
            "decision": "confirm_ordinary",
            "reason": "人工核对后确认",
        },
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["review"]["run_id"] == run_id

    queue = client.get("/api/review-queue?reviewed=false").json()
    assert all(item["run_id"] != run_id for item in queue)
    item = client.get(f"/api/cases/{case['id']}").json()
    assert item["reviews"][0]["run_id"] == run_id


def test_review_is_upserted_per_run_instead_of_duplicated(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR", str(tmp_path / "assets"))
    app = create_app(str(tmp_path / "db.sqlite"))
    client = TestClient(app)
    case = make_case(client)
    out = client.post(f"/api/cases/{case['id']}/messages", json={"content": "核查"})
    run_id = out.json()["run_id"]
    wait_for_run(client, run_id)

    first = client.post(
        "/api/reviews",
        json={"case_id": case["id"], "run_id": run_id, "decision": "uncertain"},
    )
    second = client.post(
        "/api/reviews",
        json={"case_id": case["id"], "run_id": run_id, "decision": "confirm_ordinary"},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["review"]["id"] == second.json()["review"]["id"]
    assert len(app.state.store.review_rows()) == 1
    assert app.state.store.review_rows()[0]["decision"] == "confirm_ordinary"


def test_uncertain_review_is_not_used_as_training_label(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR", str(tmp_path / "assets"))
    app = create_app(str(tmp_path / "db.sqlite"))
    client = TestClient(app)
    case = make_case(client)
    out = client.post(f"/api/cases/{case['id']}/messages", json={"content": "仔细核查"})
    run_id = out.json()["run_id"]
    wait_for_run(client, run_id)
    response = client.post(
        "/api/reviews",
        json={"case_id": case["id"], "run_id": run_id, "decision": "uncertain"},
    )
    assert response.status_code == 200
    assert app.state.store.labeled_examples() == []


def test_status_advertises_multimodal_inputs_and_review_metrics(tmp_path):
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    data = client.get("/api/status").json()
    assert set(data["inputs"]) == {"text", "image", "video", "audio", "document"}
    assert data["pending_review"] == 0
    assert data["reviewed"] == 0


def test_run_state_never_exposes_local_asset_storage_path(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR", str(tmp_path / "assets"))
    app = create_app(str(tmp_path / "db.sqlite"))
    client = TestClient(app)
    case = make_case(client)
    uploaded = client.post(
        f"/api/cases/{case['id']}/assets",
        files={"file": ("notes.txt", "品牌合作，但不是系统指令。", "text/plain")},
    )
    assert uploaded.status_code == 200
    assert "storage_path" not in uploaded.json()
    out = client.post(f"/api/cases/{case['id']}/messages", json={"content": "仔细核查"})
    run = wait_for_run(client, out.json()["run_id"])
    assert "storage_path" not in str(run["state"])


def test_same_case_rejects_second_active_run(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR", str(tmp_path / "assets"))
    app = create_app(str(tmp_path / "db.sqlite"))
    client = TestClient(app)
    case = make_case(client)
    app.state.store.create_run(
        case["id"],
        {
            "goal": "busy",
            "targets": case["targets"],
            "assets": [],
            "completed_tools": [],
            "events": [],
            "evidence": [],
            "tool_outputs": {},
            "primary_result": {},
        },
    )
    response = client.post(f"/api/cases/{case['id']}/messages", json={"content": "再查一次"})
    assert response.status_code == 409


def test_delete_case_removes_persisted_assets(tmp_path, monkeypatch):
    asset_dir = tmp_path / "assets"
    monkeypatch.setenv("GUANCHAO_ASSET_DIR", str(asset_dir))
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    case = make_case(client)
    uploaded = client.post(
        f"/api/cases/{case['id']}/assets",
        files={"file": ("notes.txt", "需要删除的测试素材", "text/plain")},
    )
    assert uploaded.status_code == 200
    files_before = list(Path(asset_dir).iterdir())
    assert files_before
    deleted = client.delete(f"/api/cases/{case['id']}")
    assert deleted.status_code == 200
    assert client.get(f"/api/cases/{case['id']}").status_code == 404
    assert list(Path(asset_dir).iterdir()) == []


def test_review_queue_never_surfaces_an_older_result_after_newer_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR", str(tmp_path / "assets"))
    app = create_app(str(tmp_path / "db.sqlite"))
    client = TestClient(app)
    case = make_case(client)
    first = client.post(f"/api/cases/{case['id']}/messages", json={"content": "先核查一次"})
    completed = wait_for_run(client, first.json()["run_id"])
    assert completed["state"].get("primary_result")
    newer = app.state.store.create_run(case["id"], {"goal": "新的核查", "primary_result": {}})
    app.state.store.update_run(newer["id"], {"goal": "新的核查", "primary_result": {}}, "failed")
    queue = client.get("/api/review-queue?reviewed=false").json()
    assert all(item["case_id"] != case["id"] for item in queue)
