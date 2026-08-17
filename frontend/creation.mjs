import { caseIdFromRequest, isCaseDetailRequest, requestMeta } from './runtime.mjs';

const JSON_HEADERS = { 'content-type': 'application/json; charset=utf-8' };

export function isCreatedCaseWrite(meta, caseId) {
  return Boolean(
    caseId
    && caseIdFromRequest(meta) === caseId
    && !['GET', 'HEAD', 'OPTIONS'].includes(meta.method)
  );
}

export function withCaseTransition(init, caseId) {
  const headers = new Headers(init.headers || {});
  headers.set('X-Guanchao-Case-Transition', caseId);
  return { ...init, headers };
}

function messageSignature(meta) {
  return meta.method === 'POST' && meta.url.pathname.endsWith('/messages') && typeof meta.body === 'string'
    ? meta.body
    : '';
}

function conflictResponse(detail) {
  return new Response(JSON.stringify({ detail }), { status: 409, headers: JSON_HEADERS });
}

export function createCreationFetch({ downstreamFetch, getHref, onRecover = () => {} } = {}) {
  if (typeof downstreamFetch !== 'function') throw new TypeError('downstreamFetch is required');
  let createdCaseId = null;
  const messageFlights = new Map();
  const recoveryCases = new Set();

  const recover = (caseId, detail) => {
    if (!caseId || recoveryCases.has(caseId)) return;
    recoveryCases.add(caseId);
    onRecover(caseId, detail);
  };

  return async function creationFetch(input, init = {}) {
    const meta = requestMeta(input, init, getHref());
    const creatingCase = meta.method === 'POST' && meta.url.pathname === '/api/cases';
    const transitionWrite = isCreatedCaseWrite(meta, createdCaseId);
    const signature = transitionWrite ? messageSignature(meta) : '';
    const effectiveInit = transitionWrite ? withCaseTransition(init, createdCaseId) : init;

    let response;
    try {
      if (signature) {
        let shared = messageFlights.get(signature);
        if (!shared) {
          shared = downstreamFetch(input, effectiveInit).then((item) => item.clone());
          messageFlights.set(signature, shared);
        }
        response = (await shared).clone();
        if (!response.ok) messageFlights.delete(signature);
      } else {
        response = await downstreamFetch(input, effectiveInit);
      }
    } catch (error) {
      if (transitionWrite && signature && createdCaseId) {
        const detail = '调查已创建，但核查未启动；正在打开已创建调查，请重新发起核查。';
        recover(createdCaseId, detail);
        throw new Error(detail);
      }
      throw error;
    }

    if (creatingCase && response.ok) {
      const created = await response.clone().json().catch(() => ({}));
      if (created?.id) createdCaseId = created.id;
    }

    if (transitionWrite && signature && !response.ok && createdCaseId) {
      const payload = await response.clone().json().catch(() => ({}));
      const detail = payload.detail || '调查已创建，但核查未启动；正在打开已创建调查，请重新发起核查。';
      recover(createdCaseId, detail);
      return conflictResponse(detail);
    }

    if (isCaseDetailRequest(meta) && response.ok && createdCaseId) {
      const snapshot = await response.clone().json().catch(() => ({}));
      if (snapshot?.id === createdCaseId) {
        createdCaseId = null;
        messageFlights.clear();
      }
    }
    return response;
  };
}

function notify(documentRef, message) {
  const toast = documentRef.querySelector?.('#toast');
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toast.__creationTimer);
  toast.__creationTimer = setTimeout(() => toast.classList.remove('show'), 3000);
}

export function installCreationGuards({ windowRef = window, documentRef = document } = {}) {
  const downstreamFetch = windowRef.fetch.bind(windowRef);
  windowRef.fetch = createCreationFetch({
    downstreamFetch,
    getHref: () => windowRef.location.href,
    onRecover: (caseId, detail) => {
      notify(documentRef, detail);
      setTimeout(() => {
        const url = new URL(windowRef.location.href);
        url.searchParams.set('case', caseId);
        windowRef.location.assign(url.toString());
      }, 650);
    },
  });
}
