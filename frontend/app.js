const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

const state = { caseId: null, runId: null, run: null, case: null, polling: null };

const platformNames = { xiaohongshu: '小红书', weibo: '微博', douyin: '抖音', bilibili: 'B站', other: '其他平台' };

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || '请求失败');
  return data;
}

function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove('show'), 2100);
}

function pct(value) { return `${Math.round((Number(value) || 0) * 100)}%`; }

async function loadCases() {
  try {
    const cases = await api('/api/cases');
    const list = $('#caseList');
    if (!cases.length) {
      list.innerHTML = '<div class="case-empty">还没有任务。可以直接载入演示。</div>';
      return;
    }
    list.innerHTML = cases.map(item => {
      const platform = platformNames[item.targets?.[0]?.platform] || '调查';
      return `<button class="case-item ${item.id === state.caseId ? 'active' : ''}" data-id="${item.id}"><span><strong>${escapeHtml(item.title)}</strong><small>${platform} · ${formatTime(item.updated_at)}</small></span></button>`;
    }).join('');
    $$('.case-item').forEach(btn => btn.addEventListener('click', () => openCase(btn.dataset.id)));
  } catch (err) { toast(err.message); }
}

async function openCase(id) {
  try {
    const item = await api(`/api/cases/${id}`);
    state.caseId = id;
    state.case = item;
    $('#welcome').hidden = true;
    $('#caseTitle').textContent = item.title;
    const target = item.targets?.[0] || {};
    $('#casePlatform').textContent = `${platformNames[target.platform] || '跨平台'} · ${target.display_name || target.handle || '账号调查'}`;
    $('#caseMeta').textContent = item.goal;
    $('#workspaceActions').hidden = false;
    renderMessages(item.messages || []);
    const latest = (item.runs || [])[0];
    if (latest) {
      state.runId = latest.id;
      renderRun(latest);
      if (latest.status === 'running') beginPolling(latest.id);
    } else clearInsight();
    loadCases();
  } catch (err) { toast(err.message); }
}

function renderMessages(messages) {
  $('#messageList').innerHTML = messages.map(m => `
    <article class="message ${m.role}">
      <span class="role">${m.role === 'user' ? 'YOU' : '观潮'}</span>
      <div class="bubble">${escapeHtml(m.content).replace(/\n/g, '<br>')}</div>
    </article>`).join('');
  $('#conversation').scrollTop = $('#conversation').scrollHeight;
}

function renderRun(run) {
  state.run = run;
  const s = run.state || {};
  const events = s.events || [];
  const completed = (s.completed_tools || []).length;
  const targetSteps = 5 + ((s.targets || []).length > 1 ? 1 : 0);
  const progress = Math.min(1, completed / targetSteps);
  $('#runStrip').hidden = run.status !== 'running';
  if (run.status === 'running') {
    const last = events[events.length - 1] || {};
    $('#runTitle').textContent = '正在核查';
    $('#runDetail').textContent = last.detail || last.title || '继续推进';
    $('#runProgress').style.width = `${Math.max(6, progress * 100)}%`;
  }
  renderTrace(events);
  renderEvidence(s.evidence || []);
  if (s.primary_result?.label || run.status === 'completed') renderSummary(s.primary_result || {});
}

function renderSummary(result) {
  if (!result || !Object.keys(result).length) return;
  $('#summaryEmpty').hidden = true;
  $('#summaryContent').hidden = false;
  $('#verdictLabel').textContent = result.label || '待补资料';
  $('#confidenceValue').textContent = pct(result.confidence);
  $('#confidenceMeter').style.width = pct(result.confidence);
  $('#marketingValue').textContent = pct(result.marketing_likelihood);
  $('#covertValue').textContent = pct(result.covert_promotion_risk);
  $('#summaryCopy').textContent = result.summary || '正在整理当前判断。';
  const missing = result.missing || [];
  $('#missingBlock').hidden = !missing.length;
  $('#missingList').innerHTML = missing.map(x => `<li>${escapeHtml(x)}</li>`).join('');
}

function renderEvidence(evidence) {
  const seen = new Map();
  evidence.forEach(item => { if (!seen.has(item.key)) seen.set(item.key, item); });
  const items = [...seen.values()].sort((a,b) => (b.strength || 0) - (a.strength || 0));
  $('#evidenceList').innerHTML = items.length ? items.map(item => `
    <article class="evidence-item ${item.direction || 'context'}">
      <div class="evidence-top"><strong>${escapeHtml(item.title)}</strong><em>${item.direction === 'against' ? '反向线索' : item.direction === 'supports' ? '支持判断' : '背景'}</em></div>
      <p>${escapeHtml(item.detail)}</p>
    </article>`).join('') : '<div class="side-empty">还没有可展示的关键证据。</div>';
}

function renderTrace(events) {
  $('#traceList').innerHTML = events.length ? events.map(event => `
    <article class="trace-item ${event.status || 'done'}">
      <div><strong>${escapeHtml(event.title)}</strong><p>${escapeHtml(event.detail || '')}</p></div>
    </article>`).join('') : '<div class="side-empty">执行过程会按时间留下记录。</div>';
}

