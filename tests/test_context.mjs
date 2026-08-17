import assert from 'node:assert/strict';
import test from 'node:test';
import {
  createDraftRegistry,
  guardCaseBoundBlob,
  normalizeBatchHeaderText,
  shouldBlockReviewShortcut,
} from '../frontend/context.mjs';

test('message and collaboration drafts stay isolated per case', () => {
  const drafts = createDraftRegistry();

  assert.deepEqual(drafts.select('case-a'), { message: '', note: '' });
  drafts.update('case-a', { message: 'A 的核查草稿', note: 'A 的协作备注' });

  assert.deepEqual(
    drafts.select('case-b', { message: 'A 的核查草稿', note: 'A 的协作备注' }),
    { message: '', note: '' },
  );
  drafts.update('case-b', { message: 'B 的核查草稿', note: '' });

  assert.deepEqual(
    drafts.select('case-a', { message: 'B 的核查草稿', note: '' }),
    { message: 'A 的核查草稿', note: 'A 的协作备注' },
  );
  assert.equal(drafts.current(), 'case-a');
});

test('successful message and note writes consume only their matching drafts', () => {
  const drafts = createDraftRegistry();
  drafts.select('case-a');
  drafts.update('case-a', { message: '已经发送的核查', note: '仍未发送的备注' });
  drafts.consume('case-a', 'message');
  assert.deepEqual(drafts.get('case-a'), { message: '', note: '仍未发送的备注' });
  drafts.consume('case-a', 'note');
  assert.deepEqual(drafts.get('case-a'), { message: '', note: '' });
});

test('failed writes can leave drafts intact for retry', () => {
  const drafts = createDraftRegistry();
  drafts.select('case-a');
  drafts.update('case-a', { message: '网络失败后要保留', note: '备注也保留' });
  assert.deepEqual(drafts.get('case-a'), { message: '网络失败后要保留', note: '备注也保留' });
});

test('clearing a deleted current case removes its drafts and selection', () => {
  const drafts = createDraftRegistry();
  drafts.select('case-a');
  drafts.update('case-a', { message: '不会泄漏', note: '不会残留' });
  drafts.clear('case-a');

  assert.equal(drafts.current(), null);
  assert.deepEqual(drafts.get('case-a'), { message: '', note: '' });
});

test('draft registry is bounded instead of retaining every case forever', () => {
  const drafts = createDraftRegistry(2);
  drafts.update('case-a', { message: 'A' });
  drafts.update('case-b', { message: 'B' });
  drafts.update('case-c', { message: 'C' });

  assert.deepEqual(drafts.get('case-a'), { message: '', note: '' });
  assert.equal(drafts.get('case-b').message, 'B');
  assert.equal(drafts.get('case-c').message, 'C');
});

test('review number shortcuts are blocked behind an open modal but not while editing form fields', () => {
  assert.equal(shouldBlockReviewShortcut({ key: '1', modalOpen: true, targetTagName: 'BUTTON' }), true);
  assert.equal(shouldBlockReviewShortcut({ key: '3', modalOpen: true, targetTagName: 'DIV' }), true);
  assert.equal(shouldBlockReviewShortcut({ key: '2', modalOpen: true, targetTagName: 'TEXTAREA' }), false);
  assert.equal(shouldBlockReviewShortcut({ key: '1', modalOpen: false, targetTagName: 'BODY' }), false);
  assert.equal(shouldBlockReviewShortcut({ key: 'x', modalOpen: true, targetTagName: 'BODY' }), false);
});

test('batch import normalizes UTF-8 BOM and common ASCII header casing before parsing', () => {
  const input = '\uFEFFPlatform,Handle,Bio,Posts,Profile_URL\nweibo,alice,生活记录,第一条|第二条,https://example.com';
  const normalized = normalizeBatchHeaderText(input);
  assert.equal(
    normalized,
    'platform,handle,bio,posts,profile_url\nweibo,alice,生活记录,第一条|第二条,https://example.com',
  );
});

test('batch import without a header only strips BOM and preserves row data', () => {
  const input = '\uFEFFWeibo,Alice,生活记录,第一条,https://example.com';
  assert.equal(normalizeBatchHeaderText(input), 'Weibo,Alice,生活记录,第一条,https://example.com');
});

test('case-bound report blob is rejected when the analyst switches cases during download', async () => {
  let selectedCase = 'case-a';
  let releaseBlob;
  const response = {
    ok: true,
    blob: () => new Promise((resolve) => { releaseBlob = () => resolve(new Blob(['report-a'])); }),
  };
  const guarded = guardCaseBoundBlob(response, 'case-a', () => selectedCase);
  const reading = guarded.blob();
  selectedCase = 'case-b';
  releaseBlob();
  await assert.rejects(reading, /已切换到另一调查/);
});

test('case-bound report blob remains available while the same case stays selected', async () => {
  const response = new Response('report-a');
  const guarded = guardCaseBoundBlob(response, 'case-a', () => 'case-a');
  const blob = await guarded.blob();
  assert.equal(await blob.text(), 'report-a');
});
