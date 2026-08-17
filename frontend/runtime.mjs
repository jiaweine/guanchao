const JSON_HEADERS = { 'content-type': 'application/json; charset=utf-8' };

export function currentCaseIdFromHref(href) {
  try {
    return new URL(href).searchParams.get('case');
  } catch {
    return null;
  }
}

function caseSelection(getHref) {
  let lastHrefCaseId = currentCaseIdFromHref(getHref());
  let activeCaseId = lastHrefCaseId;
  return {
    select(caseId) {
      activeCaseId = caseId || null;
    },
    current() {
      const hrefCaseId = currentCaseIdFromHref(getHref());
      if (hrefCaseId !== lastHrefCaseId) {
        lastHrefCaseId = hrefCaseId;
        activeCaseId = hrefCaseId;
      }
      return activeCaseId || hrefCaseId;
    },
  };
}

function requestHeaders(input, init) {
  const headers = new Headers(
    init.headers || (typeof Request !== 'undefined' && input instanceof Request ? input.headers : undefined),
  );
  return headers;
}

export function requestMeta(input, init = {}, baseHref = 'http://localhost/') {
  const rawUrl = typeof input === 'string' || input instanceof URL ? input : input.url;
  const url = new URL(rawUrl, baseHref);
  const method = String(init.method || (typeof Request !== 'undefined' && input instanceof Request ? input.method : 'GET')).toUpperCase();
  const headers = requestHeaders(input, init);
  return {
    url,
    method,
    body: init.body,
    transitionCaseId: (headers.get('X-Guanchao-Case-Transition') || '').trim(),
  };
}

export function caseIdFromRequest(meta) {
  const match = meta.url.pathname.match(/^\/api\/cases\/([^/]+)(?:\/|$)/);
  if (match && !['batch'].includes(match[1])) return decodeURIComponent(match[1]);
  if (meta.url.pathname === '/api/reviews' && typeof meta.body === 'string') {
    try {
      return JSON.parse(meta.body).case_id || null;
    } catch {
      return null;
    }
  }
  return null;
}

export function isCaseDetailRequest(meta) {
  return meta.method === 'GET' && /^\/api\/cases\/[^/]+$/.test(meta.url.pathname);
}

export function isAuditRequest(meta) {
  return meta.method === 'GET' && meta.url.pathname === '/api/audit' && Boolean(meta.url.searchParams.get('case_id'));
}

export function isSensitiveCaseWrite(meta) {
  if (meta.url.pathname === '/api/reviews' && meta.method === 'POST') return true;
  const caseId = caseIdFromRequest(meta);
  if (!caseId || ['GET', 'HEAD', 'OPTIONS'].includes(meta.method)) return false;
  return meta.url.pathname !== '/api/events';
}

