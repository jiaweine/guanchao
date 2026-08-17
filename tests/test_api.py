from fastapi.testclient import TestClient

from guanchao.api import create_app
from guanchao.sample_data import demo_target


def test_api_case_run_and_feedback(tmp_path):
    app = create_app(str(tmp_path / "api.db"))
    client = TestClient(app)
    created = client.post(
        "/api/cases",
        json={"title": "demo", "goal": "判断是否营销运营", "targets": [demo_target()]},
    )
    assert created.status_code == 200
    case_id = created.json()["id"]
    sent = client.post(f"/api/cases/{case_id}/messages", json={"content": "请仔细核查"})
    assert sent.status_code == 200
    run_id = sent.json()["run_id"]
    app.state.harness.wait(run_id, timeout=5)
    run = client.get(f"/api/runs/{run_id}")
    assert run.status_code == 200
    assert run.json()["status"] == "completed"
    fb = client.post("/api/feedback", json={"case_id": case_id, "label": 1, "note": "人工复核"})
    assert fb.status_code == 200


def test_invalid_case_is_rejected(tmp_path):
    app = create_app(str(tmp_path / "api.db"))
    client = TestClient(app)
    res = client.post("/api/cases/does-not-exist/messages", json={"content": "test"})
    assert res.status_code == 404
