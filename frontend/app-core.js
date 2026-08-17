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
  members: [],
  session: null,
  viewedRunIds: new Set(),
  listRequest: 0,
};

const platformNames = {
  xiaohongshu: '小红书',
  weibo: '微博',
  douyin: '抖音',
  bilibili: 'B站',
  other: '其他平台',
};
const platformCodes = {
  '小红书': 'xiaohongshu', xiaohongshu: 'xiaohongshu',
  '微博': 'weibo', weibo: 'weibo',
  '抖音': 'douyin', douyin: 'douyin',
  'b站': 'bilibili', 'B站': 'bilibili', bilibili: 'bilibili',
  other: 'other', '其他': 'other',
};
const kindNames = { image: '图片', video: '视频', audio: '音频', document: '文档', other: '素材' };
const reviewLabels = {
  confirm_ordinary: '普通创作者',
  uncertain: '无法判断',
  confirm_marketing: '营销运营',
};
const priorityLabels = { high: '高优先', normal: '常规', low: '低优先' };
const roleLabels = { admin: '管理员', analyst: '分析员', reviewer: '复核员' };
const eventLabels = {
  case_created: '创建调查',
  case_updated: '更新调查设置',
  case_deleted: '删除调查',
  source_refreshed: '更新账号资料',
  asset_added: '添加素材',
  asset_deleted: '删除素材',
  run_started: '开始核查',
  run_completed: '核查完成',
  run_failed: '核查中断',
  review_submitted: '提交人工复核',
  case_opened: '打开调查',
  batch_created: '创建批量调查',
  learning_run: '完成学习回放',
  comment_added: '添加协作备注',
};

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (!(options.body instanceof FormData) && options.body !== undefined) headers['Content-Type'] = 'application/json';
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
  element._timer = setTimeout(() => element.classList.remove('show'), 2400);
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

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟`;
  return `${(seconds / 3600).toFixed(1)} 小时`;
}

function currentMember() {
  return state.session;
}

function ownerMembers() {
  return state.members.filter((item) => item.active !== false && ['admin', 'analyst'].includes(item.role));
}

function canManageCases() {
  return ['admin', 'analyst'].includes(currentMember()?.role);
}

function canDelete() {
  return currentMember()?.role === 'admin';
}

function setSelectOptions(element, items, { includeAll = false, selected = '' } = {}) {
  const first = includeAll ? '<option value="">全部负责人</option>' : '';
  element.innerHTML = first + items.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.display_name)}</option>`).join('');
  element.value = selected || (includeAll ? '' : currentMember()?.id || 'local');
}

function applySessionUI() {
  const canManage = canManageCases();
  ['#newCaseBtn', '#batchBtn', '#newCaseRailBtn'].forEach((selector) => {
    const button = $(selector);
    if (button) button.disabled = !canManage;
  });
  $$('[data-open="new"], [data-open="batch"], [data-demo="1"]').forEach((button) => {
    button.disabled = !canManage;
  });
}

async function loadSession() {
  state.session = await api('/api/session');
}

async function loadMembers() {
  state.members = await api('/api/members');
  const owners = ownerMembers();
  setSelectOptions($('#ownerFilter'), owners, { includeAll: true });
  setSelectOptions($('#newOwnerSelect'), owners, { selected: currentMember()?.id });
  setSelectOptions($('#batchOwnerSelect'), owners, { selected: currentMember()?.id });
  applySessionUI();
}


async function loadWorkspaceStatus() {
  try {
    const status = await api('/api/status');
    $('#queueSummary').textContent = `待复核 ${status.pending_review || 0}`;
    $('#monitorSummary').textContent = `待更新 ${status.monitoring_due || 0}`;
    const actorName = currentMember()?.display_name || '工作空间';
    $('#workspaceState').textContent = `${actorName} · 工作空间可用`;
    return status;
  } catch {
    $('#queueSummary').textContent = '待复核 —';
    $('#monitorSummary').textContent = '待更新 —';
    return null;
  }
}

function activeFilters() {
  return {
    query: $('#caseSearch').value.trim(),
    platform: $('#platformFilter').value,
    owner: $('#ownerFilter').value,
    priority: $('#priorityFilter').value,
    sort: $('#sortFilter').value,
  };
}

