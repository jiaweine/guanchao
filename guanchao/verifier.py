from __future__ import annotations

import math

from .domain import ToolResult


class ResultVerifier:
    def verify(self, result: ToolResult) -> tuple[bool, str]:
        if not result.ok:
            return False, result.error or "工具执行失败"
        for key in ("marketing_likelihood", "covert_promotion_risk", "confidence"):
            if key not in result.payload:
                continue
            value = result.payload[key]
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                return False, f"{key} 不是有效数值"
            if not 0.0 <= float(value) <= 1.0:
                return False, f"{key} 超出预期范围"
        if result.tool == "verdict.compose":
            if not result.payload.get("label") or not result.payload.get("summary"):
                return False, "最终判断缺少必要字段"
        return True, "ok"
