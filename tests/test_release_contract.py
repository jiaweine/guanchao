import time
from pathlib import Path

from fastapi.testclient import TestClient

from guanchao.api import create_app


def _account(handle: str) -> dict:
    return {
        "platform": "weibo",
        "handle": handle,
        "display_name": handle,
        "bio": "商务合作 私信",
        "posts": [
            {"id": f"p{i}", "text": f"品牌合作 新品推荐 券后39元 评论区领取优惠 {i}"}
            for i in range(6)
        ],
    }


def _wait_run(client: TestClient, run_id: str) -> dict:
    for _ in range(160):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


def test_list_and_review_queue_use_compact_summaries(tmp_path):
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    case = client.post(
        "/api/cases",
        json={"title": "轻量列表", "goal": "核查", "targets": [_account("compact")]},
    ).json()
    run_id = client.post(
        f"/api/cases/{case['id']}/messages", json={"content": "仔细核查"}
    ).json()["run_id"]
    run = _wait_run(client, run_id)
    assert run["status"] == "completed"

    listed = next(item for item in client.get("/api/cases?status=all").json() if item["id"] == case["id"])
    assert "posts" not in listed["targets"][0]
    assert "features" not in (listed["latest_result"] or {})
    assert "evidence" not in (listed["latest_result"] or {})

    queued = next(item for item in client.get("/api/review-queue?reviewed=false").json() if item["case_id"] == case["id"])
    assert "posts" not in queued["targets"][0]
    assert "features" not in queued["result"]
    assert "evidence" not in queued["result"]

    detail = client.get(f"/api/cases/{case['id']}").json()
    assert detail["targets"][0]["posts"]
    assert detail["runs"][0]["state"]["primary_result"]["features"]


def test_readme_presents_product_images_and_owned_algorithm_math_without_arrow_flowchart():
    text = Path("README.md").read_text(encoding="utf-8")
    for image in [
        "docs/product-preview.svg",
        "docs/product-batch.svg",
        "docs/product-review-queue.svg",
        "docs/product-evidence.svg",
    ]:
        assert image in text
    for formula_token in [
        "P_{\\mathrm{mkt}}",
        "S_{\\mathrm{stab}}",
        "U_{\\mathrm{challenge}}",
        "\\mathrm{Brier}",
        "0.004",
        "-0.015",
    ]:
        assert formula_token in text
    for flow_token in ["▼", "→", "├─", "└─"]:
        assert flow_token not in text
