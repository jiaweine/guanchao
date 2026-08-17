import { caseIdFromRequest, isCaseDetailRequest, requestMeta } from './runtime.mjs';

const KIND_NAMES = { image: '图片', video: '视频', audio: '音频', document: '文档', other: '素材' };
const MAX_UPLOAD_BYTES = 30 * 1024 * 1024;

export function assetDeletePath(caseId, assetId) {
  return `/api/cases/${encodeURIComponent(caseId)}/assets/${encodeURIComponent(assetId)}`;
}

export function canOfferAssetDelete(caseSnapshot, controlsWritable) {
  const latest = (caseSnapshot?.runs || [])[0];
  return Boolean(
    caseSnapshot?.id
    && caseSnapshot.status !== 'archived'
    && latest?.status !== 'running'
    && controlsWritable
  );
}

export function removeAssetFromSnapshot(snapshot, assetId) {
  snapshot.assets = (snapshot.assets || []).filter((item) => item.id !== assetId);
  return snapshot.assets;
}

export function validateUploadSelection(files, maxBytes = MAX_UPLOAD_BYTES) {
  const selected = [...files];
  const empty = selected.find((file) => !Number(file.size));
  if (empty) return { ok: false, message: `“${empty.name || '素材'}”是空文件，请重新选择。` };
  const oversized = selected.find((file) => Number(file.size) > maxBytes);
  if (oversized) return { ok: false, message: `“${oversized.name || '素材'}”超过 30MB，请压缩或拆分后再上传。` };
  return { ok: true, message: '' };
}

export function isCaseReportRequest(meta) {
  return meta.method === 'GET' && /^\/api\/cases\/[^/]+\/report$/.test(meta.url.pathname);
}

export function shouldDiscardCaseBoundResult(meta, selectedCaseId) {
  const requestCaseId = caseIdFromRequest(meta);
  return Boolean(isCaseReportRequest(meta) && requestCaseId && selectedCaseId && requestCaseId !== selectedCaseId);
}

function isCaseSnapshotRequest(meta) {
  if (isCaseDetailRequest(meta)) return true;
  return meta.method === 'PATCH' && /^\/api\/cases\/[^/]+(?:\/target)?$/.test(meta.url.pathname);
}

function staleResultResponse(caseId) {
  return new Response(
    JSON.stringify({ detail: `已切换到另一调查，已取消原调查 ${caseId} 的结果交付。` }),
    { status: 409, headers: { 'content-type': 'application/json; charset=utf-8' } },
  );
}

function notify(documentRef, message) {
  const toast = documentRef.querySelector?.('#toast');
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(toast.__interactionTimer);
  toast.__interactionTimer = setTimeout(() => toast.classList.remove('show'), 2600);
}

function ensureInteractionStyle(documentRef) {
  if (documentRef.querySelector?.('#interactionStyle')) return;
  const style = documentRef.createElement?.('style');
  if (!style) return;
  style.id = 'interactionStyle';
  style.textContent = `
    .asset-top{justify-content:flex-start}.asset-top .asset-state{margin-left:auto}
    .asset-remove{border:0;background:transparent;color:var(--danger);font-size:12px;line-height:1;padding:6px 2px 6px 8px;min-height:28px}
    .asset-remove:hover{text-decoration:underline}.asset-remove:disabled{opacity:.45}
  `;
  documentRef.head?.appendChild(style);
}

function visibleDialog(documentRef) {
  const backdrop = [...documentRef.querySelectorAll?.('.modal-backdrop') || []].find((item) => !item.hidden);
  return backdrop?.querySelector?.('.modal') || null;
}

function dialogFocusables(dialog) {
  if (!dialog) return [];
  return [...dialog.querySelectorAll?.('button:not([disabled]),input:not([disabled]):not([type="hidden"]),textarea:not([disabled]),select:not([disabled]),a[href],[tabindex]:not([tabindex="-1"])') || []]
    .filter((element) => !element.hidden && !element.closest?.('[hidden]'));
}

