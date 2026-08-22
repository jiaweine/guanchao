from __future__ import annotations

import hashlib
import math
import os
from dataclasses import asdict, dataclass, field
from typing import Any

_ACTIONS = (
    "profile.read",
    "media.inspect",
    "pattern.compare",
    "peer.compare",
    "stability.probe",
    "evidence.challenge",
    "verdict.compose",
)
_DIM = 11


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _env_int(name: str, default: int, low: int, high: int) -> int:
    return max(low, min(high, _safe_int(os.getenv(name, str(default)), default)))


def _vector(raw: Any) -> list[float] | None:
    if not isinstance(raw, list) or len(raw) != _DIM:
        return None
    values = [_finite_float(value, math.nan) for value in raw]
    if any(not math.isfinite(value) for value in values):
        return None
    return values


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    parsed = _finite_float(value, low)
    return max(low, min(high, parsed))


def _identity() -> list[list[float]]:
    return [[1.0 if i == j else 0.0 for j in range(_DIM)] for i in range(_DIM)]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _matvec(matrix: list[list[float]], vector: list[float]) -> list[float]:
    return [_dot(row, vector) for row in matrix]


def _cosine(a: list[float], b: list[float]) -> float:
    denom = math.sqrt(max(0.0, _dot(a, a) * _dot(b, b)))
    if denom <= 1e-12:
        return 0.0
    return max(0.0, min(1.0, _dot(a, b) / denom))


def _sigmoid(value: float) -> float:
    value = max(-20.0, min(20.0, _finite_float(value, 0.0)))
    return 1.0 / (1.0 + math.exp(-value))


