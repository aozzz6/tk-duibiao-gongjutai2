const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const { spawn, spawnSync } = require('node:child_process');
const { prepareProfile, saveSettings, readSettings } = require('../desktop/profile');
const project = path.resolve(__dirname, '..');
const resources = process.env.TEST_RESOURCES || project;
const testRoot = path.join(__dirname, 'artifacts', 'backend-' + Date.now());
fs.mkdirSync(testRoot, { recursive: true });
const results = [];
const report = () => fs.writeFileSync(path.join(testRoot, 'report.json'), JSON.stringify({ resources, results }, null, 2));
async function port() { return new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)); }); }); }
async function launch(name) {
  const p = await port(); const token = 'test-instance-' + p;
  const profile = prepareProfile(resources, path.join(testRoot, name), require('node:os').homedir(), token, p);
  const log = fs.openSync(path.join(testRoot, name + '-backend.log'), 'a');
  const child = spawn(profile.python, ['-E', '-X', 'utf8', '-u', profile.server], { env: profile.env, cwd: path.dirname(profile.server), windowsHide: true, stdio: ['ignore', log, log] });
  async function call(route, body, authorized = true) {
    const r = await fetch(`http://127.0.0.1:${p}${route}`, { headers: { ...(authorized ? { 'X-Workbench-Token': token } : {}), 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { method: 'POST', body: JSON.stringify(body) }), signal: AbortSignal.timeout(20000) });
    const text = await r.text();
    if (!authorized) return r.status;
    assert.equal(r.status, 200, route + ': ' + text.slice(0, 600));
    return r.headers.get('content-type')?.includes('json') ? JSON.parse(text) : text;
  }
  try {
    let ready = false;
    for (let n = 0; n < 100; n++) {
      if (child.exitCode !== null) throw new Error('Backend exited: ' + fs.readFileSync(path.join(testRoot, name + '-backend.log'), 'utf8'));
      try { const r = await call('/desktop/health'); if (r.instance === token) { ready = true; break; } } catch {}
      await new Promise(r => setTimeout(r, 200));
    }
    assert(ready, 'Backend startup timed out');
  } catch (e) { child.kill(); fs.closeSync(log); throw e; }
  return { profile, call, stop: () => { spawnSync('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true }); if(child.exitCode===null)child.kill('SIGKILL'); fs.closeSync(log); } };
}

(async () => {
  let server;
  try {
    server = await launch('computer-A');
    assert.equal(await server.call('/api/health', undefined, false), 403);
    const cfg = await server.call('/api/config'); assert.deepEqual(cfg.accounts, []);
    for (const route of ['/', '/static/app.js', '/api/overview', '/api/accounts', '/api/groups', '/api/ledger', '/api/benchmark', '/api/runs', '/api/reports', '/api/schedule', '/api/pipeline/status', '/api/reverse/list', '/api/reverse/models', '/api/hot/latest']) {
      await server.call(route); results.push({ check: 'blank profile ' + route, ok: true });
    }
    assert.deepEqual(await server.call('/desktop/running'), []);
    const unicode = spawnSync(server.profile.python, ['-c', 'print("中文日志 ⚠")'], { env: server.profile.env, encoding: 'utf8', windowsHide: true });
    assert.equal(unicode.stdout.trim(), '中文日志 ⚠');
    results.push({ check: 'nested CLI logs use UTF-8 on Chinese Windows', ok: true });
    await server.call('/api/accounts', { account: 'portable_test_account', region: 'de', nickname: '隔离测试' });
    assert((await server.call('/api/config')).accounts.includes('portable_test_account'));
    const settings = readSettings(server.profile.root);
    saveSettings(server.profile.root, { ...settings, proxy: '' });
    const output = path.join(server.profile.root, 'pipeline', 'output');
    const video = '1234567890123456789';
    fs.writeFileSync(path.join(output, 'scrape_portable_test_account.json'), JSON.stringify({ account: 'portable_test_account', items: [{ id: video, desc: 'Portable test #test', publish_time_gmt8: '2026-10-20 12:00:00', url: 'https://www.tiktok.com/@portable_test_account/video/' + video, stats: { playCount: 10, diggCount: 2, commentCount: 0, shareCount: 0, collectCount: 0 } }] }));
    const transcript = path.join(output, 'portable_test_account_audio', 'transcripts');
    fs.mkdirSync(transcript, { recursive: true });
    fs.writeFileSync(path.join(transcript, 'portable_test_account_' + video + '_transcript.txt'), '[00:00:00,000 -> 00:00:01,000] This is a local test transcript for the portable workbench.');
    const ledgerFlow = spawnSync(server.profile.python, ['-E', '-X', 'utf8', path.join(server.profile.root, 'pipeline', 'scripts', 'post_update_flow.py'), '--accounts', 'portable_test_account'], { env: server.profile.env, encoding: 'utf8', windowsHide: true, timeout: 30000 });
    fs.writeFileSync(path.join(testRoot, 'ledger-flow.log'), ledgerFlow.stdout + ledgerFlow.stderr);
    assert.equal(ledgerFlow.status, 0, ledgerFlow.stdout + ledgerFlow.stderr);
    results.push({ check: 'new blank ledger: write, transcript export, disk verification', ok: true });
    // Exercise an actual child interpreter and sibling-module imports without network access.
    const childResult = spawnSync(server.profile.python, ['-E', '-X', 'utf8', path.join(server.profile.root, 'pipeline', 'scripts', 'post_update_flow.py'), '--help'], { env: server.profile.env, encoding: 'utf8', windowsHide: true });
    assert.equal(childResult.status, 0, childResult.stderr);
    const verify = spawnSync(server.profile.python, ['-E', '-X', 'utf8', path.join(server.profile.root, 'pipeline', 'scripts', 'post_update.py'), 'verify'], { env: server.profile.env, encoding: 'utf8', windowsHide: true });
    // The test account has no collected videos. API reachability must still be authenticated.
    assert(!verify.stdout.includes('工作台不可达'), verify.stdout + verify.stderr);
    results.push({ check: 'bundled Python subprocess / private API', ok: true, verifyExit: verify.status });
    server.stop(); server = await launch('computer-A');
    assert((await server.call('/api/config')).accounts.includes('portable_test_account'));
    assert.equal(readSettings(server.profile.root).proxy, '');
    results.push({ check: 'computer A restart preserves its account and settings', ok: true });
    server.stop(); server = await launch('computer-B');
    assert.deepEqual((await server.call('/api/config')).accounts, []);
    assert.deepEqual((await server.call('/api/runs')).runs, []);
    assert.equal(readSettings(server.profile.root).proxy, 'http://127.0.0.1:7897');
    results.push({ check: 'computer B starts empty and isolated', ok: true });
    for (const file of JSON.parse(fs.readFileSync(path.join(resources, 'payload', 'manifest.json'))).files) {
      assert(!/(^|\/)(data|output|reports|logs|__pycache__)(\/|$)|cookie|accounts\.json|\.bak/i.test(file), file);
    }
    results.push({ check: 'payload manifest excludes business data, cookies, backups', ok: true });
    report(); console.log(JSON.stringify({ ok: true, checks: results.length, report: path.join(testRoot, 'report.json') }));
  } catch (error) { results.push({ ok: false, error: error.stack }); report(); console.error(error); process.exitCode = 1; }
  finally { if (server) server.stop(); }
})();
