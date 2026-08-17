from __future__ import annotations

import math
from .domain import ToolResult


class ResultVerifier:
    def verify(self, result: ToolResult) -> tuple[bool, str]:
        if not result.ok:
            return False, result.error or "步骤没有成功完成"
        for key, value in result.payload.items():
            if isinstance(value, float) and not math.isfinite(value):
                return False, f"{key} 出现无效数值"
            if key in {"marketing_likelihood", "covert_promotion_risk", "confidence", "stability"} and isinstance(value, (int, float)):
                if not 0.0 <= float(value) <= 1.0:
                    return False, f"{key} 超出有效范围"
        for evidence in result.evidence:
            if not math.isfinite(evidence.strength) or not 0.0 <= evidence.strength <= 1.0:
                return False, "证据强度无效"
        if result.tool == "verdict.compose":
            required = {"label", "summary", "marketing_likelihood", "confidence", "stability"}
            if not required.issubset(result.payload):
                return False, "最终判断缺少必要字段"
        return True, "ok"