function filterWatchItems(items, filters) {
  const normalized = filters.query.toLowerCase();
  return items.filter((item) => {
    const target = item.targets?.[0] || {};
    if (normalized) {
      const haystack = [item.title, item.goal, target.handle, target.display_name].join(' ').toLowerCase();
      if (!haystack.includes(normalized)) return false;
    }
    if (filters.platform && target.platform !== filters.platform) return false;
    if (filters.owner && item.owner !== filters.owner) return false;
    if (filters.priority && item.priority !== filters.priority) return false;
    return true;
  });
}

function modeCopy() {
  if (state.listMode === 'queue') return '高风险、低稳定或业务高优先的结果会优先进入复核。';
  if (state.listMode === 'watch') return '这里只显示已经到更新时间、需要补充最新资料的监测账号。';
  return '搜索、筛选和归档让调查数量增长后仍然保持可管理。';
}

async function loadCases() {
  const requestId = ++state.listRequest;
  try {
    const filters = activeFilters();
    const params = new URLSearchParams();
    if (filters.query) params.set('query', filters.query);
    if (filters.platform) params.set('platform', filters.platform);
    if (filters.owner) params.set('owner', filters.owner);
    if (filters.priority) params.set('priority', filters.priority);

    let items;
    if (state.listMode === 'queue') {
      params.set('reviewed', 'false');
      params.set('sort', filters.sort);
      items = await api(`/api/review-queue?${params.toString()}`);
    } else if (state.listMode === 'watch') {
      items = filterWatchItems(await api('/api/monitoring?due_only=true'), filters);
    } else {
      params.set('status', 'all');
      const allSort = filters.sort === 'risk_desc' ? 'risk_desc' : filters.sort === 'newest' ? 'updated_desc' : 'updated_desc';
      params.set('sort', allSort);
      items = await api(`/api/cases?${params.toString()}`);
    }
    if (requestId !== state.listRequest) return;

    const list = $('#caseList');
    $('#railFootCopy').textContent = modeCopy();
    if (!items.length) {
      const empty = state.listMode === 'queue'
        ? ['没有待复核任务', '新结论完成后会自动进入这里。']
        : state.listMode === 'watch'
          ? ['没有到期监测', '需要更新资料的账号会出现在这里。']
          : ['没有匹配的调查', '调整搜索或筛选条件，或新建调查。'];
      list.innerHTML = `<div class="case-empty"><strong>${empty[0]}</strong><p>${empty[1]}</p></div>`;
      await loadWorkspaceStatus();
      return;
    }

    list.innerHTML = items.map((item) => renderCaseListItem(item)).join('');
    $$('.case-item').forEach((button) => button.addEventListener('click', () => openCase(button.dataset.id)));
    await loadWorkspaceStatus();
  } catch (error) {
    toast(error.message);
  }
}

function renderCaseListItem(item) {
  const id = item.case_id || item.id;
  const target = item.targets?.[0] || {};
  const platform = platformNames[target.platform] || '调查';
  const result = item.result || item.latest_result || {};
  const runStatus = item.latest_run_status;
  let statusText = '';
  if (state.listMode === 'watch') {
    statusText = `需更新 · ${item.next_check_at ? formatTime(item.next_check_at) : '现在'}`;
  } else if (result.label) {
    statusText = `${result.label} · 把握度 ${pct(result.confidence)}`;
  } else if (runStatus === 'running') {
    statusText = '正在核查';
  } else if (runStatus === 'failed') {
    statusText = '上次核查未完成';
  } else {
    statusText = formatTime(item.updated_at);
  }
  if (item.status === 'archived') statusText = `已归档 · ${statusText}`;
  const reviewPriority = item.review_priority;
  const priorityChip = reviewPriority !== null && reviewPriority !== undefined
    ? `<em>复核 ${Math.round(reviewPriority * 100)}</em>`
    : item.priority === 'high' ? '<em>高优先</em>' : '';
  return `<button class="case-item ${id === state.caseId ? 'active' : ''}" data-id="${escapeHtml(id)}">
    <span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(platform)} · ${escapeHtml(statusText)}</small></span>${priorityChip}
  </button>`;
}

function clearPolling() {
  clearInterval(state.polling);
  state.polling = null;
}

