import asyncio
from threading import Event, Lock

import httpx
from fastapi.testclient import TestClient
import pytest

from guanchao.api import create_app


def _target(handle: str) -> dict:
    return {
        'platform': 'weibo',
        'handle': handle,
        'bio': '生活记录',
        'posts': [{'text': '今天散步'}, {'text': '周末做饭'}],
    }


def _case(client: TestClient, handle: str) -> dict:
    response = client.post(
        '/api/cases',
        json={'title': f'{handle} · 内容调查', 'goal': '核查', 'targets': [_target(handle)]},
    )
    assert response.status_code == 200
    return response.json()


def test_cancelled_upload_finishes_asset_settlement_before_same_case_run_can_start(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / 'db.sqlite'))
    case = _case(TestClient(app), 'cancel-snapshot')
    perception_entered = Event()
    release_perception = Event()
    run_entered = Event()

    def slow_extract(path, kind, content_type):
        perception_entered.set()
        assert release_perception.wait(2), 'test did not release perception'
        return '已提取事实', 'ready'

    def observed_start(*args, **kwargs):
        run_entered.set()
        return 'run-after-cancelled-upload'

    monkeypatch.setattr(app.state.perception, 'extract', slow_extract)
    monkeypatch.setattr(app.state.harness, 'start', observed_start)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            upload = asyncio.create_task(
                client.post(
                    f"/api/cases/{case['id']}/assets",
                    files={'file': ('slow.jpg', b'fake-image', 'image/jpeg')},
                )
            )
            assert await asyncio.to_thread(perception_entered.wait, 1), 'perception did not start'
            upload.cancel()
            await asyncio.sleep(0.05)
            assert not upload.done(), 'cancelled request released before committed asset settlement finished'

            run = asyncio.create_task(
                client.post(f"/api/cases/{case['id']}/messages", json={'content': '开始核查'})
            )
            await asyncio.sleep(0.12)
            assert not run_entered.is_set(), 'same-case run crossed a cancelled but unfinished asset settlement'

            release_perception.set()
            with pytest.raises(asyncio.CancelledError):
                await upload
            started = await asyncio.wait_for(run, timeout=2)
            assert started.status_code == 200
            assert started.json()['run_id'] == 'run-after-cancelled-upload'

            snapshot = (await client.get(f"/api/cases/{case['id']}")).json()
            assert len(snapshot['assets']) == 1
            assert snapshot['assets'][0]['status'] == 'ready'

    asyncio.run(scenario())


def test_cancelled_upload_does_not_release_perception_capacity_for_another_case(tmp_path, monkeypatch):
    monkeypatch.setenv('GUANCHAO_PERCEPTION_WORKERS', '1')
    app = create_app(str(tmp_path / 'db.sqlite'))
    setup = TestClient(app)
    first = _case(setup, 'cancel-slot-a')
    second = _case(setup, 'cancel-slot-b')

    first_entered = Event()
    second_entered = Event()
    release_first = Event()
    counter_lock = Lock()
    calls = 0

    def slow_extract(path, kind, content_type):
        nonlocal calls
        with counter_lock:
            calls += 1
            index = calls
        if index == 1:
            first_entered.set()
            assert release_first.wait(2), 'test did not release first perception'
        else:
            second_entered.set()
        return '事实', 'ready'

    monkeypatch.setattr(app.state.perception, 'extract', slow_extract)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            one = asyncio.create_task(
                client.post(f"/api/cases/{first['id']}/assets", files={'file': ('a.jpg', b'a', 'image/jpeg')})
            )
            assert await asyncio.to_thread(first_entered.wait, 1)
            one.cancel()

            two = asyncio.create_task(
                client.post(f"/api/cases/{second['id']}/assets", files={'file': ('b.jpg', b'b', 'image/jpeg')})
            )
            await asyncio.sleep(0.12)
            assert not second_entered.is_set(), 'cancelled request released the bounded perception slot too early'

            release_first.set()
            with pytest.raises(asyncio.CancelledError):
                await one
            second_response = await asyncio.wait_for(two, timeout=2)
            assert second_response.status_code == 200
            assert second_entered.is_set()

    asyncio.run(scenario())
