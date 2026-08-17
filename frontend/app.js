const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

const state = {
  caseId: null,
  runId: null,
  run: null,
  case: null,
  polling: null,
  pendingFiles: [],
  listMode: 'queue',
};

const platformNames = {
  xiaohongshu: '小红书',
  weibo: '微博',
  douyin: '抖音',
  bilibili: 'B站',
  other: '其他平台',
};
const kindNames = { image: '图片', video: '视频', audio: '音频', document: '文档', other: '素材' };
const reviewLabels = {
  confirm_ordinary: '普通创作者',
  uncertain: '无法判断',
  confirm_marketing: '营销运营',
};

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const response = await fetch(path, { ...options, headers });
  const type = response.headers.get('content-type') || '';
  const data = type.includes('application/json')
    ? await response.json().catch(() => ({}))
    : await response.text();
  if (!response.ok) throw new Error((data && data.detail) || data || '请求失败');
  return data;
}

function toast(message) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.add('show');
  clearTimeout(element._timer);
  element._timer = setTimeout(() => element.classList.remove('show'), 2200);
}

function pct(value) {
  return `${Math.round((Number(value) || 0) * 100)}%`;
}

function escapeHtml(value = '') {
  return String(value).replace(/[&<>'"]/g, (char) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[char]);
}

function formatTime(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function loadWorkspaceStatus() {
  try {
    const status = await api('/api/status');
    $('#queueSummary').textContent = `待复核 ${status.pending_review || 0}`;
  } catch {
    $('#queueSummary').textContent = '待复核 —';
  }
}

async function loadCases() {
  try {
    const queueMode = state.listMode === 'queue';
    const items = await api(queueMode ? '/api/review-queue?reviewed=false' : '/api/cases');
    const list = $('#caseList');
    $('#listTitle').textContent = queueMode ? '待复核' : '全部调查';
    $('#listModeBtn').textContent = queueMode ? '查看全部' : '只看待复核';

    if (!items.length) {
      list.innerHTML = queueMode
        ? '<div class="case-empty"><strong>没有待复核任务</strong><p>新结论完成后会自动进入这里。</p></div>'
        : '<div class="case-empty"><strong>还没有调查</strong><p>从一个账号、一组内容或几份素材开始。</p></div>';
      await loadWorkspaceStatus();
      return;
    }

    list.innerHTML = items.map((item) => {
      const id = item.case_id || item.id;
      const target = item.targets?.[0] || {};
      const platform = platformNames[target.platform] || '调查';
      const queueMeta = item.result
        ? `${platform} · ${item.result.label || '待判断'} · 把握度 ${pct(item.result.confidence)}`
        : `${platform} · ${formatTime(item.updated_at)}`;
      return `<button class="case-item ${id === state.caseId ? 'active' : ''}" data-id="${escapeHtml(id)}"><span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(queueMeta)}</small></span></button>`;
    }).join('');

    $$('.case-item').forEach((button) => button.addEventListener('click', () => openCase(button.dataset.id)));
    await loadWorkspaceStatus();
  } catch (error) {
    toast(error.message);
  }
}

function setReviewControls(item, latest) {
  const tools = $('#workspaceTools');
  const reviewHint = $('#reviewHint');
  const reviewButtons = [$('#markNormalBtn'), $('#markUncertainBtn'), $('#markMarketingBtn')];
  tools.hidden = false;
  $('#deleteCaseBtn').hidden = false;
  $('#deleteCaseBtn').disabled = latest?.status === 'running';

  const finished = latest?.status === 'completed' && latest?.state?.primary_result?.label;
  reviewButtons.forEach((button) => { button.hidden = !finished; });
  reviewHint.hidden = !finished;
  if (!finished) return;

  const review = (item.reviews || []).find((entry) => entry.run_id === latest.id);
  reviewHint.textContent = review ? `已复核：${reviewLabels[review.decision] || '已确认'}` : '人工复核';
}

async function openCase(id) {
  if (state.caseId && state.caseId !== id && state.polling) {
    clearInterval(state.polling);
    state.polling = null;
  }
  try {
    const item = await api(`/api/cases/${id}`);
    state.caseId = id;
    state.case = item;
    $('#welcome').hidden = true;
    $('#caseTitle').textContent = item.title;
    const target = item.targets?.[0] || {};
    $('#casePlatform').textContent = `${platformNames[target.platform] || '跨平台'} · ${target.display_name || target.handle || '账号调查'}`;
    $('#caseMeta').textContent = item.goal;
    renderMessages(item.messages || []);
    renderAssets(item.assets || []);

    const latest = (item.runs || [])[0];
    state.runId = latest?.id || null;
    setReviewControls(item, latest);
    if (latest) {
      renderRun(latest);
      if (latest.status === 'running') beginPolling(latest.id);
    } else {
      clearInsight();
    }
    await loadCases();
  } catch (error) {
    toast(error.message);
  }
}

function renderMessages(messages) {
  $('#messageList').innerHTML = messages.map((message) => `
    <article class="message ${message.role}">
      <span class="role">${message.role === 'user' ? '你' : '观潮'}</span>
      <div class="bubble">${escapeHtml(message.content).replace(/\n/g, '<br>')}</div>
    </article>
  `).join('');
  $('#conversation').scrollTop = $('#conversation').scrollHeight;
}

function renderRun(run) {
  state.run = run;
  const current = run.state || {};
  const events = current.events || [];
  $('#runStrip').hidden = run.status !== 'running';
  if (run.status === 'running') {
    const last = events[events.length - 1] || {};
    $('#runDetail').textContent = last.detail || last.title || '正在选择下一步';
  }
  renderTrace(events);
  renderEvidence(current.evidence || []);
  if (current.primary_result?.label || run.status === 'completed') renderSummary(current.primary_result || {});
}

function renderSummary(result) {
  if (!result || !Object.keys(result).length) return;
  $('#summaryEmpty').hidden = true;
  $('#summaryContent').hidden = false;
  $('#verdictLabel').textContent = result.label || '待补资料';
  $('#confidenceValue').textContent = pct(result.confidence);
  $('#confidenceMeter').style.width = pct(result.confidence);
  $('#stabilityValue').textContent = pct(result.stability);
  $('#stabilityMeter').style.width = pct(result.stability);
  $('#marketingValue').textContent = pct(result.marketing_likelihood);
  $('#covertValue').textContent = pct(result.covert_promotion_risk);
  $('#summaryCopy').textContent = result.summary || '正在整理判断。';
  const missing = result.missing || [];
  $('#missingBlock').hidden = !missing.length;
  $('#missingList').innerHTML = missing.map((item) => `<li>${escapeHtml(item)}</li>`).join('');
}

function renderEvidence(evidence) {
  const seen = new Map();
  evidence.forEach((item) => {
    const key = `${item.key}:${item.direction}`;
    const old = seen.get(key);
    if (!old || (item.strength || 0) > (old.strength || 0)) seen.set(key, item);
  });
  const items = [...seen.values()].sort((a, b) => (b.strength || 0) - (a.strength || 0));
  $('#evidenceList').innerHTML = items.length
    ? items.map((item) => `
      <article class="evidence-item ${item.direction || 'context'}">
        <div class="evidence-top"><strong>${escapeHtml(item.title)}</strong><em>${item.direction === 'against' ? '反向线索' : item.direction === 'supports' ? '支持判断' : '背景'}</em></div>
        <p>${escapeHtml(item.detail)}</p>
      </article>
    `).join('')
    : '<div class="side-empty">还没有足够明确的关键证据。</div>';
}

function renderAssets(assets) {
  $('#assetList').innerHTML = assets.length
    ? assets.map((asset) => `
      <article class="asset-item">
        <div class="asset-top"><strong>${escapeHtml(asset.name)}</strong><span class="asset-state ${asset.status}">${asset.status === 'ready' ? '已读取' : asset.status === 'error' ? '读取失败' : '待读取'}</span></div>
        <p>${kindNames[asset.kind] || '素材'} · ${formatSize(asset.size || 0)}${asset.note ? ` · ${escapeHtml(asset.note)}` : ''}</p>
      </article>
    `).join('')
    : '<div class="side-empty">还没有添加素材。</div>';
  renderUploadTray(assets);
}

function renderTrace(events) {
  $('#traceList').innerHTML = events.length
    ? events.map((event) => `
      <article class="trace-item ${event.status || 'done'}"><div><strong>${escapeHtml(event.title)}</strong><p>${escapeHtml(event.detail || '')}</p></div></article>
    `).join('')
    : '<div class="side-empty">每一步核查都会留在这里。</div>';
}

function clearInsight() {
  $('#summaryEmpty').hidden = false;
  $('#summaryContent').hidden = true;
  $('#evidenceList').innerHTML = '<div class="side-empty">关键证据会按支持、反向和背景分别保留。</div>';
  $('#traceList').innerHTML = '<div class="side-empty">每一步核查都会留在这里。</div>';
}

function renderUploadTray(assets) {
  const tray = $('#uploadTray');
  tray.hidden = !assets.length;
  tray.innerHTML = assets.slice(-6).map((asset) => `<span class="upload-chip"><b>${kindNames[asset.kind] || '素材'}</b>${escapeHtml(asset.name)}</span>`).join('');
}

async function sendMessage() {
  const input = $('#messageInput');
  const content = input.value.trim();
  if (!content) return;
  if (!state.caseId) {
    openModal();
    return;
  }
  input.value = '';
  autosize(input);
  try {
    const out = await api(`/api/cases/${state.caseId}/messages`, {
      method: 'POST', body: JSON.stringify({ content }),
    });
    state.runId = out.run_id;
    const item = await api(`/api/cases/${state.caseId}`);
    state.case = item;
    renderMessages(item.messages || []);
    setReviewControls(item, (item.runs || [])[0]);
    beginPolling(out.run_id);
  } catch (error) {
    toast(error.message);
  }
}

function beginPolling(runId) {
  clearInterval(state.polling);
  const tick = async () => {
    if (runId !== state.runId) return;
    try {
      const run = await api(`/api/runs/${runId}`);
      if (runId !== state.runId) return;
      renderRun(run);
      if (run.status !== 'running') {
        clearInterval(state.polling);
        state.polling = null;
        await openCase(state.caseId);
      }
    } catch (error) {
      clearInterval(state.polling);
      state.polling = null;
      toast(error.message);
    }
  };
  tick();
  state.polling = setInterval(tick, 650);
}

async function loadDemo() {
  try {
    const out = await api('/api/demo', { method: 'POST' });
    state.caseId = out.case.id;
    state.runId = out.run_id;
    await openCase(out.case.id);
    toast('示例调查已开始');
  } catch (error) {
    toast(error.message);
  }
}

function openModal() {
  state.pendingFiles = [];
  renderSelectedFiles();
  $('#caseModal').hidden = false;
  $('#handleInput').focus();
}

function closeModal() {
  $('#caseModal').hidden = true;
  state.pendingFiles = [];
  renderSelectedFiles();
}

function addPendingFiles(files) {
  state.pendingFiles = [...state.pendingFiles, ...files].slice(0, 12);
  renderSelectedFiles();
}

function renderSelectedFiles() {
  const element = $('#selectedFiles');
  element.hidden = !state.pendingFiles.length;
  element.innerHTML = state.pendingFiles.map((file) => `<span>${escapeHtml(file.name)} · ${formatSize(file.size)}</span>`).join('');
}

async function uploadFiles(caseId, files) {
  for (const file of files) {
    const form = new FormData();
    form.append('file', file);
    await api(`/api/cases/${caseId}/assets`, { method: 'POST', body: form });
  }
}

async function uploadToCurrent(files) {
  if (!files.length) return;
  if (!state.caseId) {
    openModal();
    addPendingFiles(files);
    return;
  }
  try {
    toast(`正在加入 ${files.length} 份素材`);
    await uploadFiles(state.caseId, files);
    const item = await api(`/api/cases/${state.caseId}`);
    state.case = item;
    renderAssets(item.assets || []);
    toast('素材已加入当前调查');
  } catch (error) {
    toast(error.message);
  }
}

async function createCase(event) {
  event.preventDefault();
  const posts = $('#postsInput').value
    .split(/\n+/)
    .map((text) => text.trim())
    .filter(Boolean)
    .map((text, index) => ({ id: `p${index + 1}`, text }));
  if (!posts.length && !state.pendingFiles.length) {
    toast('请至少添加一条内容或一份素材');
    return;
  }

  const target = {
    platform: $('#platformInput').value,
    handle: $('#handleInput').value.trim(),
    display_name: $('#handleInput').value.trim(),
    bio: $('#bioInput').value.trim(),
    posts,
  };
  const goal = $('#goalInput').value.trim();
  const files = [...state.pendingFiles];

  try {
    const item = await api('/api/cases', {
      method: 'POST',
      body: JSON.stringify({ title: `${target.display_name} · 内容调查`, goal, targets: [target] }),
    });
    if (files.length) await uploadFiles(item.id, files);
    closeModal();
    state.caseId = item.id;
    await openCase(item.id);
    $('#messageInput').value = goal;
    await sendMessage();
    $('#caseForm').reset();
  } catch (error) {
    toast(error.message);
  }
}

async function submitReview(decision) {
  if (!state.caseId || !state.runId) return;
  const reasons = {
    confirm_ordinary: '人工确认当前证据更支持普通创作者',
    uncertain: '当前资料不足或存在争议，暂不做二元判断',
    confirm_marketing: '人工确认当前证据支持营销运营判断',
  };
  try {
    await api('/api/reviews', {
      method: 'POST',
      body: JSON.stringify({
        case_id: state.caseId,
        run_id: state.runId,
        decision,
        reason: reasons[decision],
      }),
    });
    toast(`已复核：${reviewLabels[decision]}`);
    await openCase(state.caseId);
  } catch (error) {
    toast(error.message);
  }
}

async function deleteCurrentCase() {
  if (!state.caseId) return;
  if (!window.confirm('删除这个调查及其素材和执行记录？此操作不可撤销。')) return;
  try {
    await api(`/api/cases/${state.caseId}`, { method: 'DELETE' });
    clearInterval(state.polling);
    state.caseId = null;
    state.runId = null;
    state.run = null;
    state.case = null;
    $('#workspaceTools').hidden = true;
    $('#welcome').hidden = false;
    $('#messageList').innerHTML = '';
    $('#casePlatform').textContent = '跨平台调查';
    $('#caseTitle').textContent = '查清一个账号，不只看一条内容。';
    $('#caseMeta').textContent = '给出目标和现有资料。系统会自己决定先看什么、哪里需要反向核对，以及什么时候证据足够。';
    renderAssets([]);
    clearInsight();
    await loadCases();
    toast('调查已删除');
  } catch (error) {
    toast(error.message);
  }
}

function switchTab(tab) {
  $$('.insight-tabs button').forEach((button) => button.classList.toggle('active', button.dataset.tab === tab));
  $$('.tab-panel').forEach((panel) => panel.classList.remove('active'));
  $(`#tab-${tab}`).classList.add('active');
}

function autosize(element) {
  element.style.height = 'auto';
  element.style.height = `${Math.min(150, element.scrollHeight)}px`;
}

function toggleListMode() {
  state.listMode = state.listMode === 'queue' ? 'all' : 'queue';
  loadCases();
}

$('#sendBtn').addEventListener('click', sendMessage);
$('#messageInput').addEventListener('input', (event) => autosize(event.target));
$('#messageInput').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});
$('#demoBtn').addEventListener('click', loadDemo);
$$('[data-demo="1"]').forEach((button) => button.addEventListener('click', loadDemo));
$('#newCaseBtn').addEventListener('click', openModal);
$('#newCaseRailBtn').addEventListener('click', openModal);
$$('[data-open="new"]').forEach((button) => button.addEventListener('click', openModal));
$('#closeModal').addEventListener('click', closeModal);
$('#cancelModal').addEventListener('click', closeModal);
$('#caseModal').addEventListener('click', (event) => { if (event.target.id === 'caseModal') closeModal(); });
$('#caseForm').addEventListener('submit', createCase);
$('#listModeBtn').addEventListener('click', toggleListMode);
$('#markNormalBtn').addEventListener('click', () => submitReview('confirm_ordinary'));
$('#markUncertainBtn').addEventListener('click', () => submitReview('uncertain'));
$('#markMarketingBtn').addEventListener('click', () => submitReview('confirm_marketing'));
$('#deleteCaseBtn').addEventListener('click', deleteCurrentCase);
$$('.insight-tabs button').forEach((button) => button.addEventListener('click', () => switchTab(button.dataset.tab)));
$('#attachBtn').addEventListener('click', () => $('#assetInput').click());
$('#assetInput').addEventListener('change', (event) => {
  uploadToCurrent([...event.target.files]);
  event.target.value = '';
});
$('#modalAttachBtn').addEventListener('click', () => $('#modalAssetInput').click());
$('#modalAssetInput').addEventListener('change', (event) => {
  addPendingFiles([...event.target.files]);
  event.target.value = '';
});

let dragDepth = 0;
document.addEventListener('dragenter', (event) => {
  if ([...event.dataTransfer?.types || []].includes('Files')) {
    dragDepth += 1;
    $('#dropLayer').hidden = false;
  }
});
document.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $('#dropLayer').hidden = true;
});
document.addEventListener('dragover', (event) => {
  if ([...event.dataTransfer?.types || []].includes('Files')) event.preventDefault();
});
document.addEventListener('drop', (event) => {
  if (event.dataTransfer?.files?.length) {
    event.preventDefault();
    dragDepth = 0;
    $('#dropLayer').hidden = true;
    uploadToCurrent([...event.dataTransfer.files]);
  }
});

loadCases();
