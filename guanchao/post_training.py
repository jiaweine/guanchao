from __future__ import annotations

import json
from typing import Any


class PostTrainingCorpusBuilder:
    """Build harness-aware SFT/preference records from verified trajectories without coupling the runtime to a trainer."""

    def build_jsonl(self, cases: list[dict[str, Any]], feedback: list[dict[str, Any]]) -> str:
        labels={row["case_id"]:int(row["label"]) for row in feedback}
        records=[]
        for case in cases:
            if case["id"] not in labels: continue
            completed=next((r for r in case.get("runs",[]) if r["status"]=="completed"),None)
            if not completed: continue
            state=completed["state"]
            record={
                "task":"social_account_investigation",
                "goal":state.get("goal",case.get("goal","")),
                "observations":{"targets":state.get("targets",[]),"assets":[{k:v for k,v in a.items() if k!="storage_path"} for a in state.get("assets",[])]},
                "trajectory":[{"kind":e.get("kind"),"tool":e.get("tool"),"status":e.get("status"),"detail":e.get("detail")} for e in state.get("events",[])],
                "answer":state.get("answer",""),
                "human_label":labels[case["id"]],
                "reward":1,
            }
            records.append(json.dumps(record,ensure_ascii=False))
        return "\n".join(records)+("\n" if records else "")
