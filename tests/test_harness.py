import tempfile
from guanchao.harness import AgentHarness
from guanchao.sample_data import demo_target
from guanchao.store import Store


def test_harness_executes_real_tool_loop():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store=Store(f.name); case=store.create_case("demo","仔细核查是否营销运营",[demo_target()]); run=AgentHarness(store).execute_inline(case["id"],case["goal"])
        assert run["status"]=="completed"
        completed=run["state"]["completed_tools"]
        assert "content.scan" in completed and "verdict.compose" in completed
        assert any(x in completed for x in ("stability.probe","evidence.challenge"))
        assert run["state"]["primary_result"]["label"]


def test_evidence_is_deduplicated_across_tools():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        store=Store(f.name); case=store.create_case("demo","仔细核查",[demo_target()]); run=AgentHarness(store).execute_inline(case["id"],"仔细核查")
        keys=[(e["key"],e["direction"],tuple(e.get("post_ids") or []),tuple(e.get("asset_ids") or [])) for e in run["state"]["evidence"]]
        assert len(keys)==len(set(keys))