@dataclass(slots=True)
class PolicyProfile:
    """Persistent self-evolving policy state.

    Each investigation action owns a Bayesian linear value model represented by
    a posterior mean vector and an inverse precision matrix. The initial state is
    deliberately uninformative; domain behaviour is acquired from executed
    trajectories and human review rather than hand-tuned action weights.

    Human review is stored separately from the trajectory posterior. Harness
    reconciliation keeps one feedback row per current case-level review so retries
    and edited reviews replace stale supervision instead of stacking gradients.
    """

    weights: dict[str, list[float]] = field(
        default_factory=lambda: {action: [0.0] * _DIM for action in _ACTIONS}
    )
    covariance: dict[str, list[list[float]]] = field(
        default_factory=lambda: {action: _identity() for action in _ACTIONS}
    )
    latency_ms: dict[str, float] = field(default_factory=dict)
    latency_count: dict[str, int] = field(default_factory=dict)
    experiences: list[dict[str, Any]] = field(default_factory=list)
    review_feedback: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_dataset_fingerprint: str = ""
    steps: int = 0
    reviews: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "PolicyProfile":
        base = cls()
        if not raw or not isinstance(raw, dict) or not isinstance(raw.get("weights"), dict):
            return base
        incoming_weights = raw["weights"]
        incoming_cov = raw.get("covariance") if isinstance(raw.get("covariance"), dict) else {}
        weights: dict[str, list[float]] = {}
        covariance: dict[str, list[list[float]]] = {}
        for action in _ACTIONS:
            row = incoming_weights.get(action)
            if isinstance(row, list):
                parsed = [_finite_float(value, 0.0) for value in row[:_DIM]]
            else:
                parsed = []
            parsed += [0.0] * (_DIM - len(parsed))
            weights[action] = parsed

            matrix = incoming_cov.get(action)
            parsed_matrix: list[list[float]] = []
            valid = isinstance(matrix, list) and len(matrix) == _DIM
            if valid:
                for values in matrix:
                    if not isinstance(values, list) or len(values) != _DIM:
                        valid = False
                        break
                    row_values = [_finite_float(value, math.nan) for value in values]
                    if any(not math.isfinite(value) for value in row_values):
                        valid = False
                        break
                    parsed_matrix.append(row_values)
            covariance[action] = parsed_matrix if valid else _identity()

        experiences: list[dict[str, Any]] = []
        incoming_experiences = raw.get("experiences")
        if isinstance(incoming_experiences, list):
            for item in incoming_experiences:
                if not isinstance(item, dict) or item.get("action") not in _ACTIONS:
                    continue
                features = _vector(item.get("features"))
                if features is None:
                    continue
                reward = max(-1.0, min(1.0, _finite_float(item.get("reward"), 0.0)))
                experiences.append(
                    {
                        "action": str(item["action"]),
                        "features": features,
                        "reward": reward,
                        "key": str(item.get("key") or ""),
                    }
                )

        feedback: dict[str, dict[str, Any]] = {}
        incoming_feedback = raw.get("review_feedback")
        if isinstance(incoming_feedback, dict):
            for feedback_key, item in incoming_feedback.items():
                if not isinstance(item, dict):
                    continue
                features = _vector(item.get("features"))
                if features is None:
                    continue
                reward = max(-1.0, min(1.0, _finite_float(item.get("reward"), 0.0)))
                feedback[str(feedback_key)] = {"features": features, "reward": reward}

        latency_ms: dict[str, float] = {}
        if isinstance(raw.get("latency_ms"), dict):
            for key, value in raw["latency_ms"].items():
                if key in _ACTIONS:
                    parsed = _finite_float(value, 0.0)
                    if parsed > 0.0:
                        latency_ms[key] = parsed
        latency_count: dict[str, int] = {}
        if isinstance(raw.get("latency_count"), dict):
            for key, value in raw["latency_count"].items():
                if key in _ACTIONS:
                    parsed = max(0, _safe_int(value, 0))
                    if parsed:
                        latency_count[key] = parsed

        return cls(
            weights=weights,
            covariance=covariance,
            latency_ms=latency_ms,
            latency_count=latency_count,
            experiences=experiences,
            review_feedback=feedback,
            review_dataset_fingerprint=str(raw.get("review_dataset_fingerprint") or "")[:256],
            steps=max(0, _safe_int(raw.get("steps"), 0)),
            reviews=max(0, _safe_int(raw.get("reviews"), len(feedback))),
        )

    def observe(self, trajectory: list[dict[str, Any]]) -> None:
        for item in trajectory:
            if not isinstance(item, dict):
                continue
            action = str(item.get("action") or "")
            features = _vector(item.get("features"))
            if action not in _ACTIONS or features is None:
                continue
            reward = max(-1.0, min(1.0, _finite_float(item.get("reward"), 0.0)))
            self._recursive_update(action, features, reward)
            alternative = str(item.get("alternative") or "")
            alternative_features = _vector(item.get("alternative_features"))
            if alternative in _ACTIONS and alternative_features is not None and abs(reward) > 1e-9:
                self._preference_update(
                    action if reward >= 0 else alternative,
                    features if reward >= 0 else alternative_features,
                    alternative if reward >= 0 else action,
                    alternative_features if reward >= 0 else features,
                    abs(reward),
                )
            latency = max(0.0, _finite_float(item.get("duration_ms"), 0.0))
            self._observe_latency(action, latency)
            self._remember(action, features, reward)
            self.steps += 1

    def observe_review(
        self,
        feedback_key: str,
        trajectory: list[dict[str, Any]],
        correct: bool | None,
    ) -> bool:
        verdicts = [
            item for item in trajectory
            if isinstance(item, dict) and item.get("action") == "verdict.compose"
        ]
        if not verdicts:
            return False
        features = _vector(verdicts[-1].get("features"))
        if features is None:
            return False
        feedback = {
            "features": features,
            "reward": 0.0 if correct is None else 1.0 if correct else -1.0,
        }
        key = str(feedback_key)
        previous = self.review_feedback.get(key)
        if previous == feedback:
            return False
        if previous is None:
            self.reviews += 1
        self.review_feedback[key] = feedback
        return True

    def clear_review(self, feedback_key: str) -> bool:
        key = str(feedback_key)
        if key not in self.review_feedback:
            return False
        self.review_feedback.pop(key, None)
        self.reviews = max(0, self.reviews - 1)
        return True

    def _recursive_update(self, action: str, x: list[float], reward: float) -> None:
        covariance = self.covariance[action]
        px = _matvec(covariance, x)
        denominator = max(1e-9, 1.0 + _dot(x, px))
        prediction = _dot(self.weights[action], x)
        residual = reward - prediction
        self.weights[action] = [w + (gain / denominator) * residual for w, gain in zip(self.weights[action], px)]
        self.covariance[action] = [
            [covariance[i][j] - px[i] * px[j] / denominator for j in range(_DIM)]
            for i in range(_DIM)
        ]

    def _preference_update(
        self,
        preferred: str,
        preferred_x: list[float],
        rejected: str,
        rejected_x: list[float],
        strength: float,
    ) -> None:
        preferred_score = _dot(self.weights[preferred], preferred_x)
        rejected_score = _dot(self.weights[rejected], rejected_x)
        gradient = (1.0 - _sigmoid(preferred_score - rejected_score)) * strength / math.sqrt(self.steps + 1.0)
        self.weights[preferred] = [w + gradient * x for w, x in zip(self.weights[preferred], preferred_x)]
        self.weights[rejected] = [w - gradient * x for w, x in zip(self.weights[rejected], rejected_x)]

    def _observe_latency(self, action: str, latency: float) -> None:
        if latency <= 0.0:
            return
        count = self.latency_count.get(action, 0) + 1
        mean = self.latency_ms.get(action, latency)
        self.latency_ms[action] = mean + (latency - mean) / count
        self.latency_count[action] = count

    def _remember(self, action: str, features: list[float], reward: float) -> None:
        digest = hashlib.sha256(
            (action + "|" + "|".join(f"{value:.6f}" for value in features) + f"|{reward:.6f}").encode()
        ).hexdigest()
        self.experiences.append({"action": action, "features": features, "reward": reward, "key": digest})
        capacity = _env_int("GUANCHAO_POLICY_MEMORY", 512, 64, 100_000)
        if len(self.experiences) > capacity:
            # Deterministic retention keeps serialization reproducible. The key is
            # a content digest, so duplicate replay rows naturally co-locate.
            self.experiences = sorted(
                self.experiences, key=lambda item: str(item.get("key") or "")
            )[:capacity]


