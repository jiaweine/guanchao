import asyncio
from threading import Event, Lock

import httpx
from fastapi.testclient import TestClient

from guanchao.api import create_app


def _target(handle: str) -> dict:
    return {
        'platform': 'weibo',
        'handle': handle,
        'bio': '生活记录',
        'posts': [{'text': '今天散步'}, {'text': '周末做饭'}],
    }


def _create_case(client: TestClient, handle: str) -> dict:
    response = client.post(
        '/api/cases',
        json={'title': f'{handle} · 内容调查', 'goal': '核查', 'targets': [_target(handle)]},
    )
    assert response.status_code == 200
    return response.json()


def test_slow_perception_does_not_block_unrelated_status_reads(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / 'db.sqlite'))
    case = _create_case(TestClient(app), 'slow-perception')
    entered = Event()
    release = Event()

    def slow_extract(path, kind, content_type):
        entered.set()
        assert release.wait(2), 'test did not release perception'
        return '可核对文本', 'ready'

    monkeypatch.setattr(app.state.perception, 'extract', slow_extract)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            upload = asyncio.create_task(
                client.post(
                    f"/api/cases/{case['id']}/assets",
                    files={'file': ('image.jpg', b'fake-image', 'image/jpeg')},
                )
            )
            assert await asyncio.to_thread(entered.wait, 1), 'perception did not start'
            status = await asyncio.wait_for(client.get('/api/status'), timeout=0.5)
            assert status.status_code == 200
            assert not upload.done()
            release.set()
            uploaded = await asyncio.wait_for(upload, timeout=2)
            assert uploaded.status_code == 200
            assert uploaded.json()['status'] == 'ready'

    asyncio.run(scenario())


def test_run_start_waits_until_same_case_perception_finishes(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / 'db.sqlite'))
    case = _create_case(TestClient(app), 'snapshot-order')
    perception_entered = Event()
    release_perception = Event()
    run_entered = Event()

    def slow_extract(path, kind, content_type):
        perception_entered.set()
        assert release_perception.wait(2), 'test did not release perception'
        return '素材事实', 'ready'

    def observed_start(*args, **kwargs):
        run_entered.set()
        return 'run-after-assets'

    monkeypatch.setattr(app.state.perception, 'extract', slow_extract)
    monkeypatch.setattr(app.state.harness, 'start', observed_start)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            upload = asyncio.create_task(
                client.post(
                    f"/api/cases/{case['id']}/assets",
                    files={'file': ('clip.mp4', b'fake-video', 'video/mp4')},
                )
            )
            assert await asyncio.to_thread(perception_entered.wait, 1)
            run = asyncio.create_task(
                client.post(f"/api/cases/{case['id']}/messages", json={'content': '开始核查'})
            )
            await asyncio.sleep(0.12)
            assert not run_entered.is_set(), 'run started before asset perception settled'
            release_perception.set()
            uploaded, started = await asyncio.wait_for(asyncio.gather(upload, run), timeout=2)
            assert uploaded.status_code == 200
            assert started.status_code == 200
            assert started.json()['run_id'] == 'run-after-assets'
            assert run_entered.is_set()

    asyncio.run(scenario())


def test_perception_concurrency_is_bounded_across_cases(tmp_path, monkeypatch):
    monkeypatch.setenv('GUANCHAO_PERCEPTION_WORKERS', '1')
    app = create_app(str(tmp_path / 'db.sqlite'))
    setup = TestClient(app)
    first = _create_case(setup, 'bounded-a')
    second = _create_case(setup, 'bounded-b')
    release = Event()
    first_entered = Event()
    counter_lock = Lock()
    active = 0
    max_active = 0

    def slow_extract(path, kind, content_type):
        nonlocal active, max_active
        with counter_lock:
            active += 1
            max_active = max(max_active, active)
            first_entered.set()
        assert release.wait(2), 'test did not release bounded perception'
        with counter_lock:
            active -= 1
        return '事实', 'ready'

    monkeypatch.setattr(app.state.perception, 'extract', slow_extract)

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            one = asyncio.create_task(
                client.post(f"/api/cases/{first['id']}/assets", files={'file': ('a.jpg', b'a', 'image/jpeg')})
            )
            two = asyncio.create_task(
                client.post(f"/api/cases/{second['id']}/assets", files={'file': ('b.jpg', b'b', 'image/jpeg')})
            )
            assert await asyncio.to_thread(first_entered.wait, 1)
            await asyncio.sleep(0.12)
            assert max_active == 1
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(one, two), timeout=2)
            assert all(item.status_code == 200 for item in responses)
            assert max_active == 1

    asyncio.run(scenario())
