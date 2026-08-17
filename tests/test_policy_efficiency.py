from statistics import fmean

from guanchao.harness import AgentHarness
from guanchao.store import Store


def _account(handle: str) -> dict:
    return {
        "platform": "weibo",
        "handle": handle,
        "display_name": handle,
        "bio": "品牌合作会明确标注，日常分享真实体验",
        "posts": [
            {
                "id": f"{handle}-{index}",
                "text": (
                    "合作体验会标注，实际使用后把优点和不足都记录下来。"
                    if index % 3 == 0
                    else "最近很多人问同款，入口放在主页置顶，也会回复使用感受。"
                ),
                "created_at": f"2026-07-{index + 1:02d}T08:00:00+00:00",
            }
            for index in range(9)
        ],
    }


def test_harness_experience_replay_reduces_redundant_decisions(tmp_path):
    store = Store(str(tmp_path / "efficiency.sqlite"))
    harness = AgentHarness(store)
    counts: list[int] = []

    for index in range(10):
        case = store.create_case(
            f"效率回归 {index}",
            "仔细核查营销倾向，证据足够时形成判断，不做无收益重复核查",
            [_account(f"efficiency-{index}")],
        )
        run_id = harness.start(case["id"], case["goal"])
        harness.wait(run_id, 10)
        harness.wait_learning(10)
        run = store.get_run(run_id)
        assert run["status"] == "completed"
        assert run["state"].get("answer")
        counts.append(int(run["state"]["decision_count"]))

    cold = fmean(counts[:3])
    learned = fmean(counts[-3:])
    reduction = 1.0 - learned / cold
    print(
        "HARNESS_SELF_EVOLUTION_EFFICIENCY "
        f"counts={counts} cold_mean={cold:.3f} learned_mean={learned:.3f} "
        f"reduction={reduction:.3%}"
    )
    assert learned <= cold * .8, counts
