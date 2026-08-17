import assert from 'node:assert/strict';
import test from 'node:test';
import { createCreationFetch, isCreatedCaseWrite, withCaseTransition } from '../frontend/creation.mjs';
import { requestMeta } from '../frontend/runtime.mjs';

const response = (data, status = 200) => new Response(JSON.stringify(data), {
  status,
  headers: { 'content-type': 'application/json' },
});

test('new case initial message is marked as the same create transition', async () => {
  let resolveMessage;
  let messageCalls = 0;
  let transitionHeader = '';
  const downstreamFetch = async (input, init = {}) => {
    const path = new URL(String(input), 'http://local/').pathname;
    if (path === '/api/cases') return response({ id: 'case-b' });
    if (path.endsWith('/messages')) {
      messageCalls += 1;
      transitionHeader = new Headers(init.headers || {}).get('X-Guanchao-Case-Transition') || '';
      return new Promise((resolve) => { resolveMessage = resolve; });
    }
    return response({ id: 'case-b', runs: [] });
  };
  const guarded = createCreationFetch({ downstreamFetch, getHref: () => 'http://local/?case=case-a' });

  assert.equal((await (await guarded('/api/cases', { method: 'POST', body: '{}' })).json()).id, 'case-b');
  const first = guarded('/api/cases/case-b/messages', { method: 'POST', body: JSON.stringify({ content: '开始核查' }) });
  const second = guarded('/api/cases/case-b/messages', { method: 'POST', body: JSON.stringify({ content: '开始核查' }) });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(messageCalls, 1);
  assert.equal(transitionHeader, 'case-b');
  resolveMessage(response({ run_id: 'run-b' }));
  assert.equal((await (await first).json()).run_id, 'run-b');
  assert.equal((await (await second).json()).run_id, 'run-b');
});

test('real create sequence keeps transition through case detail load until initial run starts', async () => {
  let transitionHeader = '';
  const downstreamFetch = async (input, init = {}) => {
    const path = new URL(String(input), 'http://local/').pathname;
    if (path === '/api/cases') return response({ id: 'case-b' });
    if (path === '/api/cases/case-b') return response({ id: 'case-b', runs: [] });
    if (path === '/api/cases/case-b/messages') {
      transitionHeader = new Headers(init.headers || {}).get('X-Guanchao-Case-Transition') || '';
      return response({ run_id: 'run-b' });
    }
    return response({});
  };
  const guarded = createCreationFetch({ downstreamFetch, getHref: () => 'http://local/?case=case-a' });

  await guarded('/api/cases', { method: 'POST', body: '{}' });
  await guarded('/api/cases/case-b');
  const started = await guarded('/api/cases/case-b/messages', {
    method: 'POST',
    body: JSON.stringify({ content: '首次核查' }),
  });
  assert.equal(started.status, 200);
  assert.equal(transitionHeader, 'case-b');
});

test('switching away during new case open never sends the new goal to another case', async () => {
  const recoveries = [];
  const messageTargets = [];
  const downstreamFetch = async (input) => {
    const path = new URL(String(input), 'http://local/').pathname;
    if (path === '/api/cases') return response({ id: 'case-b' });
    if (path.endsWith('/messages')) messageTargets.push(path);
    return response({ id: 'case-b', runs: [] });
  };
  const guarded = createCreationFetch({
    downstreamFetch,
    getHref: () => 'http://local/?case=case-c',
    onRecover: (caseId, detail) => recoveries.push([caseId, detail]),
  });

  await guarded('/api/cases', { method: 'POST', body: '{}' });
  await guarded('/api/cases/case-b');
  const blocked = await guarded('/api/cases/case-c/messages', {
    method: 'POST',
    body: JSON.stringify({ content: 'B 的首次核查目标' }),
  });
  assert.equal(blocked.status, 409);
  assert.deepEqual(messageTargets, []);
  assert.equal(recoveries.length, 1);
  assert.equal(recoveries[0][0], 'case-b');
});

test('failed initial run recovers the already-created case instead of encouraging duplicate creation', async () => {
  const recoveries = [];
  const downstreamFetch = async (input) => {
    const path = new URL(String(input), 'http://local/').pathname;
    if (path === '/api/cases') return response({ id: 'case-b' });
    if (path.endsWith('/messages')) return response({ detail: '执行容量已满' }, 429);
    return response({});
  };
  const guarded = createCreationFetch({
    downstreamFetch,
    getHref: () => 'http://local/?case=case-a',
    onRecover: (caseId, detail) => recoveries.push([caseId, detail]),
  });

  await guarded('/api/cases', { method: 'POST', body: '{}' });
  const failed = await guarded('/api/cases/case-b/messages', {
    method: 'POST',
    body: JSON.stringify({ content: '开始核查' }),
  });
  assert.equal(failed.status, 409);
  assert.deepEqual(recoveries, [['case-b', '执行容量已满']]);
});

test('transition helper only binds writes to the exact created case', () => {
  const message = requestMeta('/api/cases/case-b/messages', { method: 'POST' }, 'http://local/?case=case-a');
  const other = requestMeta('/api/cases/case-c/messages', { method: 'POST' }, 'http://local/?case=case-a');
  assert.equal(isCreatedCaseWrite(message, 'case-b'), true);
  assert.equal(isCreatedCaseWrite(other, 'case-b'), false);
  const init = withCaseTransition({ method: 'POST' }, 'case-b');
  assert.equal(new Headers(init.headers).get('X-Guanchao-Case-Transition'), 'case-b');
});
