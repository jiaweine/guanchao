import asyncio
from pathlib import Path
from threading import Event

import httpx
from fastapi.testclient import TestClient

import guanchao.api as api_module
from guanchao.api import create_app
from guanchao.harness import RunCapacityError


def _target(handle: str) -> dict:
    return {
        'platform': 'weibo',
        'handle': handle,
        'bio': '生活记录',
        'posts': [{'text': '今天散步'}, {'text': '周末做饭'}],
    }


def _case(client: TestClient):
    response = client.post(
        '/api/cases',
        json={'title': '状态一致性', 'goal': '核查账号', 'targets': [_target('state-check')]},
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


def test_case_mutation_lock_prevents_asset_and_run_start_from_crossing_snapshots(tmp_path, monkeypatch):
    asset_dir = tmp_path / 'assets'
    monkeypatch.setenv('GUANCHAO_ASSET_DIR', str(asset_dir))
    app = create_app(str(tmp_path / 'db.sqlite'))
    setup_client = TestClient(app)
    case = _case(setup_client)

    run_entered = Event()
    release_run = Event()
    asset_entered = Event()
    original_create_asset = app.state.store.create_asset

    def slow_start(*args, **kwargs):
        run_entered.set()
        assert release_run.wait(2), 'test did not release run start'
        return 'serialized-run'

    def observed_create_asset(*args, **kwargs):
        asset_entered.set()
        return original_create_asset(*args, **kwargs)

    monkeypatch.setattr(app.state.harness, 'start', slow_start)
    monkeypatch.setattr(app.state.store, 'create_asset', observed_create_asset)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            run_task = asyncio.create_task(
                client.post(f"/api/cases/{case['id']}/messages", json={'content': '开始核查'})
            )
            assert await asyncio.to_thread(run_entered.wait, 1), 'run request did not enter harness'
            asset_task = asyncio.create_task(
                client.post(
                    f"/api/cases/{case['id']}/assets",
                    files={'file': ('serialized.txt', b'evidence', 'text/plain')},
                )
            )
            await asyncio.sleep(0.12)
            assert not asset_entered.is_set(), 'asset mutation crossed an in-flight run start'
            release_run.set()
            run_response, asset_response = await asyncio.wait_for(
                asyncio.gather(run_task, asset_task), timeout=2
            )
            assert run_response.status_code == 200
            assert asset_response.status_code == 200
            assert asset_entered.is_set()

    asyncio.run(scenario())


def test_case_creation_is_idempotent_for_same_actor_and_request_key(tmp_path):
    client = TestClient(create_app(str(tmp_path / 'db.sqlite')))
    payload = {'title': '幂等建案', 'goal': '核查', 'targets': [_target('idem-one')]}
    headers = {'X-Guanchao-Request-Key': 'case-retry-1'}

    first = client.post('/api/cases', headers=headers, json=payload)
    second = client.post('/api/cases', headers=headers, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()['id'] == second.json()['id']
    cases = client.get('/api/cases?status=all').json()
    assert len(cases) == 1


def test_batch_creation_retry_replays_same_batch_instead_of_duplicating_cases(tmp_path):
    client = TestClient(create_app(str(tmp_path / 'db.sqlite')))
    payload = {
        'title': '幂等批次',
        'goal': '批量核查',
        'targets': [_target('idem-a'), _target('idem-b')],
        'auto_start': False,
    }
    headers = {'X-Guanchao-Request-Key': 'batch-retry-1'}

    first = client.post('/api/cases/batch', headers=headers, json=payload)
    second = client.post('/api/cases/batch', headers=headers, json=payload)
    assert first.status_code == second.status_code == 200
    first_data, second_data = first.json(), second.json()
    assert first_data['batch']['id'] == second_data['batch']['id']
    assert [item['id'] for item in first_data['cases']] == [item['id'] for item in second_data['cases']]
    assert len(client.get('/api/cases?status=all').json()) == 2


def test_idempotency_key_is_namespaced_by_trusted_actor(tmp_path, monkeypatch):
    monkeypatch.setenv('GUANCHAO_TRUST_ACTOR_HEADER', '1')
    client = TestClient(create_app(str(tmp_path / 'db.sqlite')))
    assert client.post('/api/members', json={'id': 'alice', 'display_name': 'Alice', 'role': 'analyst'}).status_code == 200
    payload = {'title': '不同成员', 'goal': '核查', 'targets': [_target('actor-key')]}
    key = {'X-Guanchao-Request-Key': 'shared-client-key'}
    local = client.post('/api/cases', headers=key, json=payload)
    alice = client.post('/api/cases', headers={**key, 'X-Guanchao-Actor': 'alice'}, json=payload)
    assert local.status_code == alice.status_code == 200
    assert local.json()['id'] != alice.json()['id']
