from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class PolicyProfile:
    challenge_confidence: float = 0.74
    stability_confidence: float = 0.78
    min_pattern_posts: int = 3
    min_stability_posts: int = 4
    verdict_evidence_floor: int = 2

    def to_dict(self) -> dict[str, Any]: return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "PolicyProfile":
        base = cls()
        if not raw: return base
        return cls(
            challenge_confidence=float(raw.get("challenge_confidence", base.challenge_confidence)),
            stability_confidence=float(raw.get("stability_confidence", base.stability_confidence)),
            min_pattern_posts=int(raw.get("min_pattern_posts", base.min_pattern_posts)),
            min_stability_posts=int(raw.get("min_stability_posts", base.min_stability_posts)),
            verdict_evidence_floor=int(raw.get("verdict_evidence_floor", base.verdict_evidence_floor)),
        )


@dataclass(slots=True)
class Decision:
    tool: str
    reason: str
    utility: float


class OwnedPolicy:
    """Evidence-gain controller: choose the next tool by expected information value, not a fixed script."""

    def __init__(self, profile: PolicyProfile | None = None):
        self.profile = profile or PolicyProfile()

    def decide(self, goal: str, state: dict[str, Any]) -> Decision | None:
        completed = set(state.get("completed_tools") or [])
        if "verdict.compose" in completed: return None
        targets = state.get("targets") or []
        sample_size = int(state.get("sample_size") or 0)
        asset_count = len(state.get("assets") or [])
        ready_assets = sum(1 for a in state.get("assets") or [] if a.get("status") == "ready")
        primary = state.get("primary_result") or {}
        confidence = float(primary.get("confidence") or 0.0)
        marketing = float(primary.get("marketing_likelihood") or 0.5)
        stability = float(primary.get("stability") or 0.0)
        evidence_count = len({(e.get("key"), e.get("direction")) for e in state.get("evidence") or []})
        cautious = any(word in goal for word in ("误判", "反向", "谨慎", "仔细", "认真", "证据", "核实", "复核"))
        batch = len(targets) > 1
        uncertainty = 1.0 - confidence
        boundary = 1.0 - min(1.0, abs(marketing - 0.5) * 2.0)

        candidates: list[Decision] = []
        def add(tool: str, utility: float, reason: str) -> None:
            if tool not in completed: candidates.append(Decision(tool, reason, utility))

        add("workspace.inspect", 100.0, "先确认资料、素材和缺口")
        if "workspace.inspect" in completed:
            add("content.scan", 92.0, "先建立近期内容的基础判断")
            add("profile.read", 78.0 + (4.0 if not state.get("tool_outputs", {}).get("profile.read") else 0.0), "补齐主页长期身份线索")
            if asset_count:
                add("media.inspect", 84.0 + ready_assets * 2.0, "核对图片、视频、音频和文档里的可见线索")
        if "content.scan" in completed:
            if sample_size >= self.profile.min_pattern_posts:
                add("pattern.compare", 54.0 + 22.0 * boundary, "检查批量模板、固定句式和发布时间模式")
            if batch:
                add("peer.compare", 61.0 + min(12.0, len(targets) * 1.5), "利用同批账号建立相对参照")
            if sample_size >= self.profile.min_stability_posts and (confidence < self.profile.stability_confidence or cautious or stability < 0.72):
                add("stability.probe", 64.0 + 28.0 * uncertainty + (8.0 if cautious else 0.0), "测试结论是否被少数内容主导")
            if confidence < self.profile.challenge_confidence or cautious or boundary > 0.55:
                add("evidence.challenge", 62.0 + 24.0 * max(uncertainty, boundary) + (8.0 if cautious else 0.0), "主动寻找能够推翻当前判断的线索")
            blockers = [t for t in ("media.inspect" if asset_count else None,) if t and t not in completed]
            if state.get("targets", [{}])[0].get("bio") and "profile.read" not in completed:
                blockers.append("profile.read")
            if cautious and sample_size >= self.profile.min_stability_posts and "stability.probe" not in completed:
                blockers.append("stability.probe")
            if cautious and "evidence.challenge" not in completed:
                blockers.append("evidence.challenge")
            if evidence_count >= self.profile.verdict_evidence_floor and not blockers:
                add("verdict.compose", 35.0 + 48.0 * confidence + 18.0 * (1.0 - boundary), "当前证据已达到成案门槛")
            elif sample_size < 3 and "profile.read" in completed:
                add("verdict.compose", 28.0, "资料有限，形成带明确缺口的谨慎判断")

        if not candidates: return None
        return max(candidates, key=lambda item: item.utility)