@dataclass(slots=True)
class Decision:
    tool: str
    reason: str
    utility: float
    features: list[float] = field(default_factory=list)
    alternative: str = ""
    alternative_features: list[float] = field(default_factory=list)


class OwnedPolicy:
    """Self-evolving contextual policy with experience replay."""

    def __init__(
        self,
        profile: PolicyProfile | None = None,
        decision_threshold: float = 0.5,
    ):
        self.profile = profile or PolicyProfile()
        self.decision_threshold = _clip(decision_threshold, 0.01, 0.99)

    def decide(self, goal: str, state: dict[str, Any]) -> Decision | None:
        completed = set(state.get("completed_tools") or [])
        if "verdict.compose" in completed:
            return None
        if "workspace.inspect" not in completed:
            return Decision("workspace.inspect", "先确认资料、素材和缺口", 1.0)
        if "content.scan" not in completed:
            return Decision("content.scan", "先建立近期内容的基础判断", 1.0)
        candidates = self._available(state, completed)
        if not candidates:
            return None
        scored: list[tuple[float, str, list[float]]] = []
        for action in candidates:
            features = self.features(goal, state, action)
            scored.append((self._score(action, features), action, features))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        utility, action, features = scored[0]
        alternative = scored[1][1] if len(scored) > 1 else ""
        alternative_features = scored[1][2] if len(scored) > 1 else []
        return Decision(action, self._reason(action), utility, features, alternative, alternative_features)

    def features(self, goal: str, state: dict[str, Any], action: str) -> list[float]:
        targets = state.get("targets") or []
        assets = state.get("assets") or []
        primary = state.get("primary_result") or {}
        confidence = _clip(_finite_float(primary.get("confidence"), 0.0))
        marketing = _clip(_finite_float(primary.get("marketing_likelihood"), 0.5))
        stability = _clip(_finite_float(primary.get("stability"), 0.0))
        sample_size = max(0, _safe_int(state.get("sample_size"), 0))
        evidence_count = len({
            (e.get("key"), e.get("direction"))
            for e in state.get("evidence") or []
            if isinstance(e, dict)
        })
        ready_assets = sum(
            1 for item in assets if isinstance(item, dict) and item.get("status") == "ready"
        )
        uncertainty = 1.0 - confidence
        boundary = self._boundary(marketing)
        instability = 1.0 - stability
        sample_support = sample_size / (sample_size + 1.0)
        evidence_support = evidence_count / (evidence_count + 1.0)
        asset_support = ready_assets / len(assets) if assets else 0.0
        has_bio = 1.0 if targets and isinstance(targets[0], dict) and targets[0].get("bio") else 0.0
        peer_support = (len(targets) - 1) / len(targets) if targets else 0.0
        cautious = 1.0 if any(
            word in str(goal)
            for word in ("误判", "反向", "谨慎", "仔细", "认真", "证据", "核实", "复核")
        ) else 0.0
        need = self._action_need(
            action,
            uncertainty,
            boundary,
            instability,
            sample_support,
            evidence_support,
            asset_support,
            has_bio,
            peer_support,
            cautious,
        )
        return [
            1.0,
            uncertainty,
            boundary,
            instability,
            sample_support,
            evidence_support,
            asset_support,
            has_bio,
            peer_support,
            cautious,
            need,
        ]

    def reward(self, before: dict[str, float], after: dict[str, float], action: str, duration_ms: float) -> float:
        gains = [
            before["uncertainty"] - after["uncertainty"],
            before["instability"] - after["instability"],
            after["evidence_support"] - before["evidence_support"],
        ]
        if action == "verdict.compose":
            gains.append(after["verdict_readiness"] - before["verdict_readiness"])
        raw = sum(gains) / len(gains)
        latencies = [value for value in self.profile.latency_ms.values() if value > 0]
        duration = max(0.0, _finite_float(duration_ms, 0.0))
        if duration > 0 and latencies:
            reference = sorted(latencies)[len(latencies) // 2]
            raw /= 1.0 + duration / max(reference, 1e-6)
        return max(-1.0, min(1.0, _finite_float(raw, 0.0)))

    def signal(self, state: dict[str, Any]) -> dict[str, float]:
        primary = state.get("primary_result") or {}
        confidence = _clip(_finite_float(primary.get("confidence"), 0.0))
        marketing = _clip(_finite_float(primary.get("marketing_likelihood"), 0.5))
        stability = _clip(_finite_float(primary.get("stability"), 0.0))
        evidence_count = len({
            (e.get("key"), e.get("direction"))
            for e in state.get("evidence") or []
            if isinstance(e, dict)
        })
        evidence_support = evidence_count / (evidence_count + 1.0)
        boundary = self._boundary(marketing)
        return {
            "uncertainty": 1.0 - confidence,
            "instability": 1.0 - stability,
            "evidence_support": evidence_support,
            "verdict_readiness": confidence * stability * evidence_support * (1.0 - boundary),
        }

    def _boundary(self, marketing: float) -> float:
        span = max(self.decision_threshold, 1.0 - self.decision_threshold, 1e-6)
        return 1.0 - min(1.0, abs(marketing - self.decision_threshold) / span)

    def _score(self, action: str, features: list[float]) -> float:
        mean = _dot(self.profile.weights[action], features)
        covariance = self.profile.covariance[action]
        variance = max(1e-12, _finite_float(_dot(features, _matvec(covariance, features)), 1e-12))
        replay_rows: list[tuple[float, float]] = []
        for item in self.profile.experiences:
            if item.get("action") != action:
                continue
            other = _vector(item.get("features"))
            if other is None:
                continue
            similarity = _cosine(features, other)
            if similarity > 0:
                replay_rows.append((similarity, _finite_float(item.get("reward"), 0.0)))
        mean = self._blend_replay(mean, variance, replay_rows)

        if action == "verdict.compose":
            review_rows: list[tuple[float, float]] = []
            for item in self.profile.review_feedback.values():
                if not isinstance(item, dict):
                    continue
                other = _vector(item.get("features"))
                reward = _finite_float(item.get("reward"), 0.0)
                if other is None or abs(reward) <= 1e-9:
                    continue
                similarity = _cosine(features, other)
                if similarity > 0:
                    review_rows.append((similarity, reward))
            mean = self._blend_replay(mean, variance, review_rows)

        exploration = math.sqrt(variance * math.log(max(0, self.profile.steps) + 2.0))
        return _finite_float(mean + exploration, 0.0)

    @staticmethod
    def _blend_replay(mean: float, variance: float, rows: list[tuple[float, float]]) -> float:
        if not rows:
            return mean
        rows.sort(key=lambda row: row[0], reverse=True)
        rows = rows[: max(1, math.ceil(math.sqrt(len(rows))))]
        replay_precision = sum(similarity for similarity, _ in rows)
        if replay_precision <= 0.0:
            return mean
        model_precision = 1.0 / max(variance, 1e-12)
        replay_value = sum(similarity * reward for similarity, reward in rows) / replay_precision
        return (model_precision * mean + replay_precision * replay_value) / (model_precision + replay_precision)

    @staticmethod
    def _available(state: dict[str, Any], completed: set[str]) -> list[str]:
        targets = state.get("targets") or []
        assets = state.get("assets") or []
        sample_size = max(0, _safe_int(state.get("sample_size"), 0))
        primary = state.get("primary_result") or {}
        evidence = state.get("evidence") or []
        candidates: list[str] = []
        if targets and isinstance(targets[0], dict) and targets[0].get("bio") and "profile.read" not in completed:
            candidates.append("profile.read")
        if assets and "media.inspect" not in completed:
            candidates.append("media.inspect")
        if sample_size > 1 and "pattern.compare" not in completed:
            candidates.append("pattern.compare")
        if len(targets) > 1 and "peer.compare" not in completed:
            candidates.append("peer.compare")
        if sample_size > 1 and "stability.probe" not in completed:
            candidates.append("stability.probe")
        if "evidence.challenge" not in completed:
            candidates.append("evidence.challenge")
        if primary and evidence and "verdict.compose" not in completed:
            candidates.append("verdict.compose")
        return candidates

    @staticmethod
    def _action_need(
        action: str,
        uncertainty: float,
        boundary: float,
        instability: float,
        sample_support: float,
        evidence_support: float,
        asset_support: float,
        has_bio: float,
        peer_support: float,
        cautious: float,
    ) -> float:
        if action == "profile.read":
            return has_bio * max(uncertainty, boundary)
        if action == "media.inspect":
            return asset_support * max(uncertainty, boundary)
        if action == "pattern.compare":
            return sample_support * boundary
        if action == "peer.compare":
            return peer_support * max(uncertainty, boundary)
        if action == "stability.probe":
            return sample_support * max(instability, boundary, cautious)
        if action == "evidence.challenge":
            return max(uncertainty, boundary, cautious)
        if action == "verdict.compose":
            return (1.0 - uncertainty) * (1.0 - instability) * evidence_support * (1.0 - boundary)
        return 0.0

    @staticmethod
    def _reason(action: str) -> str:
        return {
            "profile.read": "历史经验判断主页信息在当前上下文仍有较高信息价值",
            "media.inspect": "历史经验判断多模态素材在当前上下文仍有较高信息价值",
            "pattern.compare": "历史经验判断跨内容模式对当前不确定性最有帮助",
            "peer.compare": "历史经验判断同批账号对当前判断具有较高比较价值",
            "stability.probe": "历史经验判断当前结论需要进一步做反事实稳定性检查",
            "evidence.challenge": "历史经验判断主动寻找反向证据更可能降低当前风险",
            "verdict.compose": "历史经验判断继续观察的边际收益已低于形成可复核判断",
        }.get(action, "根据历史调查经验选择下一项核查")
