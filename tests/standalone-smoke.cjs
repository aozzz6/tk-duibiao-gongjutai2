const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const net = require('node:net');
const { spawn, spawnSync } = require('node:child_process');
const { prepareProfile, saveSettings, defaults } = require('../desktop/profile');
const resources = process.env.TEST_RESOURCES || path.resolve(__dirname, '..');
const root = path.join(__dirname, 'artifacts', 'standalone-' + Date.now());
fs.mkdirSync(root, { recursive: true });
let child, profile, logFd, modelServer, modelMode = 'normal';
const checks = [], requests = [];
const pause = ms => new Promise(r => setTimeout(r, ms));
const check = name => checks.push({ name, ok: true });
async function freePort() { return new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); }); }

(async () => {
  modelServer = http.createServer((req, res) => {
    let raw = ''; req.on('data', b => raw += b); req.on('end', () => {
      const body = JSON.parse(raw); requests.push({ path: req.url, auth: req.headers.authorization, body });
      if (modelMode === 'http-error') { res.writeHead(401, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ error: 'Do not expose local-test-key' })); return; }
      let answer;
      try { const input = JSON.parse(body.messages[1].content); if (Array.isArray(input)) { answer = {}; for (const item of input) (answer[item.account] ||= {})[String(item.row)] = '这是独立模型返回的测试译文'; answer = JSON.stringify(answer); } else answer = '依据 portable_test_account 视频 1234567890123456789 的测试分析。<script>bad()</script>'; }
      catch { answer = 'OK'; }
      if (modelMode === 'invalid-translation') answer = JSON.stringify({ nonexistent_account: { '999': '错误行不应写入' } });
      res.writeHead(200, { 'Content-Type': 'application/json' }); res.end(JSON.stringify({ choices: [{ message: { content: answer } }] }));
    });
  });
  await new Promise(resolve => modelServer.listen(0, '127.0.0.1', resolve));
  const port = await freePort(), token = 'standalone-test-token';
  const profileRoot = path.join(root, 'profile'); fs.mkdirSync(profileRoot);
  saveSettings(profileRoot, { ...defaults, proxy: 'http://127.0.0.1:9', aiBaseUrl: `http://127.0.0.1:${modelServer.address().port}/v1`, aiApiKey: 'local-test-key', aiModel: 'mock-local-model' });
  // Deliberately absent WorkBuddy home: no dependency or credentials can be reused.
  profile = prepareProfile(resources, profileRoot, path.join(root, 'no-workbuddy-user'), token, port);
  assert.equal(profile.env.WB_HOME, undefined);
  logFd = fs.openSync(path.join(root, 'backend.log'), 'a');
  child = spawn(profile.python, ['-E', '-X', 'utf8', '-u', profile.server], { env: profile.env, cwd: path.dirname(profile.server), windowsHide: true, stdio: ['ignore', logFd, logFd] });
  async function raw(route, body) {
    return fetch(`http://127.0.0.1:${port}${route}`, { headers: { 'X-Workbench-Token': token, 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) }) });
  }
  async function api(route, body) { const response = await raw(route, body); const data = await response.json(); assert.equal(response.status, 200, route + ': ' + JSON.stringify(data)); return data; }
  for (let i = 0; i < 100; i++) { try { if ((await api('/desktop/health')).ok) break; } catch {} await pause(150); }
  const status = await api('/api/standalone/status'); assert(status.standalone && status.ai_configured); assert(!JSON.stringify(status).includes('local-test-key')); check('independent settings, no WorkBuddy home, no key exposure');
  assert.equal((await raw('/standalone')).status, 200);
  assert.equal((await raw('/api/standalone/run', { action: ['report'] })).status, 400);
  assert.equal((await raw('/api/standalone/run', { action: 'full', accounts: [] })).status, 400);
  await api('/api/accounts', { account: 'portable_test_account', region: 'de' });
  await api('/api/accounts', { account: 'portable_image', region: 'de' });
  await api('/api/accounts/media-type', { accounts: ['portable_image'], media_type: 'image' });
  async function wait(run, expected = 'done') {
    for (let i = 0; i < 180; i++) { const snapshot = await api('/api/runs/' + run.id); if (snapshot.status !== 'running') { assert.equal(snapshot.status, expected, JSON.stringify(snapshot)); return snapshot; } await pause(100); }
    throw new Error('Task timed out');
  }
  async function task(action, accounts, ai) { return wait(await api('/api/standalone/run', { action, ...(accounts ? { accounts } : {}), ...(ai === undefined ? {} : { ai }) })); }
  await task('build'); check('video and image sheet creation with no collected data');
  function python(code, args = []) { const result = spawnSync(profile.python, ['-E', '-X', 'utf8', '-c', code, ...args], { env: profile.env, encoding: 'utf8', windowsHide: true }); assert.equal(result.status, 0, result.stderr); return result.stdout; }
  python("import json,openpyxl,os;from pathlib import Path;p=Path(os.environ['WB_PIPELINE_DIR']);c=json.loads((p/'accounts.json').read_text(encoding='utf-8'));v=openpyxl.load_workbook(c['xlsx']);i=openpyxl.load_workbook(c['img_xlsx']);assert v['对标记录-portable_test_account'].max_column==14;assert i['图文账号记录'].max_column==12");
  const output = path.join(profileRoot, 'pipeline', 'output');
  for (const account of ['portable_test_account', 'portable_image']) {
    fs.writeFileSync(path.join(output, 'scrape_' + account + '.json'), JSON.stringify({ account, items: [{ id: '1234567890123456789', desc: 'Portable test #topic', publish_time_gmt8: '2026-10-20 12:00:00', url: 'https://www.tiktok.com/@' + account + '/video/1234567890123456789', stats: { playCount: 10, diggCount: 2, commentCount: 0, shareCount: 1, collectCount: 0 } }] }));
  }
  const transcript = path.join(output, 'portable_test_account_audio', 'transcripts'); fs.mkdirSync(transcript, { recursive: true });
  fs.writeFileSync(path.join(transcript, 'portable_test_account_1234567890123456789_transcript.txt'), '[00:00:00,000 -> 00:00:01,000] This is an independent test transcript with enough words.');
  await task('backfill'); check('video and shared image ledger backfill, with disk verifier');
  await task('model_test'); check('model connection test uses own API settings, bypassing collection proxy');
  await task('translate', ['portable_test_account']);
  python("import json,openpyxl,os;from pathlib import Path;c=json.loads((Path(os.environ['WB_PIPELINE_DIR'])/'accounts.json').read_text(encoding='utf-8'));w=openpyxl.load_workbook(c['xlsx']);s=w['对标记录-portable_test_account'];assert s.cell(2,14).value=='这是独立模型返回的测试译文';assert 'independent test transcript' in s.cell(2,13).value");
  check('model translation writes exact selected row without WorkBuddy');
  modelMode = 'http-error';
  const failedConnection = await wait(await api('/api/standalone/run', { action: 'model_test' }), 'failed');
  assert(JSON.stringify(failedConnection).includes('HTTP 401')); assert(!JSON.stringify(failedConnection).includes('local-test-key')); check('provider authentication failure is surfaced without leaking response body or key');
  modelMode = 'invalid-translation';
  python("import json,openpyxl,os;from pathlib import Path;c=json.loads((Path(os.environ['WB_PIPELINE_DIR'])/'accounts.json').read_text(encoding='utf-8'));w=openpyxl.load_workbook(c['xlsx']);w['对标记录-portable_test_account'].cell(2,14).value=None;w.save(c['xlsx'])");
  await wait(await api('/api/standalone/run', { action: 'translate', accounts: ['portable_test_account'] }), 'failed');
  python("import json,openpyxl,os;from pathlib import Path;c=json.loads((Path(os.environ['WB_PIPELINE_DIR'])/'accounts.json').read_text(encoding='utf-8'));w=openpyxl.load_workbook(c['xlsx']);assert w['对标记录-portable_test_account'].cell(2,14).value is None");
  check('invalid model row identities fail without writing unrelated cells');
  modelMode = 'normal'; await task('translate', ['portable_test_account']);
  await task('analyze', ['portable_test_account']);
  let reports = await api('/api/standalone/reports'); const analysis = reports.items.find(r => r.ai);
  assert(analysis); const html = await (await raw('/api/standalone/reports/' + analysis.name)).text();
  assert(html.includes('&lt;script&gt;bad()&lt;/script&gt;')); assert(!html.includes('<script>bad()'));
  const data = await api('/api/standalone/reports/' + analysis.name.replace('.html', '.json')); assert.equal(data.records, 1); assert.equal(data.accounts[0].views, 10);
  check('AI analysis report retains evidence and escapes generated HTML');
  const before = requests.length; await task('full', undefined, false); assert.equal(requests.length, before); check('complete offline organization does not call model');
  await api('/api/schedule', { enabled: false, time: '09:30', accounts: ['portable_test_account'], task: 'report' });
  await wait(await api('/api/schedule/trigger', {})); check('local scheduler executes chosen report task');
  python("import sys,os,datetime;from pathlib import Path;sys.path.insert(0,str(Path(os.environ['WB_PROFILE_ROOT'])/'webapp'));from app import app;from standalone_backend import install;install(app);import scheduler;calls=[];scheduler.trigger=lambda *a,**k:calls.append(a);scheduler.save({'enabled':True,'time':'00:00','accounts':['portable_test_account'],'task':'report','last_run':None});scheduler._tick();scheduler._tick();assert len(calls)==1;assert scheduler.load()['last_run']==datetime.date.today().isoformat()");
  check('daily scheduler fires once and persists its date receipt');
  const cfg = await api('/api/config'); assert.deepEqual(cfg.agent_steps, []); assert(cfg.local_steps.includes('s3_4_sheet') && cfg.local_steps.includes('s7_backfill') && cfg.local_steps.includes('s9_report'));
  // Exercise the former four placeholder pipeline stages end to end.
  const pipelineArgs = ['-E', '-X', 'utf8', path.join(profileRoot, 'pipeline', 'pipeline.py'), '--account', 'portable_test_account', '--from', '2026-10-20', '--to', '2026-10-20', '--xlsx', JSON.parse(fs.readFileSync(path.join(profileRoot, 'pipeline', 'accounts.json'))).xlsx, '--steps', 's3_4_sheet', 's6_translate', 's7_backfill', 's9_report', '--no-resume', '--no-parallel'];
  // Asynchronous spawn leaves the local mock model server responsive.
  const cli = spawn(profile.python, pipelineArgs, { env: profile.env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] }); let cliLog = '';
  cli.stdout.on('data', b => cliLog += b); cli.stderr.on('data', b => cliLog += b);
  const code = await new Promise(resolve => cli.on('exit', resolve)); fs.writeFileSync(path.join(root, 'pipeline-adapters.log'), cliLog); assert.equal(code, 0, cliLog);
  check('all four former Agent stages execute locally in pipeline');
  assert(requests.every(r => r.auth === 'Bearer local-test-key' && r.path === '/v1/chat/completions'));
  assert(!fs.readFileSync(path.join(root, 'backend.log'), 'utf8').includes('local-test-key')); check('model secret absent from logs and report API');
  fs.writeFileSync(path.join(root, 'report.json'), JSON.stringify({ ok: true, resources, checks, modelRequests: requests.length }, null, 2));
  console.log(JSON.stringify({ ok: true, checks: checks.length, root }));
})().catch(error => { fs.writeFileSync(path.join(root, 'failure.txt'), error.stack); console.error(error); process.exitCode = 1; }).finally(() => {
  if (child && child.exitCode === null) spawnSync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true });
  if (child && child.exitCode === null) child.kill('SIGKILL');
  if (logFd !== undefined) fs.closeSync(logFd);
  if (modelServer) modelServer.close();
});
