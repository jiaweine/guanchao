from __future__ import annotations

import threading
from typing import Any

from .detection import MarketingDetector
from .domain import RunEvent
from .policy import OwnedPolicy
from .store import Store
from .tools import ToolRegistry
from .verifier import ResultVerifier


class AgentHarness:
    def __init__(self, store: Store):
        self.store = store
        self.policy = OwnedPolicy()
        self.verifier = ResultVerifier()
        self._threads: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()

    def start(self, case_id: str, message: str) -> str:
        case = self.store.get_case(case_id)
        self.store.add_message(case_id, "user", message)
        state: dict[str, Any] = {
            "goal": message or case["goal"],
            "targets": case["targets"],
            "sample_size": len(case["targets"][0].get("posts") or []) if case["targets"] else 0,
            "completed_tools": [],
            "events": [RunEvent.create("plan", "开始调查", "正在根据你的目标决定先查什么。", status="working").asdict()],
            "evidence": [],
            "tool_outputs": {},
            "primary_result": {},
            "answer": None,
        }
        run = self.store.create_run(case_id, state)
        thread = threading.Thread(target=self._execute, args=(run["id"],), daemon=True)
        with self._guard:
            self._threads[run["id"]] = thread
        thread.start()
        return run["id"]

    def execute_inline(self, case_id: str, message: str) -> dict[str, Any]:
        run_id = self.start(case_id, message)
        self.wait(run_id, timeout=10)
        return self.store.get_run(run_id)

    def wait(self, run_id: str, timeout: float = 10.0) -> None:
        with self._guard:
            thread = self._threads.get(run_id)
        if thread:
            thread.join(timeout=timeout)

    def _execute(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        state = run["state"]
        detector = MarketingDetector(self.store.get_calibration())
        registry = ToolRegistry(detector)
        try:
            for _ in range(10):
                decision = self.policy.decide(state["goal"], state)
                if decision is None:
                    break
                state["events"].append(
                    RunEvent.create("decision", "继续核查", decision.reason, tool=decision.tool, status="working").asdict()
                )
                self.store.update_run(run_id, state, "running")
                spec = registry.get(decision.tool)
                result = spec.handler(state)
                ok, reason = self.verifier.verify(result)
                if not ok:
                    state["events"].append(RunEvent.create("verify", "核查未通过", reason, decision.tool, "error").asdict())
                    self.store.update_run(run_id, state, "failed")
                    return
                state["completed_tools"].append(decision.tool)
                state["tool_outputs"][decision.tool] = result.asdict()
                state["evidence"].extend(item.asdict() for item in result.evidence)
                state["events"].append(
                    RunEvent.create("tool", self._customer_title(decision.tool), result.summary, decision.tool, "done").asdict()
                )
                if decision.tool == "content.scan":
                    state["primary_result"] = result.payload
                if decision.tool == "verdict.compose":
                    state["primary_result"] = result.payload
                    state["answer"] = result.payload["summary"]
                    self.store.add_message(run["case_id"], "assistant", state["answer"])
                self.store.update_run(run_id, state, "running")
            if not state.get("answer"):
                state["answer"] = "当前资料还不足以形成稳定判断，请补充更多近期内容。"
                self.store.add_message(run["case_id"], "assistant", state["answer"])
            state["events"].append(RunEvent.create("complete", "调查完成", "结论已和证据一起整理好。", status="done").asdict())
            self.store.update_run(run_id, state, "completed")
        except Exception as exc:
            state["events"].append(RunEvent.create("error", "执行中断", str(exc), status="error").asdict())
            self.store.update_run(run_id, state, "failed")
        finally:
            with self._guard:
                self._threads.pop(run_id, None)

    @staticmethod
    def _customer_title(tool: str) -> str:
        return {
            "workspace.inspect": "资料已读完",
            "profile.read": "主页线索已核对",
            "content.scan": "近期内容已扫描",
            "pattern.compare": "内容模式已对照",
            "peer.compare": "同批账号已比较",
            "evidence.challenge": "反向证据已检查",
            "verdict.compose": "判断已形成",
        }.get(tool, "步骤完成")
