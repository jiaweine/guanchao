import json
from guanchao.post_training import PostTrainingCorpusBuilder


def test_post_training_export_contains_verified_trajectory():
    cases=[{"id":"c1","goal":"核查","runs":[{"status":"completed","state":{"goal":"核查","targets":[{"handle":"a"}],"assets":[],"events":[{"kind":"tool","tool":"content.scan","status":"done","detail":"完成"}],"answer":"判断"}}]}]
    text=PostTrainingCorpusBuilder().build_jsonl(cases,[{"case_id":"c1","label":1}]); row=json.loads(text.strip()); assert row["human_label"]==1 and row["trajectory"][0]["tool"]=="content.scan"
