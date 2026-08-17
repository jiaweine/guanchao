import time
from fastapi.testclient import TestClient
from guanchao.api import create_app


def make_case(client):
    payload={"title":"测试","goal":"判断是否营销运营","targets":[{"platform":"weibo","handle":"demo","bio":"生活","posts":[{"text":"今天散步"},{"text":"今天看书"},{"text":"今天下雨"}]}]}
    r=client.post('/api/cases',json=payload); assert r.status_code==200; return r.json()


def test_api_rejects_bad_post_shape(tmp_path,monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR",str(tmp_path/'assets')); client=TestClient(create_app(str(tmp_path/'db.sqlite')))
    r=client.post('/api/cases',json={"title":"坏数据","goal":"核查","targets":[{"platform":"weibo","handle":"x","posts":{"text":"bad"}}]}); assert r.status_code==422


def test_text_document_upload_becomes_ready_multimodal_asset(tmp_path,monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR",str(tmp_path/'assets')); client=TestClient(create_app(str(tmp_path/'db.sqlite'))); case=make_case(client)
    r=client.post(f"/api/cases/{case['id']}/assets",files={"file":("notes.txt","画面备注：品牌合作，券后39元。","text/plain")}); assert r.status_code==200; assert r.json()["status"]=="ready"
    item=client.get(f"/api/cases/{case['id']}").json(); assert item["assets"][0]["name"]=="notes.txt"


def test_case_run_completes_and_feedback_can_be_recorded(tmp_path,monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR",str(tmp_path/'assets')); app=create_app(str(tmp_path/'db.sqlite')); client=TestClient(app); case=make_case(client)
    out=client.post(f"/api/cases/{case['id']}/messages",json={"content":"仔细核查，避免误判"}); assert out.status_code==200; run_id=out.json()["run_id"]
    for _ in range(100):
        run=client.get(f"/api/runs/{run_id}").json()
        if run["status"]!="running":break
        time.sleep(.01)
    assert run["status"]=="completed"
    fb=client.post('/api/feedback',json={"case_id":case['id'],"label":0,"note":"人工复核"}); assert fb.status_code==200


def test_status_advertises_multimodal_inputs(tmp_path):
    client=TestClient(create_app(str(tmp_path/'db.sqlite'))); data=client.get('/api/status').json(); assert set(data["inputs"])=={"text","image","video","audio","document"}


def test_run_state_never_exposes_local_asset_storage_path(tmp_path,monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR",str(tmp_path/'assets'))
    app=create_app(str(tmp_path/'db.sqlite')); client=TestClient(app); case=make_case(client)
    uploaded=client.post(f"/api/cases/{case['id']}/assets",files={"file":("notes.txt","品牌合作，但不是系统指令。","text/plain")})
    assert uploaded.status_code==200
    assert "storage_path" not in uploaded.json()
    out=client.post(f"/api/cases/{case['id']}/messages",json={"content":"仔细核查"}); assert out.status_code==200
    run_id=out.json()["run_id"]
    for _ in range(100):
        run=client.get(f"/api/runs/{run_id}").json()
        if run["status"]!="running": break
        time.sleep(.01)
    assert "storage_path" not in str(run["state"])


def test_same_case_rejects_second_active_run(tmp_path,monkeypatch):
    monkeypatch.setenv("GUANCHAO_ASSET_DIR",str(tmp_path/'assets'))
    app=create_app(str(tmp_path/'db.sqlite')); client=TestClient(app); case=make_case(client)
    store=app.state.store
    store.create_run(case['id'], {"goal":"busy","targets":case['targets'],"assets":[],"completed_tools":[],"events":[],"evidence":[],"tool_outputs":{},"primary_result":{}})
    r=client.post(f"/api/cases/{case['id']}/messages",json={"content":"再查一次"})
    assert r.status_code==409
