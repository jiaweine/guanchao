from guanchao.harness import AgentHarness
from guanchao.policy import OwnedPolicy, PolicyProfile
from guanchao.store import Store


def _account(handle: str) -> dict:
    return {
        "platform": "weibo",
        "handle": handle,
        "display_name": handle,
        "bio": "日常分享，也接受品牌合作，合作会明确标注",
        "posts": [
            {
                "id": f"{handle}-{index}",
                "text": (
                    "这周实际用了新品，优缺点都记录一下；合作内容会标注。"
                    if index % 3 == 0
                    else "新品体验，想了解同款可以看主页置顶，评论区也会回复。"
                ),
                "created_at": f"2026-08-{index + 1:02d}T08:00:00+00:00",
            }
            for index in range(8)
        ],
    }


def _ready_state() -> dict:
    return {
        "goal": "仔细核查，避免误判",
        "targets": [_account("policy")],
        "assets": [],
        "sample_size": 8,
        "completed_tools": [
            "workspace.inspect",
            "content.scan",
            "profile.read",
            "pattern.compare",
            "stability.probe",
            "peer.compare",
        ],
        "primary_result": {
            "marketing_likelihood": .84,
            "confidence": .91,
            "stability": .93,
        },
        "evidence": [
            {"key": "commercial_language", "direction": "supports"},
            {"key": "disclosure_signal", "direction": "against"},
            {"key": "authentic_variation", "direction": "against"},
        ],
    }


def test_contextual_preference_learning_can_promote_early_verdict():
    profile = PolicyProfile()
    policy = OwnedPolicy(profile)
    state = _ready_state()
    verdict_x = policy.features(state["goal"], state, "verdict.compose")
    challenge_x = policy.features(state["goal"], state, "evidence.challenge")

    for _ in range(12):
        profile.observe(
            [
                {
                    "action": "verdict.compose",
                    "features": verdict_x,
                    "alternative": "evidence.challenge",
                    "alternative_features": challenge_x,
                    "reward": 1.0,
                    "duration_ms": 1.0,
                }
            ]
        )

    learned = OwnedPolicy(profile).decide(state["goal"], state)
    assert learned is not None and learned.tool == "verdict.compose"
    assert profile.steps == 12
    assert profile.experiences
    assert any(abs(value) > 0 for value in profile.weights["verdict.compose"])
    assert PolicyProfile.from_dict(profile.to_dict()) == profile


def test_harness_replays_its_own_completed_trajectory(tmp_path):
    store = Store(str(tmp_path / "self-evolution.sqlite"))
    harness = AgentHarness(store)
    case = store.create_case("自进化轨迹", "仔细核查这个账号，避免误判", [_account("learn-run")])

    run_id = harness.start(case["id"], case["goal"])
    harness.wait(run_id, 10)
    harness.wait_learning(10)
    run = store.get_run(run_id)

    assert run["status"] == "completed"
    trajectory = run["state"].get("trajectory") or []
    assert trajectory
    profile = store.get_policy_profile()
    assert profile.steps == len(trajectory)
    assert profile.latency_count
    assert profile.experiences
    assert any(
        item["event_type"] == "harness_experience_replayed"
        for item in store.audit_events(limit=100)
    )


def test_human_review_is_delayed_feedback_for_harness(tmp_path):
    store = Store(str(tmp_path / "review-learning.sqlite"))
    harness = AgentHarness(store)
    case = store.create_case("人工反馈", "调查营销倾向并给出证据", [_account("review-run")])
    run_id = harness.start(case["id"], case["goal"])
    harness.wait(run_id, 10)
    harness.wait_learning(10)
    run = store.get_run(run_id)
    result = run["state"]["primary_result"]
    threshold = store.get_calibration().decision_threshold
    decision = (
        "confirm_marketing"
        if float(result["marketing_likelihood"]) >= threshold
        else "confirm_ordinary"
    )
    store.add_review(case["id"], run_id, decision)

    before = store.get_policy_profile().reviews
    harness.observe_review(run_id, decision)
    harness.wait_learning(10)
    after = store.get_policy_profile().reviews

    assert after == before + 1
    assert any(
        item["event_type"] == "harness_self_evolved"
        for item in store.audit_events(limit=100)
    )
