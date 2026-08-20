import pytest

from guanchao.harness import AgentHarness
from guanchao.policy import OwnedPolicy, PolicyProfile
from guanchao.sample_data import demo_target
from guanchao.store import Store


def test_policy_boundary_tracks_learned_decision_threshold():
    state = {
        "targets": [demo_target()],
        "assets": [],
        "sample_size": 8,
        "primary_result": {
            "marketing_likelihood": 0.72,
            "confidence": 0.8,
            "stability": 0.9,
        },
        "evidence": [{"key": "commercial_language", "direction": "supports"}],
    }
    default_boundary = OwnedPolicy(PolicyProfile(), decision_threshold=0.5).features(
        "仔细核查", state, "evidence.challenge"
    )[2]
    learned_boundary = OwnedPolicy(PolicyProfile(), decision_threshold=0.7).features(
        "仔细核查", state, "evidence.challenge"
    )[2]

    assert learned_boundary > default_boundary
    assert learned_boundary > 0.95


def test_submit_failure_does_not_leave_case_permanently_running(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "submit-failure.sqlite"))
    harness = AgentHarness(store)
    case = store.create_case("提交失败恢复", "仔细核查", [demo_target()])
    original_submit = harness._submit

    def fail_submit(run_id):
        raise RuntimeError("executor unavailable")

    monkeypatch.setattr(harness, "_submit", fail_submit)
    with pytest.raises(RuntimeError, match="executor unavailable"):
        harness.start(case["id"], case["goal"])

    assert store.active_run_for_case(case["id"]) is None

    monkeypatch.setattr(harness, "_submit", original_submit)
    run_id = harness.start(case["id"], case["goal"])
    harness.wait(run_id, 10)
    assert store.get_run(run_id)["status"] == "completed"