function prepareDialogSemantics(documentRef) {
  [...documentRef.querySelectorAll?.('.modal') || []].forEach((dialog, index) => {
    dialog.setAttribute('role', 'dialog');
    dialog.setAttribute('aria-modal', 'true');
    const title = dialog.querySelector?.('h3');
    if (title) {
      if (!title.id) title.id = `dialogTitle${index + 1}`;
      dialog.setAttribute('aria-labelledby', title.id);
    }
  });

  const tabList = documentRef.querySelector?.('.insight-tabs');
  if (tabList) tabList.setAttribute('role', 'tablist');
  [...documentRef.querySelectorAll?.('.insight-tabs button') || []].forEach((button, index) => {
    const panel = documentRef.querySelector?.(`#tab-${button.dataset.tab}`);
    if (!panel) return;
    if (!button.id) button.id = `insightTab${index + 1}`;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-controls', panel.id);
    button.setAttribute('aria-selected', button.classList.contains('active') ? 'true' : 'false');
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', button.id);
  });

  [...documentRef.querySelectorAll?.('button[title]') || []].forEach((button) => {
    if (!button.getAttribute('aria-label')) button.setAttribute('aria-label', button.getAttribute('title'));
  });
}

function syncTabSelection(documentRef) {
  [...documentRef.querySelectorAll?.('.insight-tabs button') || []].forEach((button) => {
    button.setAttribute('aria-selected', button.classList.contains('active') ? 'true' : 'false');
  });
}

function installModalKeyboard(documentRef) {
  documentRef.addEventListener('keydown', (event) => {
    const tab = event.target?.closest?.('[role="tab"]');
    if (tab && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      const tabs = [...documentRef.querySelectorAll?.('.insight-tabs [role="tab"]') || []];
      const index = tabs.indexOf(tab);
      if (index >= 0) {
        event.preventDefault();
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
        tabs[next]?.focus?.();
        tabs[next]?.click?.();
      }
      return;
    }
    if (event.key !== 'Tab') return;
    const dialog = visibleDialog(documentRef);
    if (!dialog) return;
    const focusables = dialogFocusables(dialog);
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (!dialog.contains(documentRef.activeElement)) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && documentRef.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && documentRef.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }, true);
}

function installModalInertState(windowRef, documentRef) {
  const shell = documentRef.querySelector?.('.app-shell');
  if (!shell) return;
  const sync = () => {
    shell.inert = Boolean(visibleDialog(documentRef));
  };
  sync();
  if (typeof windowRef.MutationObserver !== 'function') return;
  const observer = new windowRef.MutationObserver(sync);
  [...documentRef.querySelectorAll?.('.modal-backdrop') || []].forEach((modal) => {
    observer.observe(modal, { attributes: true, attributeFilter: ['hidden'] });
  });
}

function installUploadPreflight(documentRef) {
  documentRef.addEventListener('change', (event) => {
    if (!['assetInput', 'modalAssetInput'].includes(event.target?.id)) return;
    const result = validateUploadSelection(event.target.files || []);
    if (result.ok) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    event.target.value = '';
    notify(documentRef, result.message);
  }, true);

  documentRef.addEventListener('drop', (event) => {
    if (!event.dataTransfer?.files?.length) return;
    const result = validateUploadSelection(event.dataTransfer.files);
    if (result.ok) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    const layer = documentRef.querySelector?.('#dropLayer');
    if (layer) layer.hidden = true;
    notify(documentRef, result.message);
  }, true);
}

function syncAssetTray(documentRef, assets) {
  const tray = documentRef.querySelector?.('#uploadTray');
  if (!tray) return;
  tray.replaceChildren();
  tray.hidden = !assets.length;
  assets.slice(-6).forEach((asset) => {
    const chip = documentRef.createElement('span');
    chip.className = 'upload-chip';
    chip.dataset.assetId = asset.id;
    const kind = documentRef.createElement('b');
    kind.textContent = KIND_NAMES[asset.kind] || '素材';
    chip.append(kind, documentRef.createTextNode(asset.name || '素材'));
    tray.appendChild(chip);
  });
}

