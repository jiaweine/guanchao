import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from guanchao.api import create_app


def account(handle: str, platform: str = "weibo", commercial: bool = False):
    posts = (
        [
            {"text": "品牌合作 限时优惠 点击购买"},
            {"text": "同款链接 私信领取优惠"},
            {"text": "今天最后一天 券后到手"},
        ]
        if commercial
        else [
            {"text": "今天散步看到了晚霞"},
            {"text": "周末做了一顿饭"},
            {"text": "记录最近读完的书"},
        ]
    )
    return {"platform": platform, "handle": handle, "display_name": handle, "bio": "生活记录", "posts": posts}


def wait_run(client: TestClient, run_id: str):
    result = None
    for _ in range(200):
        result = client.get(f"/api/runs/{run_id}").json()
        if result["status"] != "running":
            break
        time.sleep(0.01)
    assert result and result["status"] == "completed"
    return result


def test_batch_import_creates_independent_cases_and_can_auto_start(tmp_path):
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    response = client.post(
        "/api/cases/batch",
        json={
            "title": "本周账号筛查",
            "goal": "判断是否长期营销运营",
            "targets": [account("a"), account("b", commercial=True)],
            "auto_start": True,
            "priority": "high",
            "tags": ["本周", "重点"],
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["batch"]["count"] == 2
    assert len(data["cases"]) == 2
    assert len(data["runs"]) == 2
    for item in data["runs"]:
        wait_run(client, item["run_id"])
    cases = client.get(f"/api/cases?batch_id={data['batch']['id']}").json()
    assert len(cases) == 2
    assert all(case["priority"] == "high" for case in cases)


def test_case_search_filter_archive_and_owner_assignment(tmp_path):
    app = create_app(str(tmp_path / "db.sqlite"))
    client = TestClient(app)
    member = client.post(
        "/api/members",
        json={"id": "alice", "display_name": "Alice", "role": "analyst"},
    )
    assert member.status_code == 200
    created = client.post(
        "/api/cases",
        json={
            "title": "重点账号",
            "goal": "核查",
            "targets": [account("needle", "douyin")],
            "owner": "alice",
            "priority": "high",
            "tags": ["品牌安全"],
        },
    ).json()
    result = client.get("/api/cases?query=needle&platform=douyin&owner=alice&priority=high").json()
    assert [item["id"] for item in result] == [created["id"]]
    archived = client.patch(f"/api/cases/{created['id']}", json={"archived": True}).json()
    assert archived["status"] == "archived"
    assert client.get("/api/cases").json() == []
    archived_list = client.get("/api/cases?status=archived").json()
    assert archived_list[0]["id"] == created["id"]


def test_monitoring_watchlist_only_marks_due_after_refresh_window(tmp_path):
    app = create_app(str(tmp_path / "db.sqlite"))
    client = TestClient(app)
    created = client.post(
        "/api/cases",
        json={"title": "监测账号", "goal": "持续观察", "targets": [account("watch")]},
    ).json()
    client.patch(
        f"/api/cases/{created['id']}",
        json={"monitoring_enabled": True, "monitoring_interval_hours": 24},
    )
    assert client.get("/api/monitoring?due_only=true").json() == []
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    conn = app.state.store._connect()
    conn.execute("UPDATE cases SET next_check_at = ? WHERE id = ?", (past, created["id"]))
    conn.commit()
    app.state.store._close(conn)
    due = client.get("/api/monitoring?due_only=true").json()
    assert due[0]["id"] == created["id"]
    refreshed = client.patch(
        f"/api/cases/{created['id']}/target",
        json={"target": account("watch", commercial=True), "rerun": False},
    ).json()["case"]
    assert refreshed["last_source_refresh_at"]
    assert not refreshed["monitoring_due"]


def test_report_export_contains_latest_judgement_and_review(tmp_path):
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    case = client.post(
        "/api/cases",
        json={"title": "报告测试", "goal": "仔细核查", "targets": [account("report", commercial=True)]},
    ).json()
    run_id = client.post(f"/api/cases/{case['id']}/messages", json={"content": "仔细核查"}).json()["run_id"]
    wait_run(client, run_id)
    client.post(
        "/api/reviews",
        json={"case_id": case["id"], "run_id": run_id, "decision": "confirm_marketing", "reason": "人工确认"},
    )
    markdown = client.get(f"/api/cases/{case['id']}/report?output=markdown")
    assert markdown.status_code == 200
    assert "## 当前判断" in markdown.text and "## 人工复核" in markdown.text and "人工确认" in markdown.text
    payload = client.get(f"/api/cases/{case['id']}/report?output=json").json()
    assert payload["run_id"] == run_id
    assert payload["review"]["decision"] == "confirm_marketing"


def test_roles_and_audit_preserve_accountability(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_TRUST_ACTOR_HEADER", "1")
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    client.post("/api/members", json={"id": "reviewer", "display_name": "复核员", "role": "reviewer"})
    denied = client.post(
        "/api/cases",
        headers={"X-Guanchao-Actor": "reviewer"},
        json={"title": "不应创建", "goal": "核查", "targets": [account("x")]},
    )
    assert denied.status_code == 403
    case = client.post(
        "/api/cases",
        json={"title": "审计测试", "goal": "核查", "targets": [account("audit")]},
    ).json()
    client.post(
        "/api/events",
        headers={"X-Guanchao-Actor": "reviewer"},
        json={"event_type": "case_opened", "case_id": case["id"]},
    )
    events = client.get(f"/api/audit?case_id={case['id']}").json()
    assert any(item["event_type"] == "case_opened" and item["actor"] == "reviewer" for item in events)


def test_product_metrics_include_review_quality_and_flow(tmp_path):
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    case = client.post(
        "/api/cases",
        json={"title": "指标", "goal": "核查", "targets": [account("metric", commercial=True)]},
    ).json()
    run_id = client.post(f"/api/cases/{case['id']}/messages", json={"content": "仔细核查"}).json()["run_id"]
    wait_run(client, run_id)
    client.post("/api/events", json={"event_type": "case_opened", "case_id": case["id"], "run_id": run_id})
    client.post(
        "/api/reviews",
        json={"case_id": case["id"], "run_id": run_id, "decision": "confirm_marketing"},
    )
    metrics = client.get("/api/metrics").json()
    assert metrics["reviewed"] == 1
    assert metrics["verified_last_7_days"] == 1
    assert "acceptance_rate" in metrics and "overturn_rate" in metrics
    assert "evidence_sufficiency_rate" in metrics
    assert "median_time_to_review_seconds" in metrics


def test_retention_only_purges_archived_cases(tmp_path):
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    old = client.post(
        "/api/cases",
        json={"title": "归档", "goal": "核查", "targets": [account("old")]},
    ).json()
    live = client.post(
        "/api/cases",
        json={"title": "保留", "goal": "核查", "targets": [account("live")]},
    ).json()
    client.patch(f"/api/cases/{old['id']}", json={"archived": True})
    client.put("/api/workspace/settings", json={"retention_days": 1})
    conn = client.app.state.store._connect()
    past = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (past, old["id"]))
    conn.commit()
    client.app.state.store._close(conn)
    purged = client.post("/api/workspace/purge").json()
    assert purged["deleted"] == 1
    assert client.get(f"/api/cases/{old['id']}").status_code == 404
    assert client.get(f"/api/cases/{live['id']}").status_code == 200


def test_reviewer_cannot_be_assigned_as_case_owner_and_deactivation_reassigns_work(tmp_path):
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    client.post("/api/members", json={"id": "alice", "display_name": "Alice", "role": "analyst"})
    client.post("/api/members", json={"id": "reviewer_only", "display_name": "Reviewer", "role": "reviewer"})
    denied = client.post(
        "/api/cases",
        json={"title": "错误负责人", "goal": "核查", "targets": [account("bad-owner")], "owner": "reviewer_only"},
    )
    assert denied.status_code == 422
    created = client.post(
        "/api/cases",
        json={"title": "待转交", "goal": "核查", "targets": [account("handoff")], "owner": "alice"},
    ).json()
    removed = client.delete("/api/members/alice")
    assert removed.status_code == 200
    assert client.get(f"/api/cases/{created['id']}").json()["owner"] == "local"


def test_report_never_falls_back_to_stale_result_after_newer_failed_run(tmp_path):
    app = create_app(str(tmp_path / "db.sqlite"))
    client = TestClient(app)
    case = client.post(
        "/api/cases",
        json={"title": "报告时效", "goal": "核查", "targets": [account("freshness", commercial=True)]},
    ).json()
    run_id = client.post(f"/api/cases/{case['id']}/messages", json={"content": "核查"}).json()["run_id"]
    wait_run(client, run_id)
    stale_state = {"primary_result": {}, "events": [], "goal": "newer"}
    newer = app.state.store.create_run(case["id"], stale_state)
    app.state.store.update_run(newer["id"], stale_state, "failed")
    payload = client.get(f"/api/cases/{case['id']}/report?output=json").json()
    assert payload["run_id"] is None
    assert payload["judgement"]["label"] is None


def test_batch_capacity_is_bounded_without_losing_imported_cases(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_MAX_WORKERS", "2")
    monkeypatch.setenv("GUANCHAO_MAX_INFLIGHT", "2")
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    response = client.post(
        "/api/cases/batch",
        json={
            "title": "容量边界",
            "goal": "核查",
            "targets": [account("c1"), account("c2"), account("c3")],
            "auto_start": True,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["capacity_limited"] is True
    assert data["runs"] == []
    assert len(data["cases"]) == 3
    assert len(client.get("/api/cases?status=open").json()) == 3


def test_browser_identity_header_is_ignored_unless_trusted_proxy_mode_is_enabled(tmp_path, monkeypatch):
    client = TestClient(create_app(str(tmp_path / "plain.sqlite")))
    client.post("/api/members", json={"id": "reviewer", "display_name": "复核员", "role": "reviewer"})
    session = client.get("/api/session", headers={"X-Guanchao-Actor": "reviewer"}).json()
    assert session["id"] == "local"

    monkeypatch.setenv("GUANCHAO_TRUST_ACTOR_HEADER", "1")
    trusted = TestClient(create_app(str(tmp_path / "trusted.sqlite")))
    trusted.post("/api/members", json={"id": "reviewer", "display_name": "复核员", "role": "reviewer"})
    session = trusted.get("/api/session", headers={"X-Guanchao-Actor": "reviewer"}).json()
    assert session["id"] == "reviewer" and session["role"] == "reviewer"
    denied = trusted.post(
        "/api/cases",
        headers={"X-Guanchao-Actor": "reviewer"},
        json={"title": "不能创建", "goal": "核查", "targets": [account("blocked")]},
    )
    assert denied.status_code == 403


def test_case_comments_are_collaborative_notes_not_agent_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("GUANCHAO_TRUST_ACTOR_HEADER", "1")
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    client.post("/api/members", json={"id": "reviewer", "display_name": "复核员", "role": "reviewer"})
    case = client.post(
        "/api/cases",
        json={"title": "协作备注", "goal": "核查", "targets": [account("notes")]},
    ).json()
    added = client.post(
        f"/api/cases/{case['id']}/comments",
        headers={"X-Guanchao-Actor": "reviewer"},
        json={"content": "请重点看第三条内容，品牌露出可能是活动现场。"},
    )
    assert added.status_code == 200
    snapshot = client.get(f"/api/cases/{case['id']}").json()
    assert snapshot["comments"][0]["author"] == "reviewer"
    assert "第三条内容" in snapshot["comments"][0]["content"]
    assert snapshot["messages"] == []
    events = client.get(f"/api/audit?case_id={case['id']}").json()
    assert any(event["event_type"] == "comment_added" and event["actor"] == "reviewer" for event in events)


def test_business_metrics_are_not_exposed_by_public_health_endpoint(tmp_path):
    client = TestClient(create_app(str(tmp_path / "db.sqlite")))
    health = client.get("/healthz")
    assert health.status_code == 200 and health.json() == {"ok": True}
    status = client.get("/api/status")
    assert status.status_code == 200 and "cases" in status.json()
