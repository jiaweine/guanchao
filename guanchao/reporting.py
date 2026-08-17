from __future__ import annotations

from typing import Any


class ReportBuilder:
    @staticmethod
    def _latest_completed(case: dict[str, Any]) -> dict[str, Any] | None:
        runs = case.get("runs") or []
        latest = runs[0] if runs else None
        return latest if latest and latest.get("status") == "completed" else None

    @classmethod
    def build_payload(cls, case: dict[str, Any]) -> dict[str, Any]:
        run = cls._latest_completed(case)
        state = (run or {}).get("state") or {}
        result = state.get("primary_result") or {}
        review = next(
            (item for item in case.get("reviews") or [] if run and item.get("run_id") == run.get("id")),
            None,
        )
        return {
            "title": case.get("title", "内容调查"),
            "case_id": case.get("id"),
            "goal": case.get("goal", ""),
            "owner": case.get("owner", "local"),
            "priority": case.get("priority", "normal"),
            "tags": case.get("tags") or [],
            "targets": case.get("targets") or [],
            "run_id": (run or {}).get("id"),
            "run_status": (run or {}).get("status"),
            "judgement": {
                "label": result.get("label"),
                "summary": result.get("summary"),
                "confidence": result.get("confidence"),
                "stability": result.get("stability"),
                "marketing_likelihood": result.get("marketing_likelihood"),
                "covert_promotion_risk": result.get("covert_promotion_risk"),
                "missing": result.get("missing") or [],
            },
            "evidence": state.get("evidence") or [],
            "review": review,
            "assets": case.get("assets") or [],
            "generated_from": "latest_completed_investigation",
        }

    @classmethod
    def build_markdown(cls, case: dict[str, Any]) -> str:
        payload = cls.build_payload(case)
        judgement = payload["judgement"]
        target = (payload["targets"] or [{}])[0]
        lines = [
            f"# {payload['title']}",
            "",
            f"- 调查编号：`{payload['case_id']}`",
            f"- 调查目标：{payload['goal'] or '—'}",
            f"- 账号：{target.get('display_name') or target.get('handle') or '—'}",
            f"- 平台：{target.get('platform') or '—'}",
            f"- 负责人：{payload['owner']}",
            "",
            "## 当前判断",
            "",
            f"**{judgement.get('label') or '尚未形成稳定判断'}**",
            "",
            judgement.get("summary") or "当前没有可导出的完整判断。",
            "",
        ]
        if judgement.get("confidence") is not None:
            lines.extend(
                [
                    f"- 把握度：{round(float(judgement['confidence']) * 100)}%",
                    f"- 稳定性：{round(float(judgement.get('stability') or 0) * 100)}%",
                    f"- 营销倾向：{round(float(judgement.get('marketing_likelihood') or 0) * 100)}%",
                    f"- 隐性推广风险：{round(float(judgement.get('covert_promotion_risk') or 0) * 100)}%",
                    "",
                ]
            )
        evidence = payload["evidence"]
        lines.extend(["## 关键证据", ""])
        if evidence:
            for item in evidence:
                direction = {"supports": "支持判断", "against": "反向线索", "context": "背景"}.get(
                    item.get("direction"), "背景"
                )
                lines.append(f"- **{item.get('title') or '证据'}**（{direction}）：{item.get('detail') or ''}")
        else:
            lines.append("- 暂无足够明确的关键证据。")
        missing = judgement.get("missing") or []
        lines.extend(["", "## 待补资料", ""])
        lines.extend([f"- {item}" for item in missing] or ["- 当前没有必须补充的资料。"])
        review = payload.get("review")
        lines.extend(["", "## 人工复核", ""])
        if review:
            label = {
                "confirm_ordinary": "普通创作者",
                "uncertain": "无法判断",
                "confirm_marketing": "营销运营",
            }.get(review.get("decision"), review.get("decision") or "已复核")
            lines.extend(
                [
                    f"- 复核结果：{label}",
                    f"- 复核人：{review.get('reviewer') or '—'}",
                    f"- 原因：{review.get('reason') or '—'}",
                    f"- 备注：{review.get('note') or '—'}",
                ]
            )
        else:
            lines.append("- 尚未完成人工复核。")
        lines.extend(
            [
                "",
                "---",
                "本报告用于辅助研判；涉及处置、处罚或事实认定时，应结合平台规则与人工复核。",
                "",
            ]
        )
        return "\n".join(lines)