function syncAssetDeleteActions(windowRef, documentRef, snapshot) {
  const attach = documentRef.querySelector?.('#attachBtn');
  const canDelete = canOfferAssetDelete(snapshot, Boolean(attach && !attach.disabled));
  const rows = [...documentRef.querySelectorAll?.('#assetList .asset-item') || []];
  if (!canDelete) {
    rows.forEach((row) => row.querySelector?.('[data-asset-delete]')?.remove());
    return;
  }
  const assets = snapshot.assets || [];
  rows.forEach((row, index) => {
    const asset = assets[index];
    if (!asset?.id || row.querySelector?.('[data-asset-delete]')) return;
    const top = row.querySelector?.('.asset-top');
    if (!top) return;
    const button = documentRef.createElement('button');
    button.type = 'button';
    button.className = 'asset-remove';
    button.dataset.assetDelete = asset.id;
    button.textContent = '移除';
    button.setAttribute('aria-label', `移除素材 ${asset.name || ''}`.trim());
    button.addEventListener('click', async () => {
      if (!windowRef.confirm('移除这份素材？该文件会从当前调查中永久删除。')) return;
      button.disabled = true;
      try {
        const response = await windowRef.fetch(assetDeletePath(snapshot.id, asset.id), { method: 'DELETE' });
        if (!response.ok) {
          const payload = await response.clone().json().catch(() => ({}));
          throw new Error(payload.detail || '素材移除失败');
        }
        row.remove();
        const remaining = removeAssetFromSnapshot(snapshot, asset.id);
        syncAssetTray(documentRef, remaining);
        if (!remaining.length) {
          const list = documentRef.querySelector?.('#assetList');
          if (list) list.innerHTML = '<div class="side-empty">还没有添加素材。</div>';
        }
        notify(documentRef, '素材已移除');
      } catch (error) {
        button.disabled = false;
        notify(documentRef, error.message || '素材移除失败');
      }
    });
    top.appendChild(button);
  });
}

export function installInteractionEnhancements({ windowRef = window, documentRef = document } = {}) {
  ensureInteractionStyle(documentRef);
  prepareDialogSemantics(documentRef);
  installModalKeyboard(documentRef);
  installModalInertState(windowRef, documentRef);
  installUploadPreflight(documentRef);

  let selectedSnapshot = null;
  const assetList = documentRef.querySelector?.('#assetList');
  if (assetList && typeof windowRef.MutationObserver === 'function') {
    const observer = new windowRef.MutationObserver(() => {
      if (selectedSnapshot) syncAssetDeleteActions(windowRef, documentRef, selectedSnapshot);
    });
    observer.observe(assetList, { childList: true, subtree: true });
  }

  documentRef.addEventListener('click', (event) => {
    if (event.target.closest?.('.insight-tabs button')) queueMicrotask(() => syncTabSelection(documentRef));
  });

  const guardedFetch = windowRef.fetch.bind(windowRef);
  windowRef.fetch = async (input, init = {}) => {
    const meta = requestMeta(input, init, windowRef.location.href);
    const response = await guardedFetch(input, init);

    if (shouldDiscardCaseBoundResult(meta, selectedSnapshot?.id) && response.ok) {
      return staleResultResponse(caseIdFromRequest(meta));
    }

    if (isCaseSnapshotRequest(meta) && response.ok) {
      response.clone().json().then((payload) => {
        const snapshot = payload?.case || payload;
        if (!snapshot?.id || snapshot.id === '__stale_case_request__') return;
        selectedSnapshot = snapshot;
        queueMicrotask(() => syncAssetDeleteActions(windowRef, documentRef, snapshot));
      }).catch(() => {});
    }
    return response;
  };
}