async function openCase(id) {
  if (state.caseId && state.caseId !== id && state.polling) clearPolling();
  try {
    const item = await api(`/api/cases/${id}`);
    if (id !== item.id) return;
    state.caseId = id;
    state.case = item;
    $('#welcome').hidden = true;
    $('#caseTitle').textContent = item.title;
    const target = item.targets?.[0] || {};
    $('#casePlatform').textContent = `${platformNames[target.platform] || '跨平台'} · ${target.display_name || target.handle || '账号调查'}`;
    const statusNote = item.status === 'archived' ? ' · 已归档' : item.monitoring_due ? ' · 需要更新资料' : '';
    $('#caseMeta').textContent = `${item.goal}${statusNote}`;
    renderCaseTags(item);
    renderMessages(item.messages || []);
    renderAssets(item.assets || []);

    const latest = (item.runs || [])[0];
    state.runId = latest?.id || null;
    setWorkspaceControls(item, latest);
    if (latest) {
      renderRun(latest);
      if (latest.status === 'running') beginPolling(latest.id, id);
      if (!state.viewedRunIds.has(latest.id)) {
        state.viewedRunIds.add(latest.id);
        api('/api/events', {
          method: 'POST',
          body: JSON.stringify({ event_type: 'case_opened', case_id: id, run_id: latest.id }),
        }).catch(() => {});
      }
    } else {
      clearInsight();
    }
    await loadAudit(id);
    history.replaceState(null, '', `/?case=${encodeURIComponent(id)}`);
    await loadCases();
  } catch (error) {
    toast(error.message);
  }
}

function renderCaseTags(item) {
  const element = $('#caseTags');
  const tags = [...(item.tags || [])];
  if (item.priority === 'high') tags.unshift('高优先');
  if (item.monitoring_enabled) tags.push(item.monitoring_due ? '待更新' : '监测中');
  if (item.status === 'archived') tags.push('已归档');
  element.hidden = !tags.length;
  element.innerHTML = tags.map((tag) => `<span>${escapeHtml(tag)}</span>`).join('');
}

function setWorkspaceControls(item, latest) {
  $('#workspaceTools').hidden = false;
  setSelectOptions($('#caseOwnerSelect'), ownerMembers(), { selected: item.owner });
  $('#casePrioritySelect').value = item.priority;
  $('#monitorInterval').value = String(item.monitoring_interval_hours || 168);
  $('#monitorBtn').textContent = item.monitoring_enabled ? '停止监测' : '加入监测';
  $('#archiveCaseBtn').textContent = item.status === 'archived' ? '恢复' : '归档';
  $('#deleteCaseBtn').disabled = latest?.status === 'running' || !canDelete();
  $('#refreshSourceBtn').disabled = latest?.status === 'running' || item.status === 'archived' || !canManageCases();
  $('#archiveCaseBtn').disabled = latest?.status === 'running' || !canManageCases();
  $('#monitorBtn').disabled = item.status === 'archived' || !canManageCases();
  $('#caseOwnerSelect').disabled = !canManageCases();
  $('#casePrioritySelect').disabled = !canManageCases();
  $('#monitorInterval').disabled = !canManageCases();
  $('#messageInput').disabled = item.status === 'archived' || !canManageCases();
  $('#sendBtn').disabled = item.status === 'archived' || !canManageCases();
  $('#attachBtn').disabled = item.status === 'archived' || !canManageCases();

  const finished = latest?.status === 'completed' && latest?.state?.primary_result?.label;
  $('#reviewActions').hidden = !finished;
  if (!finished) return;
  const review = (item.reviews || []).find((entry) => entry.run_id === latest.id);
  $('#reviewHint').textContent = review ? `已复核：${reviewLabels[review.decision] || '已确认'}` : '人工复核 · 完成后自动打开下一条';
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

function renderTeamNotes(comments = []) {
  $('#teamNotes').innerHTML = comments.length
    ? comments.map((comment) => {
      const member = state.members.find((item) => item.id === comment.author);
      return `<article class="team-note"><div><strong>${escapeHtml(member?.display_name || comment.author)}</strong><small>${formatTime(comment.created_at)}</small></div><p>${escapeHtml(comment.content).replace(/\n/g, '<br>')}</p></article>`;
    }).join('')
    : '<div class="side-empty">还没有协作备注。</div>';
}

async function loadAudit(caseId) {
  renderTeamNotes(state.case?.comments || []);
  try {
    const events = await api(`/api/audit?case_id=${encodeURIComponent(caseId)}&limit=80`);
    $('#auditList').innerHTML = events.length
      ? events.map((event) => {
        const member = state.members.find((item) => item.id === event.actor);
        return `<article class="audit-item"><span>${escapeHtml(eventLabels[event.event_type] || '记录')}</span><div><strong>${escapeHtml(member?.display_name || event.actor)}</strong><small>${formatTime(event.created_at)}</small></div></article>`;
      }).join('')
      : '<div class="side-empty">还没有团队操作记录。</div>';
  } catch {
    $('#auditList').innerHTML = '<div class="side-empty">记录暂时不可用。</div>';
  }
}

async function addCaseNote() {
  if (!state.caseId) return;
  const input = $('#caseNoteInput');
  const content = input.value.trim();
  if (!content) return;
  try {
    await api(`/api/cases/${state.caseId}/comments`, {
      method: 'POST',
      body: JSON.stringify({ content }),
    });
    input.value = '';
    const item = await api(`/api/cases/${state.caseId}`);
    if (item.id !== state.caseId) return;
    state.case = item;
    renderTeamNotes(item.comments || []);
    await loadAudit(state.caseId);
    toast('协作备注已添加');
  } catch (error) {
    toast(error.message);
  }
}


function clearInsight() {
  $('#summaryEmpty').hidden = false;
  $('#summaryContent').hidden = true;
  $('#evidenceList').innerHTML = '<div class="side-empty">关键证据会按支持、反向和背景分别保留。</div>';
  $('#traceList').innerHTML = '<div class="side-empty">每一步核查都会留在这里。</div>';
  $('#teamNotes').innerHTML = '<div class="side-empty">还没有协作备注。</div>';
  $('#auditList').innerHTML = '<div class="side-empty">负责人变更、复核、监测和数据操作会留在这里。</div>';
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
    openModal('caseModal');
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
    setWorkspaceControls(item, (item.runs || [])[0]);
    beginPolling(out.run_id, state.caseId);
  } catch (error) {
    toast(error.message);
  }
}

