from __future__ import annotations

import json
from typing import Any


class PostTrainingCorpusBuilder:
    """Build reviewed investigation trajectories without coupling runtime code to a trainer."""

    def build_jsonl(self, cases: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> str:
        review_by_run = {
            row["run_id"]: row
            for row in reviews
            if row.get("run_id") and row.get("decision") in {"confirm_ordinary", "confirm_marketing"}
        }
        records: list[str] = []

        for case in cases:
            for run in case.get("runs") or []:
                if run.get("status") != "completed":
                    continue
                review = review_by_run.get(run.get("id"))
                if not review:
                    continue
                state = run.get("state") or {}
                decision = review["decision"]
                record = {
                    "task": "social_account_investigation",
                    "case_id": case.get("id"),
                    "run_id": run.get("id"),
                    "goal": state.get("goal", case.get("goal", "")),
                    "observations": {
                        "targets": state.get("targets", []),
                        "assets": [
                            {key: value for key, value in asset.items() if key != "storage_path"}
                            for asset in state.get("assets", [])
                        ],
                    },
                    "trajectory": [
                        {
                            "kind": event.get("kind"),
                            "tool": event.get("tool"),
                            "status": event.get("status"),
                            "detail": event.get("detail"),
                        }
                        for event in state.get("events", [])
                    ],
                    "answer": state.get("answer", ""),
                    "human_label": 1 if decision == "confirm_marketing" else 0,
                    "review": {
                        "decision": decision,
                        "reason": review.get("reason", ""),
                        "note": review.get("note", ""),
                    },
                }
                records.append(json.dumps(record, ensure_ascii=False))

        return "\n".join(records) + ("\n" if records else "")
