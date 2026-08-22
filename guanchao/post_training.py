from __future__ import annotations

import json
from typing import Any


class PostTrainingCorpusBuilder:
    """Build reviewed investigation trajectories without coupling runtime code to a trainer."""

    @staticmethod
    def _record(
        case_id: str,
        case_goal: str,
        run_id: str,
        state: dict[str, Any],
        review: dict[str, Any],
    ) -> dict[str, Any]:
        decision = str(review.get("decision") or "")
        return {
            "task": "social_account_investigation",
            "case_id": case_id,
            "run_id": run_id,
            "goal": state.get("goal", case_goal),
            "observations": {
                "targets": state.get("targets", []),
                "assets": [
                    {key: value for key, value in asset.items() if key != "storage_path"}
                    for asset in state.get("assets", [])
                    if isinstance(asset, dict)
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
                if isinstance(event, dict)
            ],
            "answer": state.get("answer", ""),
            "human_label": 1 if decision == "confirm_marketing" else 0,
            "review": {
                "decision": decision,
                "reason": review.get("reason", ""),
                "note": review.get("note", ""),
            },
        }

    @staticmethod
    def _json_line(record: dict[str, Any]) -> str:
        return json.dumps(record, ensure_ascii=False, allow_nan=False)

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
                if not isinstance(state, dict):
                    continue
                records.append(
                    self._json_line(
                        self._record(
                            str(case.get("id") or ""),
                            str(case.get("goal") or ""),
                            str(run.get("id") or ""),
                            state,
                            review,
                        )
                    )
                )

        return "\n".join(records) + ("\n" if records else "")

    def build_jsonl_from_store(self, store: Any) -> str:
        """Export only decisive reviewed completed runs with one DB round trip.

        The legacy API enumerated every case and called Store.get_case() for each,
        materializing messages, comments, every historical run, assets and reviews
        even though the corpus only consumes the reviewed run snapshot. This join
        keeps export cost proportional to usable supervision, not workspace size.
        """
        with store._lock:
            conn = store._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT c.id AS case_id,
                           c.goal AS case_goal,
                           r.id AS run_id,
                           r.state_json,
                           rv.decision,
                           rv.reason,
                           rv.note
                    FROM reviews rv
                    JOIN runs r ON r.id = rv.run_id
                    JOIN cases c ON c.id = rv.case_id
                    WHERE rv.decision IN ('confirm_ordinary', 'confirm_marketing')
                      AND r.status = 'completed'
                    ORDER BY c.created_at, c.id, r.created_at, r.id
                    """
                ).fetchall()
            finally:
                store._close(conn)

        records: list[str] = []
        for row in rows:
            try:
                state = json.loads(row["state_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(state, dict):
                continue
            review = {
                "decision": row["decision"],
                "reason": row["reason"],
                "note": row["note"],
            }
            try:
                records.append(
                    self._json_line(
                        self._record(
                            str(row["case_id"] or ""),
                            str(row["case_goal"] or ""),
                            str(row["run_id"] or ""),
                            state,
                            review,
                        )
                    )
                )
            except (TypeError, ValueError):
                # Corrupt historical numeric payloads must not take the whole
                # export endpoint down; skip only the unusable supervision row.
                continue
        return "\n".join(records) + ("\n" if records else "")