export function allowsCaseTransition(meta, caseId) {
  return Boolean(caseId && meta.transitionCaseId && meta.transitionCaseId === caseId);
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function staleWriteResponse(caseId) {
  return jsonResponse({ detail: `操作已应用到原调查 ${caseId}；你已切换到另一调查，页面没有采用旧响应。` }, 409);
}

function restoreDraft(documentRef, content) {
  if (!content || !documentRef) return;
  const input = documentRef.querySelector?.('#messageInput');
  if (!input || input.value.trim()) return;
  input.value = content;
  if (typeof Event === 'function') input.dispatchEvent?.(new Event('input', { bubbles: true }));
}

function showToast(documentRef, message) {
  const element = documentRef?.querySelector?.('#toast');
  if (!element) return;
  element.textContent = message;
  element.classList.add('show');
  clearTimeout(element.__runtimeTimer);
  element.__runtimeTimer = setTimeout(() => element.classList.remove('show'), 2600);
}

function setReviewBusy(documentRef, busy) {
  ['#markNormalBtn', '#markUncertainBtn', '#markMarketingBtn'].forEach((selector) => {
    const button = documentRef?.querySelector?.(selector);
    if (button) button.disabled = busy;
  });
}

function parseMessageDraft(meta) {
  if (meta.method !== 'POST' || !meta.url.pathname.endsWith('/messages') || typeof meta.body !== 'string') return '';
  try {
    return String(JSON.parse(meta.body).content || '').trim();
  } catch {
    return '';
  }
}

export function createGuardedFetch({ nativeFetch, getHref, documentRef = null, onCaseState = () => {}, onRunState = () => {}, onReviewBusy = () => {} } = {}) {
  if (typeof nativeFetch !== 'function') throw new TypeError('nativeFetch is required');
  let detailSeq = 0;
  let latestDetail = null;
  let auditSeq = 0;
  let latestAudit = null;
  let reviewSubmitting = false;
  const selection = caseSelection(getHref);

  const coordinateRead = async (slot, sequence, networkPromise, fallbackData) => {
    const response = await networkPromise;
    const latest = slot();
    if (sequence === latest?.sequence) return response;
    try {
      const replacement = await latest.responsePromise;
      return replacement.clone();
    } catch {
      return jsonResponse(fallbackData);
    }
  };

  return async function guardedFetch(input, init = {}) {
    const hrefAtStart = getHref();
    const meta = requestMeta(input, init, hrefAtStart);
    const caseId = caseIdFromRequest(meta);
    const draft = parseMessageDraft(meta);

    if (meta.url.pathname === '/api/reviews' && meta.method === 'POST') {
      if (reviewSubmitting) return jsonResponse({ detail: '复核正在提交，请勿重复操作' }, 409);
      reviewSubmitting = true;
      setReviewBusy(documentRef, true);
      onReviewBusy(true);
    }

    if (isCaseDetailRequest(meta)) {
      const sequence = ++detailSeq;
      const networkPromise = nativeFetch(input, init);
      latestDetail = { sequence, caseId, responsePromise: networkPromise.then((response) => response.clone()) };
      const coordinated = await coordinateRead(() => latestDetail, sequence, networkPromise, { id: '__stale_case_request__' });
      try {
        const item = await coordinated.clone().json();
        if (item?.id && item.id !== '__stale_case_request__') {
          selection.select(item.id);
          const latest = (item.runs || [])[0];
          onCaseState(item.id, latest?.status === 'running');
        }
      } catch {
        // Core API will surface malformed responses; runtime only tracks valid case snapshots.
      }
      return coordinated;
    }

    if (isAuditRequest(meta)) {
      const sequence = ++auditSeq;
      const auditCaseId = meta.url.searchParams.get('case_id');
      const networkPromise = nativeFetch(input, init);
      latestAudit = { sequence, caseId: auditCaseId, responsePromise: networkPromise.then((response) => response.clone()) };
      return coordinateRead(() => latestAudit, sequence, networkPromise, []);
    }

    try {
      const response = await nativeFetch(input, init);
      if (meta.method === 'GET' && /^\/api\/runs\/[^/]+$/.test(meta.url.pathname)) {
        response.clone().json().then((run) => {
          if (run?.case_id) onRunState(run.case_id, run.status === 'running');
        }).catch(() => {});
      }
      if (draft && response.ok && caseId) onRunState(caseId, true);
      const selectedCaseId = selection.current();
      const moved = Boolean(caseId && selectedCaseId && caseId !== selectedCaseId);

      if (draft && !response.ok && !moved) restoreDraft(documentRef, draft);

      if (isSensitiveCaseWrite(meta) && response.ok && moved && !allowsCaseTransition(meta, caseId)) {
        return staleWriteResponse(caseId);
      }

      if (meta.url.pathname.endsWith('/target') && meta.method === 'PATCH' && response.status === 202) {
        const payload = await response.clone().json().catch(() => ({}));
        if (payload.capacity_limited) {
          setTimeout(() => showToast(documentRef, '资料已保存，但核查队列已满；稍后可重新发起核查。'), 450);
        }
      }

      return response;
    } catch (error) {
      const selectedCaseId = selection.current();
      if (draft && (!caseId || !selectedCaseId || caseId === selectedCaseId)) restoreDraft(documentRef, draft);
      throw error;
    } finally {
      if (meta.url.pathname === '/api/reviews' && meta.method === 'POST') {
        reviewSubmitting = false;
        setReviewBusy(documentRef, false);
        onReviewBusy(false);
      }
    }
  };
}

export function installRuntimeGuards({ windowRef = window, documentRef = document } = {}) {
  const nativeFetch = windowRef.fetch.bind(windowRef);
  const runningCases = new Map();
  let reviewBusy = false;
  const selection = caseSelection(() => windowRef.location.href);
  const selectedCaseId = () => selection.current();

  const enforceRunningControls = (caseId) => {
    if (!runningCases.get(caseId) || selectedCaseId() !== caseId) return;
    const send = documentRef.querySelector?.('#sendBtn');
    const attach = documentRef.querySelector?.('#attachBtn');
    if (send) send.disabled = true;
    if (attach) attach.disabled = true;
  };

  const observeRunState = (caseId, running) => {
    if (!caseId) return;
    runningCases.set(caseId, Boolean(running));
    if (!running) return;
    enforceRunningControls(caseId);
    queueMicrotask(() => enforceRunningControls(caseId));
    setTimeout(() => enforceRunningControls(caseId), 0);
  };

  const selectCaseState = (caseId, running) => {
    if (caseId) selection.select(caseId);
    observeRunState(caseId, running);
  };

  windowRef.fetch = createGuardedFetch({
    nativeFetch,
    getHref: () => windowRef.location.href,
    documentRef,
    onCaseState: selectCaseState,
    onRunState: observeRunState,
    onReviewBusy: (busy) => { reviewBusy = busy; },
  });

  let modalReturnFocus = null;
  documentRef.addEventListener('click', (event) => {
    const currentCase = selectedCaseId();
    const reviewAction = event.target.closest?.('#markNormalBtn,#markUncertainBtn,#markMarketingBtn');
    if (reviewAction && reviewBusy) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    const guardedAction = event.target.closest?.('#sendBtn,#attachBtn');
    if (guardedAction && currentCase && runningCases.get(currentCase)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      showToast(documentRef, '当前核查仍在进行，完成后再继续补充或发起下一轮。');
      return;
    }
    const opener = event.target.closest?.('#newCaseBtn,#newCaseRailBtn,#batchBtn,#workspaceBtn,#refreshSourceBtn,[data-open="new"],[data-open="batch"]');
    if (opener) modalReturnFocus = opener;
    const closer = event.target.closest?.('[data-close]');
    if (closer && modalReturnFocus) setTimeout(() => modalReturnFocus?.focus?.(), 0);
  }, true);

  documentRef.addEventListener('keydown', (event) => {
    const currentCase = selectedCaseId();
    if (reviewBusy && ['1', '2', '3'].includes(event.key) && !['INPUT', 'TEXTAREA', 'SELECT'].includes(event.target?.tagName)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey && event.target?.id === 'messageInput' && currentCase && runningCases.get(currentCase)) {
      event.preventDefault();
      event.stopImmediatePropagation();
      showToast(documentRef, '当前核查仍在进行，完成后再发起下一轮。');
      return;
    }
    if (event.key !== 'Escape') return;
    const modal = [...documentRef.querySelectorAll?.('.modal-backdrop') || []].find((item) => !item.hidden);
    if (!modal) return;
    const close = modal.querySelector?.('[data-close]');
    if (!close) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    close.click();
    setTimeout(() => modalReturnFocus?.focus?.(), 0);
  }, true);

  documentRef.addEventListener('drop', (event) => {
    const currentCase = selectedCaseId();
    if (!currentCase || !runningCases.get(currentCase) || !event.dataTransfer?.files?.length) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const layer = documentRef.querySelector?.('#dropLayer');
    if (layer) layer.hidden = true;
    showToast(documentRef, '当前核查仍在进行；请完成后再添加新素材，避免素材漏出本轮证据。');
  }, true);
}
