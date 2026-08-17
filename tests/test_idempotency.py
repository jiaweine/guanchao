from fastapi.testclient import TestClient

from guanchao.api import create_app


def _target(handle: str) -> dict:
    return {
        'platform': 'weibo',
        'handle': handle,
        'bio': '生活记录',
        'posts': [{'text': '今天散步'}, {'text': '周末做饭'}],
    }


def test_same_idempotency_key_cannot_be_reused_for_different_case_payload(tmp_path):
    client = TestClient(create_app(str(tmp_path / 'db.sqlite')))
    headers = {'X-Guanchao-Request-Key': 'same-key-different-body'}
    first = client.post(
        '/api/cases',
        headers=headers,
        json={'title': '第一条', 'goal': '核查', 'targets': [_target('first')]},
    )
    second = client.post(
        '/api/cases',
        headers=headers,
        json={'title': '第二条', 'goal': '核查', 'targets': [_target('second')]},
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert '不同的调查内容' in second.json()['detail']
    cases = client.get('/api/cases?status=all').json()
    assert len(cases) == 1
    assert cases[0]['id'] == first.json()['id']
