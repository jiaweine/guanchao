from pathlib import Path

from guanchao.harness import AgentHarness
from guanchao.sample_data import demo_target
from guanchao.store import Store


def test_harness_runs_real_tool_loop(tmp_path: Path):
    store = Store(str(tmp_path / "test.db"))
    case = store.create_case("demo", "判断是否长期营销运营", [demo_target()])
    run = AgentHarness(store).execute_inline(case["id"], "不要草率下结论，判断是否长期营销运营")
    assert run["status"] == "completed"
    state = run["state"]
    assert state["completed_tools"][0] == "workspace.inspect"
    assert "content.scan" in state["completed_tools"]
    assert "pattern.compare" in state["completed_tools"]
    assert state["completed_tools"][-1] == "verdict.compose"
    assert state["primary_result"]["label"]
    assert len(state["events"]) >= 8
    assert state["evidence"]


def test_harness_challenges_uncertain_case(tmp_path: Path):
    store = Store(str(tmp_path / "test.db"))
    target = {
        "platform": "weibo",
        "handle": "mixed",
        "bio": "生活记录，偶尔合作",
        "posts": [
            {"text": "最近用了这个杯子，优点是轻，缺点是盖子难洗。"},
            {"text": "今天散步遇到一场雨，没想到鞋全湿了。"},
            {"text": "品牌合作：这款包今天有优惠，感兴趣可以看看。"},
            {"text": "用了三周之后还是觉得肩带偏硬，不过容量确实大。"},
        ],
    }
    case = store.create_case("mixed", "帮我核查是否误判", [target])
    run = AgentHarness(store).execute_inline(case["id"], "我怕误判，帮我认真核查")
    assert run["status"] == "completed"
    assert "evidence.challenge" in run["state"]["completed_tools"] or run["state"]["primary_result"]["confidence"] >= 0.62
