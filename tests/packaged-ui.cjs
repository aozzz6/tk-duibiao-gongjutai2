const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const { spawn, spawnSync } = require('node:child_process');
const assert = require('node:assert/strict');
const root = path.join(__dirname, 'artifacts', 'packaged-ui-' + Date.now());
const profile = path.join(root, 'profile');
fs.mkdirSync(root, { recursive: true });
const pause = ms => new Promise(r => setTimeout(r, ms));
const exe = process.env.TEST_EXE || path.resolve(__dirname, '..', 'dist', 'win-unpacked', 'TikTok工作台.exe');
let debugPort, main, backendUrl;
async function freePort() { return new Promise(resolve => { const s = net.createServer(); s.listen(0, '127.0.0.1', () => { const port = s.address().port; s.close(() => resolve(port)); }); }); }
async function pages() { return fetch(`http://127.0.0.1:${debugPort}/json/list`, { signal: AbortSignal.timeout(1000) }).then(r => r.json()); }
async function waitFor(check) {
  let last;
  for (let n = 0; n < 160; n++) { try { const v = await check(); if (v) return v; } catch (e) { last = e; } await pause(250); }
  throw last || new Error('UI timeout');
}
async function connect(page) {
  const ws = new WebSocket(page.webSocketDebuggerUrl); const pending = new Map(); let id = 0;
  await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once: true }); ws.addEventListener('error', reject, { once: true }); });
  ws.addEventListener('message', event => { const message = JSON.parse(event.data); const task = pending.get(message.id); if (!task) return; pending.delete(message.id); message.error ? task.reject(new Error(JSON.stringify(message.error))) : task.resolve(message.result); });
  ws.addEventListener('close', () => { for (const task of pending.values()) task.reject(new Error('Target closed')); pending.clear(); });
  const send = (method, params = {}) => new Promise((resolve, reject) => { const requestId = ++id; pending.set(requestId, { resolve, reject }); ws.send(JSON.stringify({ id: requestId, method, params })); });
  return {
    send,
    evaluate: async expression => { const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }); if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails)); return r.result.value; },
    screenshot: async file => fs.writeFileSync(path.join(root, file), Buffer.from((await send('Page.captureScreenshot')).data, 'base64')),
    close: () => ws.close()
  };
}
(async () => {
  debugPort = await freePort();
  const fd = fs.openSync(path.join(root, 'process.log'), 'a');
  spawn(exe, [`--remote-debugging-port=${debugPort}`, '--remote-debugging-address=127.0.0.1'], { env: { ...process.env, WB_DESKTOP_TEST_PROFILE: profile }, windowsHide: true, stdio: ['ignore', fd, fd] });
  const initial = await waitFor(async () => { const all = await pages(); const m = all.find(p => p.type === 'page' && /^http:\/\/127\.0\.0\.1:/.test(p.url)); const s = all.find(p => p.url.endsWith('/settings.html')); return m && s ? { m, s } : null; });
  backendUrl = new URL(initial.m.url).origin;
  main = await connect(initial.m); const settings = await connect(initial.s);
  await waitFor(() => settings.evaluate('document.getElementById("proxy")?.value === "http://127.0.0.1:7897"'));
  const config = await main.evaluate('fetch("/api/config").then(r => r.json())'); assert.deepEqual(config.accounts, []);
  await settings.screenshot('settings-window.png');
  await main.screenshot('main-window.png');
  assert(await settings.evaluate('(()=>{ const r=document.getElementById("save").getBoundingClientRect();return r.top>=0 && r.bottom<=innerHeight; })()'), 'Save button must be visible without scrolling');
  await settings.evaluate('document.getElementById("proxy").value = ""; document.getElementById("save").click(); true');
  settings.close(); main.close();
  const restarted = await waitFor(async () => { const all = await pages(); return all.find(p => p.type === 'page' && /^http:\/\/127\.0\.0\.1:/.test(p.url) && p.id !== initial.m.id); });
  main = await connect(restarted); backendUrl = new URL(restarted.url).origin;
  assert.equal(JSON.parse(fs.readFileSync(path.join(profile, 'settings.json'))).proxy, '');
  assert.deepEqual((await main.evaluate('fetch("/api/config").then(r => r.json())')).accounts, []);
  assert(!(await pages()).some(p => p.url.endsWith('/settings.html')), 'First-start settings should not reopen after save');
  const nav=await main.evaluate('Array.from(document.querySelectorAll("#nav a")).filter(a=>getComputedStyle(a).display!=="none").map(a=>a.textContent.trim())');
  assert(!nav.some(label=>/Agent|EchoTik|转交/.test(label)),JSON.stringify(nav));
  for(const view of ['bench','accounts','run','reports','schedule','hot','reverse','overview']) {
    await main.evaluate('document.querySelector('+JSON.stringify('[data-view="'+view+'"]').replaceAll('<','')+').click(); true');
    await waitFor(()=>main.evaluate('document.querySelector(".view.active")?.id === '+JSON.stringify('view-'+view)));
  }
  await main.evaluate('document.querySelector("[data-view=reverse]").click(); true');
  await waitFor(()=>main.evaluate('document.getElementById("rv-local-file") !== null'));
  const fixture=path.join(root,'ui-local-fixture.mp4');
  const ffmpeg=path.join(path.dirname(exe),'resources','runtime','ffmpeg','ffmpeg.exe');
  const encode=spawnSync(ffmpeg,['-hide_banner','-loglevel','error','-y','-f','lavfi','-i','testsrc=size=96x160:rate=4','-t','2','-c:v','libx264','-pix_fmt','yuv420p',fixture],{windowsHide:true,encoding:'utf8'});assert.equal(encode.status,0,encode.stderr);
  const doc=await main.send('DOM.getDocument');
  const input=await main.send('DOM.querySelector',{nodeId:doc.root.nodeId,selector:'#rv-local-file'});
  await main.send('DOM.setFileInputFiles',{nodeId:input.nodeId,files:[fixture]});
  await waitFor(()=>main.evaluate('fetch("/api/reverse/list").then(r=>r.json()).then(d=>d.items.some(it=>it.stage==="ready"&&it.frames>0))'));
  await main.screenshot('reverse-local-import.png');
  await main.send('Emulation.setDeviceMetricsOverride',{width:1280,height:720,deviceScaleFactor:1,mobile:false});
  await main.evaluate('document.querySelector("[data-view=bench]").click(); true');
  await pause(300);
  const width=await main.evaluate('(()=>{const t=document.getElementById("bench-accounts"),w=t.parentElement;return {client:w.clientWidth,scroll:w.scrollWidth};})()');
  assert(width.client>0&&width.scroll<=width.client+1,JSON.stringify(width));
  await main.screenshot('bench-1280.png');
  fs.writeFileSync(path.join(root,'navigation-and-width.json'),JSON.stringify({nav,width},null,2));
  await main.send('Emulation.clearDeviceMetricsOverride');
  await main.send('Page.navigate', { url: backendUrl + '/standalone' });
  await waitFor(() => main.evaluate('document.getElementById("model-status")?.textContent.includes("模型尚未配置")'));
  assert.equal(await main.evaluate('document.querySelectorAll("[data-action]").length'), 8);
  await main.evaluate('document.querySelector("[data-action=report]").click(); true');
  await waitFor(() => main.evaluate('document.getElementById("task-status")?.textContent.includes("完成")'));
  await waitFor(() => main.evaluate('document.querySelectorAll("#reports a").length > 0'));
  await main.screenshot('standalone-tasks.png');
  await main.evaluate('document.querySelector("[data-action=model_test]").click(); true');
  await waitFor(() => main.evaluate('document.getElementById("task-status")?.textContent.includes("设置")'));
  const apiSettings = JSON.parse(fs.readFileSync(path.join(profile, 'settings.json')));
  assert(!Object.hasOwn(apiSettings, 'workbuddyHome'));
  await main.evaluate('window.close(); true').catch(() => {}); main.close(); main = null;
  await waitFor(async () => { try { await fetch(backendUrl + '/api/health', { signal: AbortSignal.timeout(500) }); return false; } catch { return true; } });
  fs.closeSync(fd);
  const report = { ok: true, executable: exe, checks: ['packaged EXE starts', 'blank initial account list', 'settings save button visible', 'settings save and automatic restart', 'settings persist on same computer', 'first-start dialog shown once', 'all retained navigation views open', 'no Agent or EchoTik entry', 'native file input imports and processes a real local video', '1280 desktop benchmark table fits its container', 'independent task page loads', 'offline report generated through UI', 'missing model config shown clearly', 'settings have no WorkBuddy dependency', 'close stops backend'], profile };
  fs.writeFileSync(path.join(root, 'report.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ ok: true, root, checks: report.checks.length }));
})().catch(async error => {
  fs.writeFileSync(path.join(root, 'failure.txt'), error.stack);
  if (main) { await main.evaluate('window.close(); true').catch(() => {}); main.close(); }
  console.error(error); process.exitCode = 1;
});
