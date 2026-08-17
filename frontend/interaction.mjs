import { isCaseDetailRequest, requestMeta } from './runtime.mjs';

const KIND_NAMES = { image: '图片', video: '视频', audio: '音频', document: '文档', other: '素材' };

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
    .asset-remove{border:0;background:transparent;color:var(--danger);font-size:8px;padding:3px 0 3px 6px}
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
    .filter((element) => !element.hidden);
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
}

function syncTabSelection(documentRef) {
  [...documentRef.querySelectorAll?.('.insight-tabs button') || []].forEach((button) => {
    button.setAttribute('aria-selected', button.classList.contains('active') ? 'true' : 'false');
  });
}

function installModalKeyboard(documentRef) {
  documentRef.addEventListener('keydown', (event) => {
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

function decorateAssetDeleteActions(windowRef, documentRef, snapshot) {
  const attach = documentRef.querySelector?.('#attachBtn');
  const canDelete = canOfferAssetDelete(snapshot, Boolean(attach && !attach.disabled));
  if (!canDelete) return;
  const assets = snapshot.assets || [];
  const rows = [...documentRef.querySelectorAll?.('#assetList .asset-item') || []];
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

  documentRef.addEventListener('click', (event) => {
    if (event.target.closest?.('.insight-tabs button')) queueMicrotask(() => syncTabSelection(documentRef));
  });

  const guardedFetch = windowRef.fetch.bind(windowRef);
  windowRef.fetch = async (input, init = {}) => {
    const meta = requestMeta(input, init, windowRef.location.href);
    const response = await guardedFetch(input, init);
    if (isCaseDetailRequest(meta) && response.ok) {
      response.clone().json().then((snapshot) => {
        if (!snapshot?.id || snapshot.id === '__stale_case_request__') return;
        queueMicrotask(() => decorateAssetDeleteActions(windowRef, documentRef, snapshot));
        setTimeout(() => decorateAssetDeleteActions(windowRef, documentRef, snapshot), 20);
      }).catch(() => {});
    }
    return response;
  };
}
