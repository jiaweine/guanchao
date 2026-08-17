from fastapi.testclient import TestClient

from guanchao.api import create_app
from guanchao.harness import RunCapacityError


def _case(client: TestClient):
    response = client.post(
        '/api/cases',
        json={
            'title': '状态一致性',
            'goal': '核查账号',
            'targets': [{
                'platform': 'weibo',
                'handle': 'state-check',
                'bio': '生活记录',
                'posts': [{'text': '今天散步'}, {'text': '周末做饭'}],
            }],
        },
    )
    assert response.status_code == 200
    return response.json()


def test_archived_case_rejects_source_refresh_and_new_assets(tmp_path, monkeypatch):
    monkeypatch.setenv('GUANCHAO_ASSET_DIR', str(tmp_path / 'assets'))
    client = TestClient(create_app(str(tmp_path / 'db.sqlite')))
    case = _case(client)
    archived = client.patch(f"/api/cases/{case['id']}", json={'archived': True})
    assert archived.status_code == 200

    target = dict(case['targets'][0])
    target['bio'] = '试图修改归档资料'
    refresh = client.patch(
        f"/api/cases/{case['id']}/target",
        json={'target': target, 'rerun': False},
    )
    upload = client.post(
        f"/api/cases/{case['id']}/assets",
        files={'file': ('late.txt', b'late evidence', 'text/plain')},
    )
    assert refresh.status_code == 409
    assert upload.status_code == 409


def test_running_case_rejects_asset_that_would_miss_current_evidence_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv('GUANCHAO_ASSET_DIR', str(tmp_path / 'assets'))
    app = create_app(str(tmp_path / 'db.sqlite'))
    client = TestClient(app)
    case = _case(client)
    monkeypatch.setattr(type(app.state.store), 'active_run_for_case', lambda self, case_id: {'id': 'busy'})
    response = client.post(
        f"/api/cases/{case['id']}/assets",
        files={'file': ('during-run.txt', b'new evidence', 'text/plain')},
    )
    assert response.status_code == 409
    assert '证据快照' in response.json()['detail']


def test_source_update_is_reported_as_saved_when_rerun_capacity_is_full(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / 'db.sqlite'))
    client = TestClient(app)
    case = _case(client)

    def capacity_full(*args, **kwargs):
        raise RunCapacityError('full')

    monkeypatch.setattr(app.state.harness, 'start', capacity_full)
    target = dict(case['targets'][0])
    target['bio'] = '已经保存的新资料'
    response = client.patch(
        f"/api/cases/{case['id']}/target",
        json={'target': target, 'rerun': True},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload['capacity_limited'] is True
    assert payload['run_id'] is None
    assert payload['case']['targets'][0]['bio'] == '已经保存的新资料'
    assert client.get(f"/api/cases/{case['id']}").json()['targets'][0]['bio'] == '已经保存的新资料'