function beginPolling(runId, caseId) {
  clearPolling();
  const tick = async () => {
    if (runId !== state.runId || caseId !== state.caseId) return;
    try {
      const run = await api(`/api/runs/${runId}`);
      if (runId !== state.runId || caseId !== state.caseId) return;
      renderRun(run);
      if (run.status !== 'running') {
        clearPolling();
        await openCase(caseId);
      }
    } catch (error) {
      clearPolling();
      toast(error.message);
    }
  };
  tick();
  state.polling = setInterval(tick, 700);
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

function openModal(id) {
  const modal = $(`#${id}`);
  if (!modal) return;
  modal.hidden = false;
  const focusable = modal.querySelector('input:not([type="hidden"]),textarea,select');
  if (focusable) setTimeout(() => focusable.focus(), 0);
}

function closeModal(id) {
  const modal = $(`#${id}`);
  if (modal) modal.hidden = true;
  if (id === 'caseModal') {
    state.pendingFiles = [];
    renderSelectedFiles();
  }
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
    state.pendingFiles = [];
    addPendingFiles(files);
    openModal('caseModal');
    return;
  }
  try {
    toast(`正在加入 ${files.length} 份素材`);
    await uploadFiles(state.caseId, files);
    const item = await api(`/api/cases/${state.caseId}`);
    state.case = item;
    renderAssets(item.assets || []);
    await loadAudit(state.caseId);
    toast('素材已加入当前调查');
  } catch (error) {
    toast(error.message);
  }
}

function formPosts(value) {
  return value.split(/\n+/).map((text) => text.trim()).filter(Boolean).map((text, index) => ({ id: `p${index + 1}`, text }));
}

function parseTags(value) {
  return value.split(/[,，]/).map((item) => item.trim()).filter(Boolean).slice(0, 12);
}

async function createCase(event) {
  event.preventDefault();
  const posts = formPosts($('#postsInput').value);
  const profileUrl = $('#profileUrlInput').value.trim();
  if (!posts.length && !state.pendingFiles.length && !$('#bioInput').value.trim() && !profileUrl) {
    toast('请至少添加主页简介、链接、近期内容或一份素材');
    return;
  }
  const target = {
    platform: $('#platformInput').value,
    handle: $('#handleInput').value.trim(),
    display_name: $('#handleInput').value.trim(),
    bio: $('#bioInput').value.trim(),
    profile_url: profileUrl || null,
    posts,
  };
  const goal = $('#goalInput').value.trim();
  const files = [...state.pendingFiles];
  try {
    const item = await api('/api/cases', {
      method: 'POST',
      body: JSON.stringify({
        title: `${target.display_name} · 内容调查`,
        goal,
        targets: [target],
        owner: $('#newOwnerSelect').value,
        priority: $('#newPrioritySelect').value,
        tags: parseTags($('#tagsInput').value),
      }),
    });
    if (files.length) await uploadFiles(item.id, files);
    closeModal('caseModal');
    $('#caseForm').reset();
    state.pendingFiles = [];
    state.caseId = item.id;
    await openCase(item.id);
    $('#messageInput').value = goal;
    await sendMessage();
  } catch (error) {
    toast(error.message);
  }
}

