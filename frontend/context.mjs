import { caseIdFromRequest, currentCaseIdFromHref, isCaseDetailRequest, requestMeta } from './runtime.mjs';

const EMPTY_DRAFT = Object.freeze({ message: '', note: '' });
const EDITABLE_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);
const BATCH_HEADER_KEYS = new Set(['platform', 'handle', 'bio', 'posts', 'profile_url']);

function cleanDraft(value = {}) {
  return {
    message: String(value.message || ''),
    note: String(value.note || ''),
  };
}

function hasDraft(value) {
  return Boolean(value.message || value.note);
}

export function createDraftRegistry(limit = 50) {
  const drafts = new Map();
  let currentCaseId = null;

  const store = (caseId, value) => {
    if (!caseId) return;
    const draft = cleanDraft(value);
    drafts.delete(caseId);
    if (hasDraft(draft)) drafts.set(caseId, draft);
    while (drafts.size > Math.max(1, Number(limit) || 50)) drafts.delete(drafts.keys().next().value);
  };

  return {
    current() {
      return currentCaseId;
    },
    select(caseId, currentDraft = EMPTY_DRAFT) {
      if (currentCaseId && currentCaseId !== caseId) store(currentCaseId, currentDraft);
      currentCaseId = caseId || null;
      return cleanDraft(currentCaseId ? drafts.get(currentCaseId) || EMPTY_DRAFT : EMPTY_DRAFT);
    },
    update(caseId, value) {
      if (caseId) store(caseId, value);
    },
    consume(caseId, field) {
      if (!caseId || !['message', 'note'].includes(field)) return;
      const draft = cleanDraft(drafts.get(caseId) || EMPTY_DRAFT);
      draft[field] = '';
      store(caseId, draft);
    },
    clear(caseId) {
      if (!caseId) return;
      drafts.delete(caseId);
      if (currentCaseId === caseId) currentCaseId = null;
    },
    get(caseId) {
      return cleanDraft(drafts.get(caseId) || EMPTY_DRAFT);
    },
  };
}

export function shouldBlockReviewShortcut({ key, modalOpen, targetTagName = '' } = {}) {
  return Boolean(
    modalOpen
    && ['1', '2', '3'].includes(String(key || ''))
    && !EDITABLE_TAGS.has(String(targetTagName || '').toUpperCase())
  );
}

function unquoteHeaderCell(cell) {
  const trimmed = String(cell || '').trim();
  if (trimmed.startsWith('"') && trimmed.endsWith('"') && trimmed.length >= 2) {
    return { quoted: true, value: trimmed.slice(1, -1) };
  }
  return { quoted: false, value: trimmed };
}

export function normalizeBatchHeaderText(text) {
  const raw = String(text || '');
  if (!raw) return raw;
  const newline = raw.search(/\r?\n/);
  const firstLine = (newline >= 0 ? raw.slice(0, newline) : raw).replace(/^\uFEFF/, '');
  const rest = newline >= 0 ? raw.slice(newline) : '';
  const cells = firstLine.split(',');
  const normalizedValues = cells.map((cell) => unquoteHeaderCell(cell).value.replace(/^\uFEFF/, '').trim());
  const hasHeader = normalizedValues.some((value) => value.toLowerCase() === 'handle' || value === '账号');
  if (!hasHeader) return firstLine + rest;

  const normalized = cells.map((cell) => {
    const parsed = unquoteHeaderCell(cell);
    const value = parsed.value.replace(/^\uFEFF/, '').trim();
    const lower = value.toLowerCase();
    const next = BATCH_HEADER_KEYS.has(lower) ? lower : value;
    return parsed.quoted ? `"${next}"` : next;
  });
  return normalized.join(',') + rest;
}

export function guardCaseBoundBlob(response, requestCaseId, getSelectedCaseId) {
  if (!response?.ok || !requestCaseId || typeof response.blob !== 'function') return response;
  const originalBlob = response.blob.bind(response);
  Object.defineProperty(response, 'blob', {
    configurable: true,
    value: async () => {
      const blob = await originalBlob();
      if (getSelectedCaseId() !== requestCaseId) throw new Error('已切换到另一调查，原调查报告未下载。');
      return blob;
    },
  });
  return response;
}

