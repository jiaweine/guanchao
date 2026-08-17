from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Decision:
    tool: str
    reason: str


class OwnedPolicy:
    """Goal-aware deterministic controller for the investigation loop."""

    def decide(self, goal: str, state: dict) -> Decision | None:
        completed = set(state.get("completed_tools") or [])
        target_count = len(state.get("targets") or [])
        primary = state.get("primary_result") or {}
        confidence = float(primary.get("confidence") or 0.0)
        sample_size = int(state.get("sample_size") or 0)

        if "workspace.inspect" not in completed:
            return Decision("workspace.inspect", "先确认当前资料是否足以支持判断")
        if "profile.read" not in completed:
            return Decision("profile.read", "先看账号主页与基础身份线索")
        if "content.scan" not in completed:
            return Decision("content.scan", "扫描近期内容中的持续性营销线索")
        if sample_size >= 3 and "pattern.compare" not in completed:
            return Decision("pattern.compare", "检查是否存在批量模板、固定转化句式或异常节奏")
        if target_count > 1 and "peer.compare" not in completed:
            return Decision("peer.compare", "当前任务包含多个账号，先做同批对照")
        cautious_goal = any(word in goal for word in ("误判", "反向", "谨慎", "草率", "仔细核查", "认真核查"))
        if (confidence < 0.62 or cautious_goal) and sample_size >= 3 and "evidence.challenge" not in completed:
            return Decision("evidence.challenge", "当前任务需要更谨慎的结论，先做一次反向挑战")
        if "verdict.compose" not in completed:
            return Decision("verdict.compose", "证据已达到当前资料条件下的完成门槛")
        return None