function splitCsvLine(line) {
  const result = [];
  let current = '';
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (char === '"') {
      if (quoted && line[index + 1] === '"') {
        current += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
    } else if (char === ',' && !quoted) {
      result.push(current.trim());
      current = '';
    } else {
      current += char;
    }
  }
  result.push(current.trim());
  return result;
}

function normalizeBatchTarget(raw, index) {
  const platformRaw = String(raw.platform || raw['平台'] || 'other').trim();
  const platform = platformCodes[platformRaw] || platformCodes[platformRaw.toLowerCase()] || 'other';
  const handle = String(raw.handle || raw.account || raw['账号'] || raw.display_name || '').trim();
  if (!handle) throw new Error(`第 ${index + 1} 个账号缺少账号名`);
  let posts = raw.posts || raw['内容'] || [];
  if (typeof posts === 'string') posts = posts.split('|').map((text) => text.trim()).filter(Boolean);
  if (!Array.isArray(posts)) posts = [];
  posts = posts.map((item, postIndex) => typeof item === 'string' ? { id: `p${postIndex + 1}`, text: item } : item);
  return {
    platform,
    handle,
    display_name: String(raw.display_name || raw.name || raw['名称'] || handle).trim(),
    bio: String(raw.bio || raw['简介'] || '').trim(),
    profile_url: String(raw.profile_url || raw.url || raw['链接'] || '').trim() || null,
    posts,
  };
}

function parseBatchText(text) {
  const value = text.trim();
  if (!value) return [];
  if (value.startsWith('[') || value.startsWith('{')) {
    const parsed = JSON.parse(value);
    const rows = Array.isArray(parsed) ? parsed : parsed.targets;
    if (!Array.isArray(rows)) throw new Error('JSON 需要是账号数组，或包含 targets 数组');
    return rows.map(normalizeBatchTarget);
  }
  const lines = value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (!lines.length) return [];
  const first = splitCsvLine(lines[0]).map((item) => item.toLowerCase());
  const hasHeader = first.includes('handle') || first.includes('账号');
  const header = hasHeader ? splitCsvLine(lines.shift()).map((item) => item.trim()) : ['platform', 'handle', 'bio', 'posts', 'profile_url'];
  return lines.map((line, index) => {
    const cells = splitCsvLine(line);
    const raw = {};
    header.forEach((key, cellIndex) => { raw[key] = cells[cellIndex] || ''; });
    return normalizeBatchTarget(raw, index);
  });
}

function previewBatch() {
  try {
    const targets = parseBatchText($('#batchInput').value);
    $('#batchParseState').textContent = targets.length ? `已识别 ${targets.length} 个账号` : '尚未解析';
    $('#batchParseState').classList.toggle('ready', Boolean(targets.length));
    return targets;
  } catch (error) {
    $('#batchParseState').textContent = error.message;
    $('#batchParseState').classList.remove('ready');
    return null;
  }
}

async function createBatch(event) {
  event.preventDefault();
  const targets = previewBatch();
  if (!targets || !targets.length) {
    toast('请先提供可识别的账号列表');
    return;
  }
  try {
    const data = await api('/api/cases/batch', {
      method: 'POST',
      body: JSON.stringify({
        title: `${targets.length} 个账号 · 批量调查`,
        goal: $('#batchGoalInput').value.trim(),
        targets,
        owner: $('#batchOwnerSelect').value,
        priority: $('#batchPrioritySelect').value,
        tags: parseTags($('#batchTagsInput').value),
        auto_start: true,
      }),
    });
    closeModal('batchModal');
    $('#batchForm').reset();
    state.listMode = 'all';
    updateListModeUI();
    await loadCases();
    toast(data.capacity_limited
      ? `已导入 ${data.batch.count} 个账号；当前执行队列已满，可稍后从调查列表继续发起`
      : `已导入 ${data.batch.count} 个账号并开始核查`);
  } catch (error) {
    toast(error.message);
  }
}

async function readBatchFile(file) {
  if (!file) return;
  const text = await file.text();
  $('#batchInput').value = text;
  previewBatch();
}

