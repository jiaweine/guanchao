from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from typing import Any

from .detection import MarketingDetector
from .domain import RunEvent
from .evolution import EvolutionEngine
from .policy import OwnedPolicy
from .semantic import SemanticEvidenceGateway
from .store import Store
from .tools import ToolRegistry
from .verifier import ResultVerifier


class ActiveRunError(RuntimeError):
    pass


class RunCapacityError(RuntimeError):
    pass


class AgentHarness:
    def __init__(self, store: Store):
        self.store = store
        self.verifier = ResultVerifier()
        self.semantic_gateway = SemanticEvidenceGateway()
        self._guard = threading.RLock()
        self._learning_guard = threading.RLock()
        self._futures: dict[str, Future] = {}
        self._active_cases: dict[str, str] = {}
        max_workers = max(2, int(os.getenv("GUANCHAO_MAX_WORKERS", "8")))
        self._max_inflight = max(max_workers, int(os.getenv("GUANCHAO_MAX_INFLIGHT", "256")))
        self._capacity = threading.BoundedSemaphore(self._max_inflight)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="guanchao")
        self._learning_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="guanchao-learning")
        self._learning_future: Future | None = None

    def start(self, case_id: str, message: str, actor: str = "local") -> str:
        self._reserve(1)
        try:
            with self._guard:
                run_id = self._prepare_run(case_id, message, actor)
                self._submit(run_id)
                return run_id
        except Exception:
            self._capacity.release()
            raise

    def start_many(self, case_ids: list[str], message: str, actor: str = "local") -> list[dict[str, str]]:
        if not case_ids:
            return []
        self._reserve(len(case_ids))
        prepared: list[tuple[str, str]] = []
        try:
            with self._guard:
                for case_id in case_ids:
                    prepared.append((case_id, self._prepare_run(case_id, message, actor)))
                for _, run_id in prepared:
                    self._submit(run_id)
        except Exception:
            with self._guard:
                for case_id, run_id in prepared:
                    run = self.store.get_run(run_id)
                    state = run["state"]
                    state["events"].append(
                        RunEvent.create("error", "批量核查未启动", "批量任务准备过程中断，可以重新发起。", status="error").asdict()
                    )
                    self.store.update_run(run_id, state, "failed")
                    self._active_cases.pop(case_id, None)
            for _ in case_ids:
                self._capacity.release()
            raise
        return [{"case_id": case_id, "run_id": run_id} for case_id, run_id in prepared]

    def _reserve(self, count: int) -> None:
        acquired = 0
        for _ in range(count):
            if not self._capacity.acquire(blocking=False):
                for _ in range(acquired):
                    self._capacity.release()
                raise RunCapacityError(f"inflight capacity {self._max_inflight} reached")
            acquired += 1

    def _prepare_run(self, case_id: str, message: str, actor: str) -> str:
        case = self.store.get_case(case_id)
        existing = self._active_cases.get(case_id)
        if existing:
            run = self.store.get_run(existing)
            if run["status"] == "running":
                raise ActiveRunError(existing)
            self._active_cases.pop(case_id, None)
        db_active = self.store.active_run_for_case(case_id)
        if db_active:
            raise ActiveRunError(db_active["id"])
        self.store.add_message(case_id, "user", message)
        assets = self.store.list_assets(case_id, include_text=True)
        state: dict[str, Any] = {
            "goal": message or case["goal"],
            "targets": case["targets"],
            "assets": assets,
            "sample_size": len(case["targets"][0].get("posts") or []) if case["targets"] else 0,
            "completed_tools": [],
            "events": [RunEvent.create("plan", "开始核查", "正在判断哪些证据最值得先看。", status="working").asdict()],
            "evidence": [],
            "tool_outputs": {},
            "primary_result": {},
            "answer": None,
            "decision_count": 0,
            "trajectory": [],
        }
        run = self.store.create_run(case_id, state, actor=actor)
        self._active_cases[case_id] = run["id"]
        return run["id"]

    def _submit(self, run_id: str) -> None:
        self._futures[run_id] = self._executor.submit(self._execute, run_id)

    def execute_inline(self, case_id: str, message: str, actor: str = "local") -> dict[str, Any]:
        run_id = self.start(case_id, message, actor=actor)
        self.wait(run_id, 10)
        return self.store.get_run(run_id)

    def wait(self, run_id: str, timeout: float = 10) -> None:
        with self._guard:
            future = self._futures.get(run_id)
        if not future:
            return
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            return

    def wait_learning(self, timeout: float = 10) -> None:
        with self._learning_guard:
            future = self._learning_future
        if future is None:
            return
        try:
            future.result(timeout=timeout)
        except TimeoutError:
            return

    def observe_review(self, run_id: str, decision: str, actor: str = "local") -> None:
        with self._learning_guard:
            previous = self._learning_future
            self._learning_future = self._learning_executor.submit(
                self._apply_review_learning, run_id, decision, actor, previous
            )

    def _execute(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        state = run["state"]
        case_id = run["case_id"]
        detector = MarketingDetector(self.store.get_calibration(), self.semantic_gateway)
        policy = OwnedPolicy(self.store.get_policy_profile())
        registry = ToolRegistry(detector)
        try:
            for _ in range(len(registry.names()) + 1):
                decision = policy.decide(state["goal"], state)
                if decision is None:
                    break
                state["decision_count"] += 1
                state["events"].append(
                    RunEvent.create("decision", "继续核查", decision.reason, decision.tool, "working").asdict()
                )
                before = policy.signal(state)
                started = time.perf_counter()
                result = registry.get(decision.tool).handler(state)
                duration_ms = (time.perf_counter() - started) * 1000.0
                ok, reason = self.verifier.verify(result)
                if not ok:
                    state["events"].append(
                        RunEvent.create("verify", "这一步没有通过检查", reason, decision.tool, "error").asdict()
                    )
                    self.store.update_run(run_id, state, "failed")
                    return
                state["completed_tools"].append(decision.tool)
                state["tool_outputs"][decision.tool] = result.asdict()
                self._merge_evidence(state, result.asdict()["evidence"])
                state["events"].append(
                    RunEvent.create("tool", self._customer_title(decision.tool), result.summary, decision.tool, "done").asdict()
                )
                if decision.tool == "content.scan":
                    state["primary_result"] = result.payload
                if decision.tool == "stability.probe" and state.get("primary_result"):
                    state["primary_result"]["stability"] = result.payload.get("stability", state["primary_result"].get("stability"))
                if decision.tool == "verdict.compose":
                    state["primary_result"] = result.payload
                    state["answer"] = result.payload["summary"]
                    self.store.add_message(case_id, "assistant", state["answer"])
                if decision.features:
                    after = policy.signal(state)
                    reward = policy.reward(before, after, decision.tool, duration_ms)
                    if decision.tool == "verdict.compose":
                        reward = max(reward, before["verdict_readiness"])
                    state["trajectory"].append(
                        {
                            "action": decision.tool,
                            "features": decision.features,
                            "alternative": decision.alternative,
                            "alternative_features": decision.alternative_features,
                            "reward": reward,
                            "duration_ms": duration_ms,
                        }
                    )
                self.store.update_run(run_id, state, "running")
            if not state.get("answer"):
                state["answer"] = "当前资料还不足以形成稳定判断，请补充更多近期内容或可核对素材。"
                self.store.add_message(case_id, "assistant", state["answer"])
            state["events"].append(
                RunEvent.create("complete", "核查完成", "判断、证据和待补资料已经整理好。", status="done").asdict()
            )
            self.store.update_run(run_id, state, "completed")
            if state.get("trajectory"):
                self._schedule_trajectory_learning(run_id, state["trajectory"])
        except Exception as exc:
            state["internal_error"] = type(exc).__name__
            state["events"].append(
                RunEvent.create("error", "执行未完成", "本次核查中断，已保留已有记录，可以重新发起。", status="error").asdict()
            )
            self.store.update_run(run_id, state, "failed")
        finally:
            with self._guard:
                self._futures.pop(run_id, None)
                self._active_cases.pop(case_id, None)
            self._capacity.release()

    def _schedule_trajectory_learning(self, run_id: str, trajectory: list[dict[str, Any]]) -> None:
        with self._learning_guard:
            previous = self._learning_future
            self._learning_future = self._learning_executor.submit(
                self._apply_trajectory_learning, run_id, list(trajectory), previous
            )

    def _apply_trajectory_learning(self, run_id: str, trajectory: list[dict[str, Any]], previous: Future | None = None) -> None:
        if previous is not None:
            try:
                previous.result()
            except Exception:
                pass
        profile = self.store.get_policy_profile()
        profile.observe(trajectory)
        self.store.save_policy_profile(profile)
        self.store.record_event(
            "harness_experience_replayed",
            run_id=run_id,
            metadata={"steps": len(trajectory), "policy_steps": profile.steps},
        )

    def _apply_review_learning(self, run_id: str, decision: str, actor: str, previous: Future | None = None) -> None:
        if previous is not None:
            try:
                previous.result()
            except Exception:
                pass
        if decision == "uncertain":
            return
        run = self.store.get_run(run_id)
        state = run.get("state") or {}
        trajectory = list(state.get("trajectory") or [])
        result = state.get("primary_result") or {}
        calibration = self.store.get_calibration()
        predicted = float(result.get("marketing_likelihood") or 0.0) >= calibration.decision_threshold
        human = decision == "confirm_marketing"
        profile = self.store.get_policy_profile()
        profile.observe_review(trajectory, predicted == human)
        self.store.save_policy_profile(profile)
        report = EvolutionEngine().evolve(calibration, self.store.labeled_examples(), profile)
        if report.accepted:
            self.store.save_calibration(report.calibration)
        self.store.record_event(
            "harness_self_evolved",
            actor=actor,
            run_id=run_id,
            metadata={
                "review_correct": predicted == human,
                "calibration_promoted": report.accepted,
                "examples": report.examples,
                "policy_steps": profile.steps,
            },
        )

    @staticmethod
    def _merge_evidence(state: dict[str, Any], items: list[dict[str, Any]]) -> None:
        seen = {
            (evidence.get("key"), evidence.get("direction"), tuple(evidence.get("post_ids") or []), tuple(evidence.get("asset_ids") or []))
            for evidence in state.get("evidence") or []
        }
        for item in items:
            key = (item.get("key"), item.get("direction"), tuple(item.get("post_ids") or []), tuple(item.get("asset_ids") or []))
            if key not in seen:
                state["evidence"].append(item)
                seen.add(key)

    @staticmethod
    def _customer_title(tool: str) -> str:
        return {
            "workspace.inspect": "资料已整理",
            "profile.read": "主页已核对",
            "media.inspect": "素材已读取",
            "content.scan": "近期内容已扫描",
            "pattern.compare": "内容模式已对照",
            "peer.compare": "同批账号已比较",
            "stability.probe": "稳定性已检查",
            "evidence.challenge": "反向线索已核查",
            "verdict.compose": "判断已形成",
        }.get(tool, "步骤完成")
