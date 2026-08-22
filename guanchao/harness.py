from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from typing import Any

from .detection import Calibration, MarketingDetector
from .domain import FeatureVector, RunEvent
from .evolution import EvolutionEngine, EvolutionReport, LabeledExample
from .policy import OwnedPolicy
from .run_lock import run_claim
from .semantic import SemanticEvidenceGateway
from .store import Store
from .tools import ToolRegistry
from .verifier import ResultVerifier

_LEARNING_CLAIM = "__guanchao_learning_state__"


class ActiveRunError(RuntimeError):
    pass


class RunCapacityError(RuntimeError):
    pass


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _produced_marketing(label: str) -> bool | None:
    if label in {"明显营销倾向", "高度营销化"}:
        return True
    if label == "更像普通创作者":
        return False
    return None


class AgentHarness:
    def __init__(self, store: Store):
        self.store = store
        self.verifier = ResultVerifier()
        self.semantic_gateway = SemanticEvidenceGateway()
        self._guard = threading.RLock()
        self._learning_guard = threading.RLock()
        self._futures: dict[str, Future] = {}
        self._active_cases: dict[str, str] = {}
        self._closed = False
        max_workers = _env_int("GUANCHAO_MAX_WORKERS", 8, 2, 128)
        configured_inflight = _env_int("GUANCHAO_MAX_INFLIGHT", 256, 1, 100_000)
        self._max_inflight = max(max_workers, configured_inflight)
        self._capacity = threading.BoundedSemaphore(self._max_inflight)
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="guanchao")
        self._learning_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="guanchao-learning")
        self._learning_future: Future | None = None

    def close(self) -> None:
        """Finish durable work and release executor threads exactly once."""
        with self._guard:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)
        self._learning_executor.shutdown(wait=True, cancel_futures=False)

    def start(self, case_id: str, message: str, actor: str = "local") -> str:
        self._ensure_open()
        self._reserve(1)
        run_id: str | None = None
        try:
            with self._guard:
                self._ensure_open()
                run_id = self._prepare_run(case_id, message, actor)
                self._submit(run_id)
                return run_id
        except Exception:
            if run_id is not None:
                with self._guard:
                    self._fail_unsubmitted_run(case_id, run_id, "核查未启动")
            self._capacity.release()
            raise

    def start_many(self, case_ids: list[str], message: str, actor: str = "local") -> list[dict[str, str]]:
        if not case_ids:
            return []
        self._ensure_open()
        self._reserve(len(case_ids))
        prepared: list[tuple[str, str]] = []
        submitted = 0
        try:
            with self._guard:
                self._ensure_open()
                for case_id in case_ids:
                    prepared.append((case_id, self._prepare_run(case_id, message, actor)))
                for _, run_id in prepared:
                    self._submit(run_id)
                    submitted += 1
        except Exception:
            with self._guard:
                for case_id, run_id in prepared[submitted:]:
                    self._fail_unsubmitted_run(case_id, run_id, "批量核查未启动")
            for _ in range(len(case_ids) - submitted):
                self._capacity.release()
            raise
        return [{"case_id": case_id, "run_id": run_id} for case_id, run_id in prepared]

    def _ensure_open(self) -> None:
        with self._guard:
            if self._closed:
                raise RunCapacityError("harness is closed")

    def _reserve(self, count: int) -> None:
        acquired = 0
        for _ in range(count):
            if not self._capacity.acquire(blocking=False):
                for _ in range(acquired):
                    self._capacity.release()
                raise RunCapacityError(f"inflight capacity {self._max_inflight} reached")
            acquired += 1

    def _prepare_run(self, case_id: str, message: str, actor: str) -> str:
        with run_claim(self.store.path, case_id):
            case = self.store.get_case(case_id)
            existing = self._active_cases.get(case_id)
            if existing:
                run = self.store.get_run(existing)
                if run["status"] == "running":
                    raise ActiveRunError(existing)
                if self._active_cases.get(case_id) == existing:
                    self._active_cases.pop(case_id, None)
            db_active = self.store.active_run_for_case(case_id)
            if db_active:
                raise ActiveRunError(db_active["id"])
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
            try:
                # User message, running row and run_started audit event are one
                # SQLite transaction. If the DB-level single-running invariant
                # rejects this claim, no ghost chat message or orphan run survives.
                run = self.store.create_run(
                    case_id,
                    state,
                    actor=actor,
                    user_message=message,
                )
            except sqlite3.IntegrityError:
                active = self.store.active_run_for_case(case_id)
                if active:
                    raise ActiveRunError(active["id"]) from None
                raise
            self._active_cases[case_id] = run["id"]
            return run["id"]

    def _submit(self, run_id: str) -> None:
        future = self._executor.submit(self._execute, run_id)
        with self._guard:
            self._futures[run_id] = future
        future.add_done_callback(lambda done, rid=run_id: self._forget_future(rid, done))

    def _forget_future(self, run_id: str, future: Future) -> None:
        with self._guard:
            if self._futures.get(run_id) is future:
                self._futures.pop(run_id, None)

    def _fail_unsubmitted_run(self, case_id: str, run_id: str, title: str) -> None:
        try:
            run = self.store.get_run(run_id)
        except KeyError:
            if self._active_cases.get(case_id) == run_id:
                self._active_cases.pop(case_id, None)
            return
        if run["status"] == "running":
            state = run["state"]
            state.setdefault("events", []).append(
                RunEvent.create(
                    "error",
                    title,
                    "任务没有进入执行器，已安全结束本次记录，可以重新发起。",
                    status="error",
                ).asdict()
            )
            self.store.update_run(run_id, state, "failed")
        if self._active_cases.get(case_id) == run_id:
            self._active_cases.pop(case_id, None)

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
            if self._closed:
                return
            previous = self._learning_future
            try:
                future = self._learning_executor.submit(
                    self._apply_review_learning, run_id, decision, actor, previous
                )
            except RuntimeError:
                return
            self._learning_future = future

    def evolve_now(self, actor: str = "local") -> EvolutionReport:
        with self._learning_guard:
            if self._closed:
                raise RuntimeError("harness is closed")
            previous = self._learning_future
            future = self._learning_executor.submit(self._apply_manual_evolution, actor, previous)
            self._learning_future = future
        return future.result()

    def _execute(self, run_id: str) -> None:
        case_id: str | None = None
        state: dict[str, Any] | None = None
        completed = False
        try:
            run = self.store.get_run(run_id)
            state = run["state"]
            case_id = run["case_id"]
            calibration = self.store.get_calibration()
            detector = MarketingDetector(calibration, self.semantic_gateway)
            policy = OwnedPolicy(
                self.store.get_policy_profile(),
                decision_threshold=calibration.decision_threshold,
            )
            registry = ToolRegistry(detector)

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
                serialized = result.asdict()
                state["tool_outputs"][decision.tool] = serialized
                self._merge_evidence(state, serialized["evidence"])
                state["events"].append(
                    RunEvent.create("tool", self._customer_title(decision.tool), result.summary, decision.tool, "done").asdict()
                )
                if decision.tool == "content.scan":
                    state["primary_result"] = result.payload
                if decision.tool == "stability.probe" and state.get("primary_result"):
                    state["primary_result"]["stability"] = result.payload.get(
                        "stability", state["primary_result"].get("stability")
                    )
                if decision.tool == "verdict.compose":
                    state["primary_result"] = result.payload
                    state["answer"] = result.payload["summary"]
                    self.store.add_message(case_id, "assistant", state["answer"])
                if decision.features:
                    after = policy.signal(state)
                    information_gain = policy.reward(before, after, decision.tool, duration_ms)
                    if decision.tool == "verdict.compose":
                        reward = before["verdict_readiness"]
                    else:
                        reward = max(-1.0, min(1.0, information_gain - before["verdict_readiness"]))
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
            completed = True

            if state.get("trajectory"):
                try:
                    self._schedule_trajectory_learning(run_id, state["trajectory"])
                except Exception as exc:
                    try:
                        self.store.record_event(
                            "harness_learning_schedule_failed",
                            run_id=run_id,
                            metadata={"error": type(exc).__name__},
                        )
                    except Exception:
                        pass
        except Exception as exc:
            if state is not None and not completed:
                state["internal_error"] = type(exc).__name__
                state.setdefault("events", []).append(
                    RunEvent.create(
                        "error",
                        "执行未完成",
                        "本次核查中断，已保留已有记录，可以重新发起。",
                        status="error",
                    ).asdict()
                )
                try:
                    self.store.update_run(run_id, state, "failed")
                except Exception:
                    pass
        finally:
            with self._guard:
                if case_id is not None and self._active_cases.get(case_id) == run_id:
                    self._active_cases.pop(case_id, None)
                elif case_id is None:
                    for active_case, active_run in list(self._active_cases.items()):
                        if active_run == run_id:
                            self._active_cases.pop(active_case, None)
            self._capacity.release()

    def _schedule_trajectory_learning(self, run_id: str, trajectory: list[dict[str, Any]]) -> None:
        with self._learning_guard:
            previous = self._learning_future
            self._learning_future = self._learning_executor.submit(
                self._apply_trajectory_learning, run_id, list(trajectory), previous
            )

    def _apply_trajectory_learning(
        self,
        run_id: str,
        trajectory: list[dict[str, Any]],
        previous: Future | None = None,
    ) -> None:
        self._await_previous(previous)
        with run_claim(self.store.path, _LEARNING_CLAIM):
            profile = self.store.get_policy_profile()
            profile.observe(trajectory)
            self.store.save_policy_profile(profile)
            self.store.record_event(
                "harness_experience_replayed",
                run_id=run_id,
                metadata={"steps": len(trajectory), "policy_steps": profile.steps},
            )

    @staticmethod
    def _await_previous(previous: Future | None) -> None:
        if previous is None:
            return
        try:
            previous.result()
        except Exception:
            pass

    def _apply_review_learning(
        self,
        run_id: str,
        decision: str,
        actor: str,
        previous: Future | None = None,
    ) -> None:
        self._await_previous(previous)
        with run_claim(self.store.path, _LEARNING_CLAIM):
            stored_review = next(
                (row for row in self.store.review_rows() if row.get("run_id") == run_id),
                None,
            )
            if stored_review is None:
                return
            decision = str(stored_review.get("decision") or decision)
            actor = str(stored_review.get("reviewer") or actor)

            run = self.store.get_run(run_id)
            state = run.get("state") or {}
            result = state.get("primary_result") or {}
            predicted = _produced_marketing(str(result.get("label") or ""))
            review_correct: bool | None = None
            if decision != "uncertain":
                human = decision == "confirm_marketing"
                review_correct = None if predicted is None else predicted == human

            latest = self._latest_reviews_by_case()
            profile = self.store.get_policy_profile()
            profile_changed = self._sync_review_feedback(profile, latest)
            examples = self.review_examples(latest)
            fingerprint = self._review_dataset_fingerprint(examples)
            dataset_changed = fingerprint != profile.review_dataset_fingerprint
            calibration_promoted = False
            calibration_reset = False
            report_examples = len(examples)

            if dataset_changed:
                report, calibration_promoted, calibration_reset = self._fit_review_dataset(
                    examples, profile
                )
                report_examples = report.examples
                profile.review_dataset_fingerprint = fingerprint

            if profile_changed or dataset_changed:
                self.store.save_policy_profile(profile)

            if not profile_changed and not dataset_changed:
                self.store.record_event(
                    "harness_review_feedback_noop",
                    actor=actor,
                    run_id=run_id,
                    metadata={"decision": decision},
                )
                return

            self.store.record_event(
                "harness_self_evolved",
                actor=actor,
                run_id=run_id,
                metadata={
                    "review_correct": review_correct,
                    "calibration_promoted": calibration_promoted,
                    "calibration_reset": calibration_reset,
                    "examples": report_examples,
                    "policy_steps": profile.steps,
                    "review_feedback": len(profile.review_feedback),
                },
            )

    def _apply_manual_evolution(
        self,
        actor: str,
        previous: Future | None = None,
    ) -> EvolutionReport:
        self._await_previous(previous)
        with run_claim(self.store.path, _LEARNING_CLAIM):
            latest = self._latest_reviews_by_case()
            profile = self.store.get_policy_profile()
            self._sync_review_feedback(profile, latest)
            examples = self.review_examples(latest)
            report, _, _ = self._fit_review_dataset(examples, profile)
            profile.review_dataset_fingerprint = self._review_dataset_fingerprint(examples)
            self.store.save_policy_profile(profile)
            return report

    def _fit_review_dataset(
        self,
        examples: list[LabeledExample],
        profile: Any,
    ) -> tuple[EvolutionReport, bool, bool]:
        baseline = Calibration()
        current = self.store.get_calibration()
        report = EvolutionEngine().evolve(baseline, examples, profile)
        if report.accepted:
            self.store.save_calibration(report.calibration)
            return report, True, False
        reset = current != baseline
        if reset:
            self.store.save_calibration(baseline)
        return report, False, reset

    def _latest_reviews_by_case(self) -> dict[str, dict[str, Any]]:
        return {
            str(row.get("case_id")): row
            for row in self.store.latest_review_snapshots()
            if row.get("case_id")
        }

    def _sync_review_feedback(
        self,
        profile: Any,
        latest: dict[str, dict[str, Any]] | None = None,
    ) -> bool:
        desired: dict[str, dict[str, Any]] = {}
        latest = latest if latest is not None else self._latest_reviews_by_case()
        for case_id, row in latest.items():
            decision = str(row.get("decision") or "")
            if decision == "uncertain":
                continue
            state = row.get("run_state") or {}
            if not isinstance(state, dict):
                continue
            trajectory = list(state.get("trajectory") or [])
            verdicts = [
                item
                for item in trajectory
                if isinstance(item, dict) and item.get("action") == "verdict.compose"
            ]
            if not verdicts:
                continue
            raw_features = verdicts[-1].get("features") or []
            if not isinstance(raw_features, list) or len(raw_features) != 11:
                continue
            try:
                features = [float(value) for value in raw_features]
            except (TypeError, ValueError, OverflowError):
                continue
            if any(not (float("-inf") < value < float("inf")) for value in features):
                continue
            predicted = _produced_marketing(
                str((state.get("primary_result") or {}).get("label") or "")
            )
            human = decision == "confirm_marketing"
            reward = 0.0 if predicted is None else 1.0 if predicted == human else -1.0
            desired[case_id] = {"features": features, "reward": reward}

        changed = profile.review_feedback != desired or profile.reviews != len(desired)
        if changed:
            profile.review_feedback = desired
            profile.reviews = len(desired)
        return changed

    def review_examples(
        self,
        latest: dict[str, dict[str, Any]] | None = None,
    ) -> list[LabeledExample]:
        examples: list[LabeledExample] = []
        latest = latest if latest is not None else self._latest_reviews_by_case()
        for case_id, row in latest.items():
            decision = str(row.get("decision") or "")
            if decision == "uncertain":
                continue
            state = row.get("run_state") or {}
            if not isinstance(state, dict):
                continue
            features = (state.get("primary_result") or {}).get("features")
            if not isinstance(features, dict):
                continue
            try:
                vector = FeatureVector(**features)
            except (TypeError, ValueError):
                continue
            examples.append(
                LabeledExample(
                    vector,
                    1 if decision == "confirm_marketing" else 0,
                    case_id,
                )
            )
        examples.sort(key=lambda item: item.group)
        return examples

    @staticmethod
    def _review_dataset_fingerprint(examples: list[Any]) -> str:
        rows = [
            {
                "group": str(item.group),
                "label": int(item.label),
                "features": item.features.asdict(),
            }
            for item in examples
        ]
        rows.sort(
            key=lambda row: (
                row["group"],
                row["label"],
                json.dumps(row["features"], sort_keys=True, separators=(",", ":")),
            )
        )
        payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _merge_evidence(state: dict[str, Any], items: list[dict[str, Any]]) -> None:
        seen = {
            (
                evidence.get("key"),
                evidence.get("direction"),
                tuple(evidence.get("post_ids") or []),
                tuple(evidence.get("asset_ids") or []),
            )
            for evidence in state.get("evidence") or []
        }
        for item in items:
            key = (
                item.get("key"),
                item.get("direction"),
                tuple(item.get("post_ids") or []),
                tuple(item.get("asset_ids") or []),
            )
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