function openRefreshModal() {
  if (!state.case) return;
  const target = state.case.targets?.[0] || {};
  $('#refreshBioInput').value = target.bio || '';
  $('#refreshProfileUrlInput').value = target.profile_url || '';
  $('#refreshPostsInput').value = (target.posts || []).map((post) => post.text || '').filter(Boolean).join('\n');
  $('#refreshRerun').checked = true;
  openModal('refreshModal');
}

async function refreshSource(event) {
  event.preventDefault();
  if (!state.case) return;
  const old = state.case.targets?.[0] || {};
  const target = {
    ...old,
    bio: $('#refreshBioInput').value.trim(),
    profile_url: $('#refreshProfileUrlInput').value.trim() || null,
    posts: formPosts($('#refreshPostsInput').value),
  };
  try {
    const out = await api(`/api/cases/${state.caseId}/target`, {
      method: 'PATCH',
      body: JSON.stringify({ target, rerun: $('#refreshRerun').checked }),
    });
    closeModal('refreshModal');
    state.case = out.case;
    if (out.run_id) {
      state.runId = out.run_id;
      await openCase(state.caseId);
      beginPolling(out.run_id, state.caseId);
      toast('资料已更新，正在重新核查');
    } else {
      await openCase(state.caseId);
      toast('资料已更新');
    }
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
  const reviewedCase = state.caseId;
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
    state.listMode = 'queue';
    updateListModeUI();
    await loadCases();
    const next = $('#caseList .case-item');
    if (next && next.dataset.id !== reviewedCase) {
      await openCase(next.dataset.id);
    } else {
      await openCase(reviewedCase);
    }
  } catch (error) {
    toast(error.message);
  }
}

async function patchCurrentCase(payload, successMessage = '') {
  if (!state.caseId) return;
  try {
    const item = await api(`/api/cases/${state.caseId}`, {
      method: 'PATCH', body: JSON.stringify(payload),
    });
    state.case = item;
    setWorkspaceControls(item, (item.runs || [])[0]);
    renderCaseTags(item);
    await loadAudit(state.caseId);
    await loadCases();
    if (successMessage) toast(successMessage);
  } catch (error) {
    toast(error.message);
  }
}

async function toggleMonitoring() {
  if (!state.case) return;
  const enabled = !state.case.monitoring_enabled;
  await patchCurrentCase({
    monitoring_enabled: enabled,
    monitoring_interval_hours: Number($('#monitorInterval').value),
  }, enabled ? '已加入持续监测' : '已停止持续监测');
}

async function archiveCurrentCase() {
  if (!state.case) return;
  const archived = state.case.status !== 'archived';
  await patchCurrentCase({ archived }, archived ? '调查已归档' : '调查已恢复');
}

async function deleteCurrentCase() {
  if (!state.caseId) return;
  if (!canDelete()) {
    toast('只有管理员可以永久删除调查');
    return;
  }
  if (!window.confirm('删除这个调查及其素材、执行记录和复核结果？此操作不可撤销。')) return;
  try {
    await api(`/api/cases/${state.caseId}`, { method: 'DELETE' });
    clearCurrentCase();
    await loadCases();
    toast('调查已删除');
  } catch (error) {
    toast(error.message);
  }
}

function clearCurrentCase() {
  clearPolling();
  state.caseId = null;
  state.runId = null;
  state.run = null;
  state.case = null;
  $('#workspaceTools').hidden = true;
  $('#welcome').hidden = false;
  $('#messageList').innerHTML = '';
  $('#casePlatform').textContent = '跨平台调查';
  $('#caseTitle').textContent = '查清一个账号，不只看一条内容。';
  $('#caseMeta').textContent = '把账号、内容和素材放进来；系统负责核查顺序，人工负责最后确认。';
  $('#caseTags').hidden = true;
  renderAssets([]);
  clearInsight();
  history.replaceState(null, '', '/');
}

async function downloadReport() {
  if (!state.caseId) return;
  try {
    const response = await fetch(`/api/cases/${state.caseId}/report?output=markdown`);
    if (!response.ok) throw new Error('报告生成失败');
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `观潮-${state.caseId}.md`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast('调查报告已生成');
  } catch (error) {
    toast(error.message);
  }
}

async function copyCaseLink() {
  if (!state.caseId) return;
  const url = new URL(window.location.href);
  url.searchParams.set('case', state.caseId);
  try {
    await navigator.clipboard.writeText(url.toString());
    toast('调查链接已复制');
  } catch {
    toast('当前浏览器无法自动复制链接');
  }
}

function metricRow(label, value, note = '') {
  return `<div class="workspace-metric"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong>${note ? `<small>${escapeHtml(note)}</small>` : ''}</div>`;
}