export function submittedDraftField(meta, response) {
  const applied = response?.ok || response?.headers?.get?.('X-Guanchao-Write-Applied') === '1';
  if (!applied || meta.method !== 'POST') return '';
  if (/^\/api\/cases\/[^/]+\/messages$/.test(meta.url.pathname)) return 'message';
  if (/^\/api\/cases\/[^/]+\/comments$/.test(meta.url.pathname)) return 'note';
  return '';
}

function visibleModal(documentRef) {
  return [...documentRef.querySelectorAll?.('.modal-backdrop') || []].find((item) => !item.hidden) || null;
}

function draftFromDocument(documentRef) {
  return {
    message: documentRef.querySelector?.('#messageInput')?.value || '',
    note: documentRef.querySelector?.('#caseNoteInput')?.value || '',
  };
}

function applyDraft(documentRef, draft) {
  const message = documentRef.querySelector?.('#messageInput');
  const note = documentRef.querySelector?.('#caseNoteInput');
  if (message) {
    message.value = draft.message || '';
    if (typeof Event === 'function') message.dispatchEvent?.(new Event('input', { bubbles: true }));
  }
  if (note) note.value = draft.note || '';
}

function isCaseDelete(meta) {
  return meta.method === 'DELETE' && /^\/api\/cases\/[^/]+$/.test(meta.url.pathname);
}

function isCaseReport(meta) {
  return meta.method === 'GET' && /^\/api\/cases\/[^/]+\/report$/.test(meta.url.pathname);
}

export function installCaseContextGuards({ windowRef = window, documentRef = document } = {}) {
  const registry = createDraftRegistry();
  const initialCaseId = currentCaseIdFromHref(windowRef.location.href);
  if (initialCaseId) registry.select(initialCaseId, EMPTY_DRAFT);

  const saveCurrent = () => {
    const current = registry.current();
    if (current) registry.update(current, draftFromDocument(documentRef));
  };

  ['#messageInput', '#caseNoteInput'].forEach((selector) => {
    documentRef.querySelector?.(selector)?.addEventListener?.('input', saveCurrent);
  });

  const batchForm = documentRef.querySelector?.('#batchForm');
  batchForm?.addEventListener?.('submit', () => {
    const input = documentRef.querySelector?.('#batchInput');
    if (input) input.value = normalizeBatchHeaderText(input.value);
  }, true);

  documentRef.addEventListener('keydown', (event) => {
    if (!shouldBlockReviewShortcut({
      key: event.key,
      modalOpen: Boolean(visibleModal(documentRef)),
      targetTagName: event.target?.tagName,
    })) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);

  const downstreamFetch = windowRef.fetch.bind(windowRef);
  windowRef.fetch = async (input, init = {}) => {
    const meta = requestMeta(input, init, windowRef.location.href);
    const response = await downstreamFetch(input, init);
    const requestCaseId = caseIdFromRequest(meta);
    const consumedField = submittedDraftField(meta, response);
    if (consumedField && requestCaseId) registry.consume(requestCaseId, consumedField);

    if (isCaseDetailRequest(meta) && response.ok) {
      const snapshot = await response.clone().json().catch(() => ({}));
      if (snapshot?.id && snapshot.id !== '__stale_case_request__') {
        const restored = registry.select(snapshot.id, draftFromDocument(documentRef));
        applyDraft(documentRef, restored);
      }
    } else if (isCaseDelete(meta) && response.ok && requestCaseId) {
      const wasCurrent = registry.current() === requestCaseId;
      registry.clear(requestCaseId);
      if (wasCurrent) applyDraft(documentRef, EMPTY_DRAFT);
    }

    if (isCaseReport(meta) && response.ok) return guardCaseBoundBlob(response, requestCaseId, () => registry.current());
    return response;
  };

  return registry;
}
