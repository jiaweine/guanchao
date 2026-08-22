import json

from fastapi.testclient import TestClient

from guanchao.api import create_app
from guanchao.domain import FeatureVector
from guanchao.store import Store


def _target(handle: str) -> dict:
    return {
        "platform": "weibo",
        "handle": handle,
        "bio": "生活记录",
        "posts": [{"id": f"{handle}-p1", "text": "今天散步"}],
    }


def _reviewable_state(label: str = "更像普通创作者") -> dict:
    return {
        "goal": "核查",
        "targets": [_target("reviewable")],
        "assets": [],
        "primary_result": {
            "label": label,
            "marketing_likelihood": 0.2 if label == "更像普通创作者" else 0.8,
            "covert_promotion_risk": 0.1,
            "confidence": 0.8,
            "stability": 0.9,
            "missing": [],
            "features": FeatureVector().asdict(),
        },
        "trajectory": [],
    }


def test_malformed_persisted_json_degrades_to_safe_defaults(tmp_path):
    store = Store(str(tmp_path / "corrupt.sqlite"))
    case = store.create_case("坏状态恢复", "核查", [_target("corrupt")])
    run = store.create_run(case["id"], _reviewable_state())

    conn = store._connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value_json, updated_at) VALUES('workspace', ?, 'now')",
            ("{broken",),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value_json, updated_at) VALUES('calibration', ?, 'now')",
            ("not-json",),
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value_json, updated_at) VALUES('policy_profile', ?, 'now')",
            ("[]",),
        )
        conn.execute("UPDATE runs SET state_json = ? WHERE id = ?", ("{bad-run", run["id"]))
        conn.execute("UPDATE cases SET tags_json = ? WHERE id = ?", ("{bad-tags", case["id"]))
        conn.commit()
    finally:
        store._close(conn)

    assert store.workspace_settings() == {"retention_days": 0}
    assert store.get_calibration().decision_threshold == 0.5
    assert store.get_policy_profile().steps == 0

    listed = store.list_cases(status="all")
    assert len(listed) == 1
    assert listed[0]["tags"] == []
    assert listed[0]["latest_result"] is None
    assert store.get_run(run["id"])["state"] == {}
    assert store.product_metrics()["cases"] == 1


def test_case_api_limit_is_pushed_down_without_cross_limit_cache_pollution(tmp_path):
    app = create_app(str(tmp_path / "limits.sqlite"))
    store = app.state.store
    conn = store._connect()
    try:
        rows = []
        for index in range(80):
            stamp = f"2026-08-22T10:{index % 60:02d}:00+00:00"
            rows.append(
                (
                    f"bulk-{index:04d}",
                    f"批量任务 {index}",
                    "核查",
                    json.dumps([_target(f"bulk-{index}")], ensure_ascii=False),
                    stamp,
                    stamp,
                    "open",
                    "normal",
                    "local",
                    "[]",
                    0,
                    168,
                    None,
                    None,
                    None,
                )
            )
        conn.executemany(
            """
            INSERT INTO cases(
                id, title, goal, targets_json, created_at, updated_at, status, priority, owner,
                tags_json, monitoring_enabled, monitoring_interval_hours, next_check_at,
                last_source_refresh_at, batch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        store._close(conn)

    try:
        client = TestClient(app)
        small = client.get("/api/cases?status=all&limit=7")
        large = client.get("/api/cases?status=all&limit=31")
        small_again = client.get("/api/cases?status=all&limit=7")
        assert small.status_code == large.status_code == small_again.status_code == 200
        assert len(small.json()) == 7
        assert len(large.json()) == 31
        assert small_again.json() == small.json()
    finally:
        app.state.harness.close()


def test_metrics_use_constant_connection_count_instead_of_per_review_n_plus_one(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "metrics.sqlite"))
    for index in range(25):
        case = store.create_case(f"指标 {index}", "核查", [_target(f"m-{index}")])
        run = store.create_run(case["id"], _reviewable_state())
        store.update_run(run["id"], _reviewable_state(), "completed")
        store.add_review(
            case["id"],
            run["id"],
            "confirm_ordinary",
            reviewer="local",
        )

    store._metrics_cache = None
    calls = 0
    original_connect = store._connect

    def counted_connect():
        nonlocal calls
        calls += 1
        return original_connect()

    monkeypatch.setattr(store, "_connect", counted_connect)
    metrics = store.product_metrics()
    assert metrics["reviewed"] == 25
    assert metrics["acceptance_rate"] == 1.0
    # One Store connection should be enough for the batched metrics query set.
    # Keep a little slack so a future harmless health query does not make this brittle.
    assert calls <= 2


def test_verified_last_7_days_excludes_uncertain_reviews(tmp_path):
    store = Store(str(tmp_path / "verified.sqlite"))

    decisive_case = store.create_case("明确复核", "核查", [_target("decisive")])
    decisive_run = store.create_run(decisive_case["id"], _reviewable_state())
    store.update_run(decisive_run["id"], _reviewable_state(), "completed")
    store.add_review(
        decisive_case["id"],
        decisive_run["id"],
        "confirm_ordinary",
        reviewer="local",
    )

    uncertain_case = store.create_case("不确定复核", "核查", [_target("uncertain")])
    uncertain_run = store.create_run(uncertain_case["id"], _reviewable_state())
    store.update_run(uncertain_run["id"], _reviewable_state(), "completed")
    store.add_review(
        uncertain_case["id"],
        uncertain_run["id"],
        "uncertain",
        reviewer="local",
    )

    store._metrics_cache = None
    metrics = store.product_metrics()
    assert metrics["reviewed"] == 2
    assert metrics["uncertain_rate"] == 0.5
    assert metrics["verified_last_7_days"] == 1
