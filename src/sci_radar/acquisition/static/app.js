const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function toast(message, error = false) {
  const el = $('#toast');
  el.textContent = message;
  el.setAttribute('role', error ? 'alert' : 'status');
  el.setAttribute('aria-live', error ? 'assertive' : 'polite');
  el.classList.toggle('error', error);
  el.classList.add('show');
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => el.classList.remove('show'), 4200);
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let message = `请求失败 (${response.status})`;
    try { const data = await response.json(); message = data.detail || message; } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function activateTab(tab, focus = false) {
  $$('.tab').forEach((item) => { item.classList.remove('active'); item.setAttribute('aria-selected', 'false'); });
  $$('.tab').forEach((item) => { item.tabIndex = -1; });
  $$('.capture-panel').forEach((panel) => { panel.classList.remove('active'); panel.hidden = true; });
  tab.classList.add('active'); tab.setAttribute('aria-selected', 'true'); tab.tabIndex = 0;
  const panel = $(`#panel-${tab.dataset.tab}`); panel.hidden = false; panel.classList.add('active');
  if (focus) tab.focus();
}

$$('.tab').forEach((tab) => {
  tab.addEventListener('click', () => activateTab(tab));
  tab.addEventListener('keydown', (event) => {
    const tabs = $$('.tab');
    const current = tabs.indexOf(tab);
    const targets = {ArrowRight: (current + 1) % tabs.length, ArrowLeft: (current - 1 + tabs.length) % tabs.length, Home: 0, End: tabs.length - 1};
    if (!(event.key in targets)) return;
    event.preventDefault();
    activateTab(tabs[targets[event.key]], true);
  });
});

$('#file').addEventListener('change', (event) => {
  $('#file-name').textContent = event.target.files[0]?.name || '尚未选择文件';
});
$('#text').addEventListener('input', (event) => { $('#char-count').textContent = `${event.target.value.length.toLocaleString()} 字符`; });

async function submitForm(form, action) {
  const button = form.querySelector('button[type=submit]');
  button.disabled = true;
  try {
    const task = await action();
    toast(`任务已入账：${task.resource_type} · 正在保存原始内容`);
    form.reset();
    $('#file-name').textContent = '尚未选择文件';
    $('#char-count').textContent = '0 字符';
    await loadDashboard();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
}

$('#url-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submitForm(form, () => request('/api/v1/acquisitions', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url: form.url.value, capture_screenshot: form.capture_screenshot.checked}),
  }));
});

$('#text-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submitForm(form, () => request('/api/v1/acquisitions/text', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: form.text.value, display_name: form.display_name.value || '粘贴文本'}),
  }));
});

$('#file-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submitForm(form, () => request('/api/v1/acquisitions/file', {method: 'POST', body: new FormData(form)}));
});

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
}

const typeLabels = {web_page:'网页', wechat_article:'微信文章', pdf:'PDF', word:'Word', text:'文本', video:'视频', podcast:'播客', binary_file:'文件'};
const statusLabels = {success:'成功', partial:'部分成功', failed:'失败', blocked:'已阻断', running:'采集中', pending:'等待中'};
const safeStatuses = new Set(['success', 'partial', 'failed', 'blocked', 'quarantined', 'running', 'pending', 'uploading', 'submitted', 'polling', 'downloading', 'assembling']);
let dashboardLoading = false;
let systemOnline = null;

function statusClass(value) {
  return safeStatuses.has(value) ? value : 'unknown';
}

function setSystemState(online) {
  const state = $('#system-state');
  state.querySelector('span').textContent = online ? '采集节点在线' : '采集节点连接异常';
  state.classList.toggle('degraded', !online);
  const changed = systemOnline !== online;
  systemOnline = online;
  return changed;
}

