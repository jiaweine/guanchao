import assert from 'node:assert/strict';
import test from 'node:test';
import { createDraftRegistry, shouldBlockReviewShortcut } from '../frontend/context.mjs';

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
