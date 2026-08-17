import json

from guanchao.post_training import PostTrainingCorpusBuilder


def test_post_training_export_binds_review_to_exact_run():
    cases = [
        {
            "id": "c1",
            "goal": "核查",
            "runs": [
                {
                    "id": "r2",
                    "status": "completed",
                    "state": {
                        "goal": "第二次核查",
                        "targets": [{"handle": "a"}],
                        "assets": [],
                        "events": [{"kind": "tool", "tool": "content.scan", "status": "done", "detail": "完成"}],
                        "answer": "第二次判断",
                    },
                },
                {
                    "id": "r1",
                    "status": "completed",
                    "state": {
                        "goal": "第一次核查",
                        "targets": [{"handle": "a"}],
                        "assets": [],
                        "events": [],
                        "answer": "第一次判断",
                    },
                },
            ],
        }
    ]
    reviews = [
        {"case_id": "c1", "run_id": "r2", "decision": "confirm_marketing", "reason": "证据充分", "note": ""},
        {"case_id": "c1", "run_id": "r1", "decision": "uncertain", "reason": "资料不足", "note": ""},
    ]
    rows = [json.loads(line) for line in PostTrainingCorpusBuilder().build_jsonl(cases, reviews).splitlines()]
    assert len(rows) == 1
    assert rows[0]["run_id"] == "r2"
    assert rows[0]["human_label"] == 1
    assert rows[0]["trajectory"][0]["tool"] == "content.scan"
    assert rows[0]["review"]["reason"] == "证据充分"