async function openWorkspace() {
  try {
    const [metrics, settings] = await Promise.all([api('/api/metrics'), api('/api/workspace/settings')]);
    $('#workspaceMetrics').innerHTML = [
      metricRow('近 7 天已确认', String(metrics.verified_last_7_days || 0), '完成明确人工复核'),
      metricRow('直接接受率', metrics.acceptance_rate === null ? '—' : pct(metrics.acceptance_rate), '人工与明确初始判断一致'),
      metricRow('推翻率', metrics.overturn_rate === null ? '—' : pct(metrics.overturn_rate), '需要重点关注'),
      metricRow('证据充足率', metrics.evidence_sufficiency_rate === null ? '—' : pct(metrics.evidence_sufficiency_rate), '复核时无需补资料'),
      metricRow('复核中位耗时', formatDuration(metrics.median_time_to_review_seconds), '核查完成到人工确认'),
      metricRow('活跃复核效率', metrics.verified_per_active_review_hour === null ? '—' : `${metrics.verified_per_active_review_hour}/小时`, '至少 3 次有效复核后计算'),
    ].join('');
    $('#retentionDaysInput').value = settings.retention_days ?? 0;
    $('#currentActorName').textContent = currentMember()?.display_name || '当前成员';
    $('#currentActorRole').textContent = roleLabels[currentMember()?.role] || currentMember()?.role || '成员';
    renderMembers();
    const isAdmin = currentMember()?.role === 'admin';
    $('#memberForm').hidden = !isAdmin;
    $('#retentionDaysInput').disabled = !isAdmin;
    $('#saveRetentionBtn').hidden = !isAdmin;
    $('#purgeBtn').hidden = !isAdmin;
    openModal('workspaceModal');
  } catch (error) {
    toast(error.message);
  }
}

function renderMembers() {
  const isAdmin = currentMember()?.role === 'admin';
  $('#memberList').innerHTML = state.members.map((member) => `
    <div class="member-row"><div><strong>${escapeHtml(member.display_name)}</strong><small>${escapeHtml(member.id)}</small></div><span>${escapeHtml(roleLabels[member.role] || member.role)}</span>${member.id === 'local' ? '<em>默认</em>' : isAdmin ? `<button data-remove-member="${escapeHtml(member.id)}">移除</button>` : '<em>成员</em>'}</div>
  `).join('');
  $$('[data-remove-member]').forEach((button) => button.addEventListener('click', async () => {
    try {
      await api(`/api/members/${button.dataset.removeMember}`, { method: 'DELETE' });
      await loadMembers();
      renderMembers();
      toast('成员已移除');
    } catch (error) {
      toast(error.message);
    }
  }));
}

async function saveMember(event) {
  event.preventDefault();
  try {
    await api('/api/members', {
      method: 'POST',
      body: JSON.stringify({
        id: $('#memberIdInput').value.trim(),
        display_name: $('#memberNameInput').value.trim(),
        role: $('#memberRoleInput').value,
      }),
    });
    $('#memberForm').reset();
    await loadMembers();
    renderMembers();
    toast('成员已保存');
  } catch (error) {
    toast(error.message);
  }
}

async function saveRetention() {
  try {
    const data = await api('/api/workspace/settings', {
      method: 'PUT', body: JSON.stringify({ retention_days: Number($('#retentionDaysInput').value || 0) }),
    });
    $('#retentionDaysInput').value = data.retention_days;
    toast('数据留存策略已保存');
  } catch (error) {
    toast(error.message);
  }
}

