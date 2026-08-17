import struct
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


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


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

    listed = next(
        item
        for item in client.get("/api/cases?status=all").json()
        if item["id"] == case["id"]
    )
    assert "posts" not in listed["targets"][0]
    assert "features" not in (listed["latest_result"] or {})
    assert "evidence" not in (listed["latest_result"] or {})

    queued = next(
        item
        for item in client.get("/api/review-queue?reviewed=false").json()
        if item["case_id"] == case["id"]
    )
    assert "posts" not in queued["targets"][0]
    assert "features" not in queued["result"]
    assert "evidence" not in queued["result"]

    detail = client.get(f"/api/cases/{case['id']}").json()
    assert detail["targets"][0]["posts"]
    assert detail["runs"][0]["state"]["primary_result"]["features"]


def test_readme_uses_portable_markdown_images_and_self_evolution_math():
    text = Path("README.md").read_text(encoding="utf-8")
    png_images = [
        "docs/product-preview.png",
        "docs/product-batch.png",
        "docs/product-review-queue.png",
        "docs/product-evidence.png",
    ]
    svg_sources = [
        "docs/product-preview.svg",
        "docs/product-batch.svg",
        "docs/product-review-queue.svg",
        "docs/product-evidence.svg",
    ]

    assert "<img" not in text.lower()
    assert "<table" not in text.lower()
    assert 'width="' not in text.lower()

    for image in png_images:
        assert f"](./{image})" in text
        path = Path(image)
        assert path.is_file()
        width, height = _png_dimensions(path)
        assert width >= 2400
        assert height >= 1400

    for source in svg_sources:
        assert Path(source).is_file()

    assert "\\operatorname" not in text
    assert "Harness 自进化" in text
    assert "Gödel Agent" in text
    assert "Contextual Experience Replay" in text
    assert "Direct Preference Optimization" in text
    assert "Feel-Good Thompson Sampling" in text
    assert "Distributionally Robust Policy" in text

    for formula_token in [
        "P_{\\mathrm{mkt}}",
        "x^\\top\\theta_a",
        "P_a x",
        "\\mathcal L_{\\mathrm{pref}}",
        "S_{\\mathrm{stab}}",
        "P_{\\mathrm{covert}}",
        "\\mathrm{Brier}",
        "\\mathrm{ECE}",
        "\\min_k",
    ]:
        assert formula_token in text

    assert "0.004" not in text
    assert "-0.015" not in text
    assert "challenge_confidence" not in text

    for flow_token in ["▼", "→", "├─", "└─"]:
        assert flow_token not in text