function clearInsight() {
  $('#summaryEmpty').hidden = false;
  $('#summaryContent').hidden = true;
  $('#evidenceList').innerHTML = '<div class="side-empty">完成一次调查后显示关键证据。</div>';
  $('#traceList').innerHTML = '<div class="side-empty">执行过程会按时间留下记录。</div>';
}

async function sendMessage() {
  const input = $('#messageInput');
  const content = input.value.trim();
  if (!content) return;
  if (!state.caseId) { openModal(); return; }
  input.value = '';
  autosize(input);
  try {
    const out = await api(`/api/cases/${state.caseId}/messages`, { method: 'POST', body: JSON.stringify({ content }) });
    state.runId = out.run_id;
    const item = await api(`/api/cases/${state.caseId}`);
    state.case = item;
    renderMessages(item.messages || []);
    beginPolling(out.run_id);
  } catch (err) { toast(err.message); }
}

function beginPolling(runId) {
  clearInterval(state.polling);
  const tick = async () => {
    try {
      const run = await api(`/api/runs/${runId}`);
      renderRun(run);
      if (run.status !== 'running') {
        clearInterval(state.polling);
        state.polling = null;
        const item = await api(`/api/cases/${state.caseId}`);
        state.case = item;
        renderMessages(item.messages || []);
        loadCases();
      }
    } catch (err) {
      clearInterval(state.polling); state.polling = null; toast(err.message);
    }
  };
  tick();
  state.polling = setInterval(tick, 420);
}

async function loadDemo() {
  try {
    const out = await api('/api/demo', { method: 'POST' });
    state.caseId = out.case.id;
    state.runId = out.run_id;
    await openCase(out.case.id);
    beginPolling(out.run_id);
    toast('已载入完整演示');
  } catch (err) { toast(err.message); }
}

function openModal() { $('#caseModal').hidden = false; $('#handleInput').focus(); }
function closeModal() { $('#caseModal').hidden = true; }

async function createCase(ev) {
  ev.preventDefault();
  const posts = $('#postsInput').value.split(/\n+/).map(x => x.trim()).filter(Boolean).map((text, i) => ({ id: `p${i+1}`, text }));
  const target = {
    platform: $('#platformInput').value,
    handle: $('#handleInput').value.trim(),
    display_name: $('#handleInput').value.trim(),
    bio: $('#bioInput').value.trim(),
    posts,
  };
  const goal = $('#goalInput').value.trim();
  const title = `${target.display_name} · 内容调查`;
  try {
    const item = await api('/api/cases', { method: 'POST', body: JSON.stringify({ title, goal, targets: [target] }) });
    closeModal();
    state.caseId = item.id;
    await openCase(item.id);
    $('#messageInput').value = goal;
    sendMessage();
    $('#caseForm').reset();
  } catch (err) { toast(err.message); }
}

async function submitFeedback(label) {
  if (!state.caseId) return;
  try {
    await api('/api/feedback', { method: 'POST', body: JSON.stringify({ case_id: state.caseId, label, note: '' }) });
    toast(label ? '已记录：营销运营' : '已记录：普通创作者');
  } catch (err) { toast(err.message); }
}

function switchTab(tab) {
  $$('.evidence-tabs button').forEach(b => b.classList.toggle('active', b.dataset.tab === tab));
  $$('.tab-panel').forEach(p => p.classList.remove('active'));
  $(`#tab-${tab}`).classList.add('active');
}

function autosize(el) { el.style.height = 'auto'; el.style.height = `${Math.min(150, el.scrollHeight)}px`; }
function formatTime(iso) { try { const d = new Date(iso); return `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`; } catch { return ''; } }
function escapeHtml(value='') { return String(value).replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch])); }

$('#sendBtn').addEventListener('click', sendMessage);
$('#messageInput').addEventListener('input', e => autosize(e.target));
$('#messageInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } });
$('#demoBtn').addEventListener('click', loadDemo);
$$('[data-demo="1"]').forEach(b => b.addEventListener('click', loadDemo));
$('#newCaseBtn').addEventListener('click', openModal);
$('#newCaseRailBtn').addEventListener('click', openModal);
$$('[data-open="new"]').forEach(b => b.addEventListener('click', openModal));
$('#closeModal').addEventListener('click', closeModal);
$('#cancelModal').addEventListener('click', closeModal);
$('#caseModal').addEventListener('click', e => { if (e.target.id === 'caseModal') closeModal(); });
$('#caseForm').addEventListener('submit', createCase);
$('#refreshCases').addEventListener('click', loadCases);
$('#markNormalBtn').addEventListener('click', () => submitFeedback(0));
$('#markMarketingBtn').addEventListener('click', () => submitFeedback(1));
$$('.evidence-tabs button').forEach(b => b.addEventListener('click', () => switchTab(b.dataset.tab)));

loadCases();
