import { caseIdFromRequest, currentCaseIdFromHref, isCaseDetailRequest, requestMeta } from './runtime.mjs';

const EMPTY_DRAFT = Object.freeze({ message: '', note: '' });
const EDITABLE_TAGS = new Set(['INPUT', 'TEXTAREA', 'SELECT']);

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
    while (drafts.size > Math.max(1, Number(limit) || 50)) {
      drafts.delete(drafts.keys().next().value);
    }
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
      if (!caseId) return;
      store(caseId, value);
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

    if (isCaseDetailRequest(meta) && response.ok) {
      const snapshot = await response.clone().json().catch(() => ({}));
      if (snapshot?.id && snapshot.id !== '__stale_case_request__') {
        const restored = registry.select(snapshot.id, draftFromDocument(documentRef));
        applyDraft(documentRef, restored);
      }
    } else if (isCaseDelete(meta) && response.ok) {
      const deletedCaseId = caseIdFromRequest(meta);
      if (deletedCaseId) {
        const wasCurrent = registry.current() === deletedCaseId;
        registry.clear(deletedCaseId);
        if (wasCurrent) applyDraft(documentRef, EMPTY_DRAFT);
      }
    }

    return response;
  };

  return registry;
}
