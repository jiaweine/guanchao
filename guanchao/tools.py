from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .detection import MarketingDetector
from .domain import AccountSnapshot, Evidence, ToolResult


@dataclass(slots=True)
class ToolSpec:
    name: str
    risk: str
    description: str
    handler: Callable[[dict], ToolResult]


class ToolRegistry:
    def __init__(self, detector: MarketingDetector):
        self.detector = detector
        self._tools: dict[str, ToolSpec] = {}
        self._register_defaults()

    def get(self, name: str) -> ToolSpec:
        return self._tools[name]

    def names(self) -> list[str]:
        return list(self._tools)

    def _register(self, name: str, risk: str, description: str, handler: Callable[[dict], ToolResult]) -> None:
        self._tools[name] = ToolSpec(name, risk, description, handler)

    def _register_defaults(self) -> None:
        self._register("workspace.inspect", "read", "Inspect provided account evidence", self._workspace)
        self._register("profile.read", "read", "Read profile-level commercial signals", self._profile)
        self._register("content.scan", "read", "Scan content signals", self._content)
        self._register("pattern.compare", "simulation", "Compare content patterns", self._patterns)
        self._register("peer.compare", "simulation", "Compare multiple accounts", self._peers)
        self._register("evidence.challenge", "simulation", "Challenge the current conclusion", self._challenge)
        self._register("verdict.compose", "read", "Compose evidence-backed verdict", self._verdict)

    @staticmethod
    def _primary(state: dict) -> AccountSnapshot:
        return AccountSnapshot.from_dict(state["targets"][0])

    def _workspace(self, state: dict) -> ToolResult:
        targets = [AccountSnapshot.from_dict(item) for item in state.get("targets") or []]
        if not targets:
            return ToolResult("workspace.inspect", False, "没有可检查的账号资料", error="empty_workspace")
        post_count = sum(len(t.posts) for t in targets)
        missing = []
        if post_count < 3:
            missing.append("近期内容不足")
        if all(not t.bio for t in targets):
            missing.append("缺少主页简介")
        return ToolResult(
            "workspace.inspect",
            True,
            f"已读取 {len(targets)} 个账号、{post_count} 条内容。",
            payload={"target_count": len(targets), "post_count": post_count, "missing": missing},
        )

    def _profile(self, state: dict) -> ToolResult:
        account = self._primary(state)
        features, evidence, _ = self.detector.extract(account)
        selected = [e for e in evidence if e.key in {"profile_commerciality", "contact_pressure"}]
        detail = "主页暂未出现强经营或导流线索。" if not selected else "主页中出现了可复核的经营或导流线索。"
        return ToolResult(
            "profile.read",
            True,
            detail,
            payload={
                "handle": account.handle,
                "display_name": account.display_name,
                "followers": account.followers,
                "verified": account.verified,
                "profile_commerciality": features.profile_commerciality,
            },
            evidence=selected,
        )

    def _content(self, state: dict) -> ToolResult:
        account = self._primary(state)
        result = self.detector.analyze(account)
        selected = [
            e
            for e in result.evidence
            if e.key in {"commercial_language", "call_to_action", "contact_pressure", "cross_post_pressure", "authentic_variation", "disclosure_signal"}
        ]
        return ToolResult(
            "content.scan",
            True,
            f"已检查 {len(account.posts)} 条近期内容，并整理持续性线索。",
            payload={
                "marketing_likelihood": result.marketing_likelihood,
                "covert_promotion_risk": result.covert_promotion_risk,
                "confidence": result.confidence,
                "features": result.features.asdict(),
                "missing": result.missing,
            },
            evidence=selected,
        )

    def _patterns(self, state: dict) -> ToolResult:
        account = self._primary(state)
        result = self.detector.analyze(account)
        selected = [e for e in result.evidence if e.key in {"template_reuse", "cadence_burst", "engagement_pattern"}]
        return ToolResult(
            "pattern.compare",
            True,
            "已比较内容结构、发布时间和互动形态。",
            payload={
                "template_reuse": result.features.template_reuse,
                "cadence_burst": result.features.cadence_burst,
                "engagement_pattern": result.features.engagement_pattern,
            },
            evidence=selected,
        )

    def _peers(self, state: dict) -> ToolResult:
        accounts = [AccountSnapshot.from_dict(item) for item in state.get("targets") or []]
        rows = []
        for account in accounts:
            result = self.detector.analyze(account)
            rows.append({
                "handle": account.handle,
                "label": result.label,
                "marketing_likelihood": result.marketing_likelihood,
                "confidence": result.confidence,
            })
        rows.sort(key=lambda x: x["marketing_likelihood"], reverse=True)
        return ToolResult("peer.compare", True, f"已完成 {len(rows)} 个账号的同批对照。", payload={"accounts": rows})

    def _challenge(self, state: dict) -> ToolResult:
        account = self._primary(state)
        result = self.detector.analyze(account)
        against = [e for e in result.evidence if e.direction == "against"]
        support = [e for e in result.evidence if e.direction == "supports"]
        if against:
            summary = "找到与当前营销判断相反的个人化表达，已纳入最终判断。"
        elif result.missing:
            summary = "没有找到足够强的反向线索，但资料缺口会降低最终把握度。"
        else:
            summary = "反向挑战未发现足以推翻当前判断的证据。"
        return ToolResult(
            "evidence.challenge",
            True,
            summary,
            payload={
                "supports": len(support),
                "against": len(against),
                "missing": result.missing,
                "confidence": result.confidence,
            },
            evidence=against[:3],
        )

    def _verdict(self, state: dict) -> ToolResult:
        account = self._primary(state)
        result = self.detector.analyze(account)
        return ToolResult(
            "verdict.compose",
            True,
            result.summary,
            payload={
                "label": result.label,
                "summary": result.summary,
                "marketing_likelihood": result.marketing_likelihood,
                "covert_promotion_risk": result.covert_promotion_risk,
                "confidence": result.confidence,
                "missing": result.missing,
                "features": result.features.asdict(),
            },
            evidence=result.evidence,
        )
