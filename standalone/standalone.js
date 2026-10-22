let activeRun = null;
const $ = id => document.getElementById(id);
async function api(route, body) {
  const response = await fetch(route, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '请求失败');
  return data;
}
function message(text, error = false) { $('task-status').textContent = text; $('task-status').className = error ? 'error' : ''; }
async function refresh() {
  const [status, reports, history] = await Promise.all([api('/api/standalone/status'), api('/api/standalone/reports'), api('/api/runs?limit=15')]);
  if (!$('accounts').children.length) {
    for (const account of status.accounts) {
      const label = document.createElement('label'), checkbox = document.createElement('input');
      checkbox.type = 'checkbox'; checkbox.value = account.account;
      label.append(checkbox, document.createTextNode(' @' + account.account)); $('accounts').append(label);
    }
    if (!status.accounts.length) $('accounts').textContent = '暂无账号，请返回账号管理添加。';
  }
  $('model-status').textContent = status.ai_configured ? '模型：' + status.ai_model + '（已配置）' : '模型尚未配置；本地任务可以直接使用。';
  $('reports').replaceChildren();
  for (const file of reports.items) {
    const row = document.createElement('div'), link = document.createElement('a'); row.className = 'report';
    const name = document.createElement('span'); name.textContent = (file.ai ? 'AI 分析 · ' : '统计报告 · ') + file.name;
    link.href = '/api/standalone/reports/' + encodeURIComponent(file.name); link.target = '_blank'; link.rel = 'noopener'; link.textContent = '查看报告'; row.append(name, link); $('reports').append(row);
  }
  if (!reports.items.length) $('reports').textContent = '尚未生成报告。';
  $('history').replaceChildren();
  for (const run of history.runs.filter(r => r.kind === 'standalone').slice(0, 8)) {
    const row = document.createElement('div'), button = document.createElement('button'); row.className = 'history';
    const text = document.createElement('span'); text.textContent = run.title + ' · ' + ({running:'执行中',done:'完成',failed:'失败',stopped:'已停止',interrupted:'已中断'}[run.status] || run.status);
    button.textContent = '查看日志'; button.onclick = () => { activeRun = run.id; poll(); }; row.append(text, button); $('history').append(row);
    if (!activeRun && run.status === 'running') activeRun = run.id;
  }
}
async function poll() {
  if (!activeRun) return;
  try {
    const run = await api('/api/runs/' + encodeURIComponent(activeRun));
    message(run.title + ' · ' + ({running:'执行中',done:'完成',failed:'失败',stopped:'已停止',interrupted:'已中断'}[run.status] || run.status), run.status === 'failed');
    $('logs').textContent = (run.tail || []).map(line => typeof line === 'string' ? line : line.text || '').join('\n') || '正在启动…';
    $('stop').disabled = run.status !== 'running';
  } catch (error) { message(error.message, true); }
}
document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', async () => {
  button.disabled = true;
  try {
    const accounts = Array.from($('accounts').querySelectorAll('input:checked')).map(input => input.value);
    const body = { action: button.dataset.action, ai: button.dataset.action === 'full' && $('include-ai').checked };
    if (accounts.length) body.accounts = accounts;
    const run = await api('/api/standalone/run', body); activeRun = run.id;
    message(run.title + ' · 已启动'); await poll(); await refresh();
  } catch (error) { message(error.message, true); }
  finally { button.disabled = false; }
}));
$('stop').onclick = async () => { try { if (activeRun) await api('/api/runs/' + encodeURIComponent(activeRun) + '/stop', {}); await poll(); } catch (error) { message(error.message, true); } };
$('refresh').onclick = () => refresh().catch(e => message(e.message, true));
refresh().then(poll).catch(e => message(e.message, true));
setInterval(() => { poll(); refresh().catch(() => {}); }, 2500);