async function purgeRetention() {
  if (!window.confirm('立即按当前留存策略清理到期的已归档调查？')) return;
  try {
    const data = await api('/api/workspace/purge', { method: 'POST' });
    toast(`已清理 ${data.deleted} 个到期归档调查`);
    await loadCases();
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

function updateListModeUI() {
  $$('[data-list-mode]').forEach((button) => button.classList.toggle('active', button.dataset.listMode === state.listMode));
  $('#sortFilter').disabled = state.listMode === 'watch';
}

function setListMode(mode) {
  state.listMode = mode;
  updateListModeUI();
  loadCases();
}

function debounce(fn, wait = 180) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function bindEvents() {
  $('#sendBtn').addEventListener('click', sendMessage);
  $('#messageInput').addEventListener('input', (event) => autosize(event.target));
  $('#messageInput').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });
  $('#newCaseBtn').addEventListener('click', () => openModal('caseModal'));
  $('#newCaseRailBtn').addEventListener('click', () => openModal('caseModal'));
  $('#batchBtn').addEventListener('click', () => openModal('batchModal'));
  $('#workspaceBtn').addEventListener('click', openWorkspace);
  $$('[data-demo="1"]').forEach((button) => button.addEventListener('click', loadDemo));
  $$('[data-open="new"]').forEach((button) => button.addEventListener('click', () => openModal('caseModal')));
  $$('[data-open="batch"]').forEach((button) => button.addEventListener('click', () => openModal('batchModal')));
  $$('[data-close]').forEach((button) => button.addEventListener('click', () => closeModal(button.dataset.close)));
  $$('.modal-backdrop').forEach((modal) => modal.addEventListener('click', (event) => { if (event.target === modal) closeModal(modal.id); }));
  $('#caseForm').addEventListener('submit', createCase);
  $('#batchForm').addEventListener('submit', createBatch);
  $('#refreshForm').addEventListener('submit', refreshSource);
  $('#memberForm').addEventListener('submit', saveMember);
  $('#batchInput').addEventListener('input', debounce(previewBatch));
  $('#batchFileBtn').addEventListener('click', () => $('#batchFileInput').click());
  $('#batchFileInput').addEventListener('change', (event) => { readBatchFile(event.target.files[0]); event.target.value = ''; });
  $$('[data-list-mode]').forEach((button) => button.addEventListener('click', () => setListMode(button.dataset.listMode)));
  ['#caseSearch', '#platformFilter', '#ownerFilter', '#priorityFilter', '#sortFilter'].forEach((selector) => $(selector).addEventListener(selector === '#caseSearch' ? 'input' : 'change', debounce(loadCases)));
  $('#markNormalBtn').addEventListener('click', () => submitReview('confirm_ordinary'));
  $('#markUncertainBtn').addEventListener('click', () => submitReview('uncertain'));
  $('#markMarketingBtn').addEventListener('click', () => submitReview('confirm_marketing'));
  $('#refreshSourceBtn').addEventListener('click', openRefreshModal);
  $('#reportBtn').addEventListener('click', downloadReport);
  $('#copyLinkBtn').addEventListener('click', copyCaseLink);
  $('#archiveCaseBtn').addEventListener('click', archiveCurrentCase);
  $('#deleteCaseBtn').addEventListener('click', deleteCurrentCase);
  $('#monitorBtn').addEventListener('click', toggleMonitoring);
  $('#monitorInterval').addEventListener('change', () => {
    if (state.case?.monitoring_enabled) patchCurrentCase({ monitoring_interval_hours: Number($('#monitorInterval').value) }, '监测周期已更新');
  });
  $('#caseOwnerSelect').addEventListener('change', () => patchCurrentCase({ owner: $('#caseOwnerSelect').value }, '负责人已更新'));
  $('#casePrioritySelect').addEventListener('change', () => patchCurrentCase({ priority: $('#casePrioritySelect').value }, '业务优先级已更新'));
  $$('.insight-tabs button').forEach((button) => button.addEventListener('click', () => switchTab(button.dataset.tab)));
  $('#attachBtn').addEventListener('click', () => $('#assetInput').click());
  $('#assetInput').addEventListener('change', (event) => { uploadToCurrent([...event.target.files]); event.target.value = ''; });
  $('#modalAttachBtn').addEventListener('click', () => $('#modalAssetInput').click());
  $('#modalAssetInput').addEventListener('change', (event) => { addPendingFiles([...event.target.files]); event.target.value = ''; });
  $('#saveRetentionBtn').addEventListener('click', saveRetention);
  $('#purgeBtn').addEventListener('click', purgeRetention);
  $('#addCaseNoteBtn').addEventListener('click', addCaseNote);
  $('#caseNoteInput').addEventListener('keydown', (event) => { if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') addCaseNote(); });

  document.addEventListener('keydown', (event) => {
    const target = event.target;
    if (target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return;
    if ($('#reviewActions').hidden) return;
    if (event.key === '1') submitReview('confirm_ordinary');
    if (event.key === '2') submitReview('uncertain');
    if (event.key === '3') submitReview('confirm_marketing');
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
}

async function bootstrap() {
  bindEvents();
  updateListModeUI();
  try {
    await loadSession();
    await loadMembers();
    await loadCases();
    const requested = new URL(window.location.href).searchParams.get('case');
    if (requested) await openCase(requested);
  } catch (error) {
    toast(error.message);
  }
}

bootstrap();
