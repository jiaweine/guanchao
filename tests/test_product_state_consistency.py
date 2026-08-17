from pathlib import Path

from fastapi.testclient import TestClient

import guanchao.api as api_module
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


def _upload(client: TestClient, case_id: str, name: str = 'evidence.txt') -> dict:
    response = client.post(
        f'/api/cases/{case_id}/assets',
        files={'file': (name, b'new evidence', 'text/plain')},
    )
    assert response.status_code == 200
    return response.json()


def test_archived_case_rejects_source_refresh_and_all_asset_mutations(tmp_path, monkeypatch):
    asset_dir = tmp_path / 'assets'
    monkeypatch.setenv('GUANCHAO_ASSET_DIR', str(asset_dir))
    client = TestClient(create_app(str(tmp_path / 'db.sqlite')))
    case = _case(client)
    asset = _upload(client, case['id'])
    files_before = list(Path(asset_dir).iterdir())
    assert files_before

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
    deleted = client.delete(f"/api/cases/{case['id']}/assets/{asset['id']}")
    assert refresh.status_code == 409
    assert upload.status_code == 409
    assert deleted.status_code == 409
    assert list(Path(asset_dir).iterdir()) == files_before


def test_running_case_rejects_asset_mutations_that_change_the_evidence_snapshot(tmp_path, monkeypatch):
    asset_dir = tmp_path / 'assets'
    monkeypatch.setenv('GUANCHAO_ASSET_DIR', str(asset_dir))
    app = create_app(str(tmp_path / 'db.sqlite'))
    client = TestClient(app)
    case = _case(client)
    asset = _upload(client, case['id'])
    files_before = list(Path(asset_dir).iterdir())

    monkeypatch.setattr(type(app.state.store), 'active_run_for_case', lambda self, case_id: {'id': 'busy'})
    upload = client.post(
        f"/api/cases/{case['id']}/assets",
        files={'file': ('during-run.txt', b'new evidence', 'text/plain')},
    )
    deleted = client.delete(f"/api/cases/{case['id']}/assets/{asset['id']}")
    assert upload.status_code == 409
    assert deleted.status_code == 409
    assert '证据快照' in upload.json()['detail']
    assert list(Path(asset_dir).iterdir()) == files_before


def test_single_asset_delete_removes_database_record_and_file(tmp_path, monkeypatch):
    asset_dir = tmp_path / 'assets'
    monkeypatch.setenv('GUANCHAO_ASSET_DIR', str(asset_dir))
    client = TestClient(create_app(str(tmp_path / 'db.sqlite')))
    case = _case(client)
    asset = _upload(client, case['id'])
    assert list(Path(asset_dir).iterdir())

    response = client.delete(f"/api/cases/{case['id']}/assets/{asset['id']}")
    assert response.status_code == 200
    assert client.get(f"/api/cases/{case['id']}").json()['assets'] == []
    assert list(Path(asset_dir).iterdir()) == []


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


def test_read_hot_path_does_not_invoke_extra_wrapper_member_lookup(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / 'db.sqlite'))
    client = TestClient(app)

    def forbidden_lookup(app, request):
        raise AssertionError('wrapper member lookup leaked into read hot path')

    monkeypatch.setattr(api_module, '_request_member', forbidden_lookup)
    response = client.get('/api/status')
    assert response.status_code == 200