async function loadDashboard() {
  if (dashboardLoading) return;
  dashboardLoading = true;
  try {
    const [summary, tasks, assets, evidence] = await Promise.all([
      request('/api/v1/dashboard/summary'),
      request('/api/v1/acquisitions?limit=30'),
      request('/api/v1/raw-assets?limit=30'),
      request('/api/v1/evidence?limit=30'),
    ]);
    $('#asset-count').textContent = String(summary.assets).padStart(2, '0');
    $('#task-count').textContent = summary.tasks;
    $('#success-count').textContent = summary.assets;
    $('#stored-size').textContent = formatBytes(summary.stored_bytes);
    $('#failure-count').textContent = (summary.by_status.failed || 0) + (summary.by_status.blocked || 0);
    setSystemState(true);
    const rows = $('#task-rows');
    if (!tasks.length) {
      rows.innerHTML = '<tr class="empty"><td colspan="7">还没有采集记录，从上方投递第一份原始内容。</td></tr>';
    } else {
      const assetsByTask = new Map(assets.map((asset) => [asset.task_id, asset]));
      rows.innerHTML = tasks.map((task) => {
        const asset = assetsByTask.get(task.task_id);
        const size = asset?.primary_blob?.size_bytes || task.staged_blob?.size_bytes;
        const name = task.display_name || task.requested_url || '未命名内容';
        const locator = task.requested_url || task.staged_blob?.sha256 || task.source_key;
        const rawLink = asset?.primary_blob?.blob_id ? `<a class="download-link" href="/api/v1/blobs/${encodeURIComponent(asset.primary_blob.blob_id)}">原始文件</a>` : '';
        const parseLink = asset && ['pdf', 'word'].includes(asset.resource_type) ? `<button class="ingest-link" type="button" data-ingest="${escapeHtml(asset.asset_id)}">解析入库</button>` : '';
        const link = rawLink || parseLink ? `<span class="action-links">${rawLink}${parseLink}</span>` : '—';
        const taskStatus = String(task.status || 'unknown');
        const resourceType = String(task.resource_type || 'unknown');
        const version = asset ? `V${Number(asset.version_no) || 0}` : '—';
        const errNote = (['failed', 'blocked'].includes(taskStatus) && task.last_error_message)
          ? `<small class="error-text" style="color: #ff5252; display: block; margin-top: 2px;">⚠ ${escapeHtml(task.last_error_message)}</small>`
          : '';
        return `<tr><td class="content-cell"><strong title="${escapeHtml(name)}">${escapeHtml(name)}</strong><small title="${escapeHtml(locator)}">${escapeHtml(locator)}</small>${errNote}</td><td>${escapeHtml(typeLabels[resourceType] || resourceType)}</td><td><span class="tag status-${statusClass(taskStatus)}">${escapeHtml(statusLabels[taskStatus] || taskStatus)}</span></td><td>${escapeHtml(version)}</td><td>${size ? formatBytes(size) : '远程'}</td><td>${new Date(task.created_at).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</td><td>${link}</td></tr>`;
      }).join('');
    }
    const evidenceRows = $('#evidence-rows');
    if (!evidence.length) {
      evidenceRows.innerHTML = '<tr class="empty"><td colspan="6">还没有 Evidence。文本和网页会自动入库，PDF / Word 请在采集记录中确认解析。</td></tr>';
    } else {
      evidenceRows.innerHTML = evidence.map((item) => `<tr><td class="content-cell"><strong>${escapeHtml(item.evidence_id)}</strong><small>${escapeHtml(item.view_uri || '')}</small></td><td><span class="tag status-${statusClass(String(item.status || 'unknown'))}">${escapeHtml(statusLabels[item.status] || item.status)}</span></td><td>${escapeHtml(item.asset_id)}</td><td>${escapeHtml(String(item.pipeline_fingerprint || '').slice(0, 12))}</td><td>${new Date(item.created_at).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</td><td><span class="action-links"><a class="download-link" href="/api/v1/evidence/${encodeURIComponent(item.evidence_id)}/content" target="_blank" rel="noopener">Markdown</a><a class="download-link" href="/api/v1/evidence/${encodeURIComponent(item.evidence_id)}/package">下载包</a></span></td></tr>`).join('');
    }
  } catch (error) {
    if (setSystemState(false)) toast(`读取账本失败：${error.message}`, true);
  }
  finally { dashboardLoading = false; }
}

function escapeHtml(value) {
  return String(value || '').replace(/[&<>'"]/g, (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]));
}

$('#refresh').addEventListener('click', loadDashboard);
document.addEventListener('click', async (event) => {
  if (!(event.target instanceof Element)) return;
  const button = event.target.closest('[data-ingest]');
  if (!button) return;
  if (!window.confirm('该文档将发送给 MinerU 外部解析服务。确认继续吗？')) return;
  button.disabled = true;
  try {
    await request('/api/v1/ingestions', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({asset_id: button.dataset.ingest, external_processing_allowed: true}),
    });
    toast('MinerU 解析任务已进入单 Worker 队列');
    await loadDashboard();
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});
loadDashboard();
setInterval(loadDashboard, 12000);
