import assert from 'node:assert/strict';
import test from 'node:test';
import {
  caseIdFromRequest,
  createGuardedFetch,
  currentCaseIdFromHref,
  isCaseDetailRequest,
  isSensitiveCaseWrite,
  requestMeta,
} from '../frontend/runtime.mjs';
import {
  assetDeletePath,
  canOfferAssetDelete,
  isCaseReportRequest,
  removeAssetFromSnapshot,
  shouldDiscardCaseBoundResult,
} from '../frontend/interaction.mjs';

const response = (data, status = 200) => new Response(JSON.stringify(data), { status, headers: { 'content-type': 'application/json' } });

test('request classification keeps case-scoped writes explicit', () => {
  const detail = requestMeta('/api/cases/a', {}, 'http://local/?case=a');
  const review = requestMeta('/api/reviews', { method: 'POST', body: JSON.stringify({ case_id: 'a' }) }, 'http://local/?case=a');
  const comment = requestMeta('/api/cases/a/comments', { method: 'POST', body: JSON.stringify({ content: 'note' }) }, 'http://local/?case=a');
  assert.equal(currentCaseIdFromHref('http://local/?case=a'), 'a');
  assert.equal(caseIdFromRequest(detail), 'a');
  assert.equal(isCaseDetailRequest(detail), true);
  assert.equal(caseIdFromRequest(review), 'a');
  assert.equal(isSensitiveCaseWrite(review), true);
  assert.equal(isSensitiveCaseWrite(comment), true);
});

test('older case detail response is replaced by the latest requested case', async () => {
  const pending = new Map();
  const nativeFetch = (input) => new Promise((resolve) => pending.set(String(input), resolve));
  const guarded = createGuardedFetch({ nativeFetch, getHref: () => 'http://local/?case=a' });
  const first = guarded('/api/cases/a');
  const second = guarded('/api/cases/b');
  pending.get('/api/cases/b')(response({ id: 'b' }));
  pending.get('/api/cases/a')(response({ id: 'a' }));
  assert.equal((await (await second).json()).id, 'b');
  assert.equal((await (await first).json()).id, 'b');
});

test('successful stale write cannot mutate the newly opened case UI', async () => {
  let href = 'http://local/?case=a';
  let resolveWrite;
  const nativeFetch = () => new Promise((resolve) => { resolveWrite = resolve; });
  const guarded = createGuardedFetch({ nativeFetch, getHref: () => href });
  const write = guarded('/api/cases/a', { method: 'PATCH', body: JSON.stringify({ priority: 'high' }) });
  href = 'http://local/?case=b';
  resolveWrite(response({ id: 'a' }));
  const guardedResponse = await write;
  assert.equal(guardedResponse.status, 409);
  assert.match((await guardedResponse.json()).detail, /原调查/);
});

test('selected case state closes the gap before the browser URL is updated', async () => {
  const pending = new Map();
  const nativeFetch = (input, init = {}) => new Promise((resolve) => pending.set(`${init.method || 'GET'} ${String(input)}`, resolve));
  const guarded = createGuardedFetch({ nativeFetch, getHref: () => 'http://local/?case=a' });

  const write = guarded('/api/cases/a', { method: 'PATCH', body: JSON.stringify({ owner: 'analyst' }) });
  const openB = guarded('/api/cases/b');
  pending.get('GET /api/cases/b')(response({ id: 'b', runs: [] }));
  assert.equal((await (await openB).json()).id, 'b');

  pending.get('PATCH /api/cases/a')(response({ id: 'a' }));
  const staleWrite = await write;
  assert.equal(staleWrite.status, 409);
  assert.match((await staleWrite.json()).detail, /原调查/);
});

test('stale collaborative note response cannot clear the newly opened case composer', async () => {
  let href = 'http://local/?case=a';
  let resolveComment;
  const nativeFetch = () => new Promise((resolve) => { resolveComment = resolve; });
  const guarded = createGuardedFetch({ nativeFetch, getHref: () => href });
  const comment = guarded('/api/cases/a/comments', { method: 'POST', body: JSON.stringify({ content: 'A 的备注' }) });
  href = 'http://local/?case=b';
  resolveComment(response({ ok: true }));
  const guardedResponse = await comment;
  assert.equal(guardedResponse.status, 409);
  assert.match((await guardedResponse.json()).detail, /原调查/);
});

test('failed message request restores the draft instead of losing user text', async () => {
  const input = { value: '', dispatchEvent() {} };
  const documentRef = { querySelector(selector) { return selector === '#messageInput' ? input : null; } };
  const guarded = createGuardedFetch({
    nativeFetch: async () => response({ detail: 'busy' }, 409),
    getHref: () => 'http://local/?case=a',
    documentRef,
  });
  await guarded('/api/cases/a/messages', { method: 'POST', body: JSON.stringify({ content: '不要丢掉这段文字' }) });
  assert.equal(input.value, '不要丢掉这段文字');
});

test('asset management only offers deletion for writable idle cases and survives consecutive deletes', () => {
  const base = { id: 'case a', status: 'active', runs: [{ status: 'completed' }] };
  assert.equal(canOfferAssetDelete(base, true), true);
  assert.equal(canOfferAssetDelete({ ...base, status: 'archived' }, true), false);
  assert.equal(canOfferAssetDelete({ ...base, runs: [{ status: 'running' }] }, true), false);
  assert.equal(canOfferAssetDelete(base, false), false);
  assert.equal(assetDeletePath('case a', 'asset/1'), '/api/cases/case%20a/assets/asset%2F1');

  const snapshot = { assets: [{ id: 'a' }, { id: 'b' }, { id: 'c' }] };
  assert.deepEqual(removeAssetFromSnapshot(snapshot, 'a').map((item) => item.id), ['b', 'c']);
  assert.deepEqual(removeAssetFromSnapshot(snapshot, 'b').map((item) => item.id), ['c']);
});

test('case-bound report result is discarded after switching to another investigation', () => {
  const report = requestMeta('/api/cases/a/report?output=markdown', {}, 'http://local/?case=a');
  assert.equal(isCaseReportRequest(report), true);
  assert.equal(shouldDiscardCaseBoundResult(report, 'a'), false);
  assert.equal(shouldDiscardCaseBoundResult(report, 'b'), true);
  const ordinaryRead = requestMeta('/api/cases/a', {}, 'http://local/?case=b');
  assert.equal(shouldDiscardCaseBoundResult(ordinaryRead, 'b'), false);
});
