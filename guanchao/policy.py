from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


@dataclass(slots=True)
class PolicyProfile:
    challenge_confidence: float = 0.76
    stability_confidence: float = 0.80
    min_pattern_posts: int = 3
    min_stability_posts: int = 4
    verdict_evidence_floor: int = 2
    cost_weight: float = 0.18
    caution_gain: float = 0.18

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "PolicyProfile":
        base = cls()
        if not raw:
            return base
        return cls(
            challenge_confidence=float(raw.get("challenge_confidence", base.challenge_confidence)),
            stability_confidence=float(raw.get("stability_confidence", base.stability_confidence)),
            min_pattern_posts=int(raw.get("min_pattern_posts", base.min_pattern_posts)),
            min_stability_posts=int(raw.get("min_stability_posts", base.min_stability_posts)),
            verdict_evidence_floor=int(raw.get("verdict_evidence_floor", base.verdict_evidence_floor)),
            cost_weight=_clip(float(raw.get("cost_weight", base.cost_weight)), 0.05, 0.45),
            caution_gain=_clip(float(raw.get("caution_gain", base.caution_gain)), 0.05, 0.35),
        )


@dataclass(slots=True)
class Decision:
    tool: str
    reason: str
    utility: float


class OwnedPolicy:
    """Information-gain controller with explicit observation cost.

    The old controller compared large hand-picked utility constants. This policy
    instead computes normalized expected information gain in [0, 1], subtracts
    an interpretable observation cost, and applies hard blockers only where a
    trustworthy verdict requires them.
    """

    _COST = {
        "profile.read": 0.14,
        "media.inspect": 0.34,
        "pattern.compare": 0.24,
        "peer.compare": 0.32,
        "stability.probe": 0.42,
        "evidence.challenge": 0.38,
        "verdict.compose": 0.08,
    }

    def __init__(self, profile: PolicyProfile | None = None):
        self.profile = profile or PolicyProfile()

    def decide(self, goal: str, state: dict[str, Any]) -> Decision | None:
        completed = set(state.get("completed_tools") or [])
        if "verdict.compose" in completed:
            return None
        if "workspace.inspect" not in completed:
            return Decision("workspace.inspect", "先确认资料、素材和缺口", 1.0)
        if "content.scan" not in completed:
            return Decision("content.scan", "先建立近期内容的基础判断", 1.0)

        targets = state.get("targets") or []
        sample_size = int(state.get("sample_size") or 0)
        assets = state.get("assets") or []
        ready_assets = sum(1 for a in assets if a.get("status") == "ready")
        primary = state.get("primary_result") or {}
        confidence = _clip(float(primary.get("confidence") or 0.0))
        marketing = _clip(float(primary.get("marketing_likelihood") or 0.5))
        stability = _clip(float(primary.get("stability") or 0.0))
        evidence_count = len({(e.get("key"), e.get("direction")) for e in state.get("evidence") or []})
        cautious = any(word in goal for word in ("误判", "反向", "谨慎", "仔细", "认真", "证据", "核实", "复核"))

        uncertainty = 1.0 - confidence
        boundary = 1.0 - min(1.0, abs(marketing - 0.5) * 2.0)
        sample_support = 1.0 - math.exp(-sample_size / 5.0)
        evidence_support = min(1.0, evidence_count / max(1, self.profile.verdict_evidence_floor + 1))
        caution = self.profile.caution_gain if cautious else 0.0

        candidates: list[Decision] = []

        def add(tool: str, gain: float, reason: str) -> None:
            if tool in completed:
                return
            cost = self._COST.get(tool, 0.2)
            utility = _clip(gain) - self.profile.cost_weight * cost
            candidates.append(Decision(tool, reason, utility))

        has_bio = bool(targets and targets[0].get("bio"))
        if has_bio:
            gain = 0.30 + 0.35 * uncertainty + 0.35 * boundary
            add("profile.read", gain, "主页信息能补充长期身份与经营背景")

        if assets:
            readiness = ready_assets / len(assets)
            gain = 0.34 + 0.36 * readiness + 0.30 * max(uncertainty, boundary)
            add("media.inspect", gain, "多模态素材能提供独立于文本的交叉证据")

        if sample_size >= self.profile.min_pattern_posts:
            gain = sample_support * (0.32 + 0.40 * boundary + 0.28 * uncertainty)
            add("pattern.compare", gain, "跨内容模式可以检验是否存在长期批量化行为")

        if len(targets) > 1:
            peer_support = 1.0 - math.exp(-(len(targets) - 1) / 4.0)
            gain = peer_support * (0.38 + 0.34 * boundary + 0.28 * uncertainty)
            add("peer.compare", gain, "同批账号提供相对背景，能减少孤立判断")

        stability_needed = (
            sample_size >= self.profile.min_stability_posts
            and (
                confidence < self.profile.stability_confidence
                or stability < 0.72
                or boundary > 0.42
                or cautious
            )
        )
        if stability_needed:
            gain = max(uncertainty, 1.0 - stability, 0.75 * boundary) + caution
            add("stability.probe", gain, "需要确认结论是否被少数内容主导")

        challenge_needed = (
            confidence < self.profile.challenge_confidence
            or boundary > 0.46
            or cautious
        )
        if challenge_needed:
            gain = max(uncertainty, boundary) + caution
            add("evidence.challenge", gain, "主动寻找能推翻当前倾向的证据")

        blockers: list[str] = []
        if assets and "media.inspect" not in completed:
            blockers.append("media.inspect")
        if has_bio and "profile.read" not in completed:
            blockers.append("profile.read")
        if cautious and sample_size >= self.profile.min_stability_posts and "stability.probe" not in completed:
            blockers.append("stability.probe")
        if cautious and "evidence.challenge" not in completed:
            blockers.append("evidence.challenge")

        if evidence_count >= self.profile.verdict_evidence_floor and not blockers:
            verdict_gain = confidence * (0.55 + 0.45 * stability) * (1.0 - 0.58 * boundary)
            verdict_gain *= 0.72 + 0.28 * evidence_support
            add("verdict.compose", verdict_gain, "当前证据覆盖与稳定性已达到成案条件")
        elif sample_size < 3 and "profile.read" in completed and not blockers:
            add("verdict.compose", 0.28, "资料有限，只形成带明确缺口的谨慎判断")

        if not candidates:
            return None
        return max(candidates, key=lambda item: item.utility)
