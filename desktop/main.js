const { app, BrowserWindow, Menu, dialog, shell, ipcMain } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const http = require('node:http');
const crypto = require('node:crypto');
const { spawn, spawnSync } = require('node:child_process');
const { prepareProfile, readSettings, saveSettings } = require('./profile');

const dataRoot = process.env.WB_DESKTOP_TEST_PROFILE || path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'TikTokWorkbench');
fs.mkdirSync(dataRoot, { recursive: true });
app.setPath('userData', path.join(dataRoot, 'browser'));
app.setPath('crashDumps', path.join(dataRoot, 'logs', 'crashes'));
const resources = app.isPackaged ? process.resourcesPath : path.resolve(__dirname, '..');
let mainWindow, settingsWindow, backend, baseUrl, token, stopping = false, quitApproved = false, logFd, backendOwnsTree = false;
const firstRun = !fs.existsSync(path.join(dataRoot, 'settings.json'));

function log(message) {
  fs.mkdirSync(path.join(dataRoot, 'logs'), { recursive: true });
  fs.appendFileSync(path.join(dataRoot, 'logs', 'desktop.log'), `${new Date().toISOString()} ${message}\n`);
}
function request(route) {
  return new Promise((resolve, reject) => {
    const req = http.get(baseUrl + route, { headers: { 'X-Workbench-Token': token }, timeout: 1500 }, res => {
      let body = ''; res.on('data', chunk => body += chunk); res.on('end', () => {
        try { if (res.statusCode !== 200) throw new Error('HTTP ' + res.statusCode); resolve(JSON.parse(body)); } catch (e) { reject(e); }
      });
    });
    req.on('timeout', () => req.destroy(new Error('后台响应超时'))); req.on('error', reject);
  });
}
function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer(); server.once('error', reject);
    server.listen(0, '127.0.0.1', () => { const port = server.address().port; server.close(() => resolve(port)); });
  });
}
function external(url) { if (/^https?:\/\//i.test(url)) shell.openExternal(url); }
function secureNavigation(win) {
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (baseUrl && new URL(url).origin === baseUrl) win.loadURL(url);
    else external(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (baseUrl && new URL(url).origin === baseUrl) return;
    event.preventDefault(); external(url);
  });
  win.webContents.session.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
}
function showSettings() {
  if (settingsWindow) { settingsWindow.focus(); return; }
  settingsWindow = new BrowserWindow({ width: 740, height: 780, minWidth: 650, minHeight: 600, parent: mainWindow, title: '工作台设置', autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.js'), nodeIntegration: false, contextIsolation: true, sandbox: true } });
  settingsWindow.setMenu(null);
  settingsWindow.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));
  settingsWindow.webContents.on('will-navigate', e => e.preventDefault());
  settingsWindow.loadFile(path.join(__dirname, 'settings.html'));
  settingsWindow.on('closed', () => settingsWindow = null);
}
function settingsSender(event) {
  if (!settingsWindow || event.sender !== settingsWindow.webContents || event.senderFrame !== settingsWindow.webContents.mainFrame) throw new Error('无权访问设置');
}
ipcMain.handle('settings:read', event => { settingsSender(event); return { settings: readSettings(dataRoot), dataRoot }; });
ipcMain.handle('settings:save', async (event, input) => {
  settingsSender(event);
  const active = await request('/desktop/running');
  if (Array.isArray(active) && active.length) throw new Error('请先停止运行中的任务，再保存设置并重启');
  saveSettings(dataRoot, input);
  quitApproved = true;
  app.relaunch(); app.quit();
  return true;
});
ipcMain.handle('settings:folder', event => { settingsSender(event); shell.openPath(dataRoot); });

async function confirmClose() {
  let running;
  try { running = await request('/desktop/running'); } catch { running = null; }
  if (running === null || (Array.isArray(running) && running.length)) {
    const result = await dialog.showMessageBox(mainWindow, { type: 'question', buttons: ['继续工作', '结束任务并退出'], defaultId: 0, cancelId: 0,
      message: running === null ? '无法确认后台任务状态，仍要退出吗？' : `有 ${running.length} 个任务正在运行，退出将结束这些任务。` });
    if (result.response !== 1) return false;
  }
  return true;
}
function stopBackend() {
  stopping = true;
  if (backend && backend.exitCode === null && !backendOwnsTree) spawnSync(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'taskkill.exe'), ['/PID', String(backend.pid), '/T', '/F'], { windowsHide: true, timeout: 15000 });
  if (backend && backend.exitCode === null) backend.kill('SIGKILL');
  if (logFd !== undefined) { fs.closeSync(logFd); logFd = undefined; }
}
app.on('before-quit', event => {
  if (backend && !quitApproved) {
    event.preventDefault();
    confirmClose().then(ok => { if (ok) { quitApproved = true; app.quit(); } });
    return;
  }
  stopBackend();
});
app.on('window-all-closed', () => app.quit());
app.on('second-instance', () => { if (mainWindow) { if (mainWindow.isMinimized()) mainWindow.restore(); mainWindow.focus(); } });

async function start() {
  const port = await freePort(); token = crypto.randomBytes(32).toString('hex'); baseUrl = `http://127.0.0.1:${port}`;
  const profile = prepareProfile(resources, dataRoot, os.homedir(), token, port);
  log('启动，后台端口 ' + port);
  logFd = fs.openSync(path.join(dataRoot, 'logs', 'backend.log'), 'a');
  backend = spawn(profile.python, ['-E', '-X', 'utf8', '-u', profile.server], {
    cwd: path.join(dataRoot, 'webapp'), env: profile.env, windowsHide: true, stdio: ['ignore', logFd, logFd]
  });
  let failed;
  backend.on('error', error => { failed = error; });
  backend.on('exit', (code) => {
    if (!stopping) {
      failed = new Error('后台意外退出，代码 ' + code);
      if (mainWindow) { log(failed.message); dialog.showErrorBox('工作台后台已停止', '请重新启动软件。排查日志：' + path.join(dataRoot, 'logs', 'backend.log')); quitApproved = true; app.quit(); }
    }
  });
  const deadline = Date.now() + 45000;
  while (true) {
    if (failed) throw failed;
    try { const health = await request('/desktop/health'); if (health.ok && health.instance === token) { backendOwnsTree=health.job_owned===true; break; } } catch {}
    if (Date.now() >= deadline) throw new Error('后台启动超时，请检查 backend.log');
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  mainWindow = new BrowserWindow({ width: 1440, height: 940, minWidth: 1000, minHeight: 650, title: 'TikTok 工作台', show: false,
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } });
  mainWindow.webContents.session.webRequest.onBeforeSendHeaders({ urls: [baseUrl + '/*'] }, (details, callback) => {
    details.requestHeaders['X-Workbench-Token'] = token;
    callback({ requestHeaders: details.requestHeaders });
  });
  secureNavigation(mainWindow);
  mainWindow.on('close', event => {
    if (quitApproved) return;
    event.preventDefault();
    confirmClose().then(ok => { if (ok) { quitApproved = true; app.quit(); } });
  });
  const menu = [
    { label: '工作台', submenu: [ { label: '设置…', click: showSettings }, { label: '打开本机数据目录', click: () => shell.openPath(dataRoot) },
      { label: '查看日志', click: () => shell.openPath(path.join(dataRoot, 'logs')) }, { type: 'separator' }, { label: '退出', click: () => app.quit() } ] },
    { label: '编辑', submenu: [{ role: 'undo', label: '撤销' }, { role: 'redo', label: '重做' }, { type: 'separator' }, { role: 'cut', label: '剪切' }, { role: 'copy', label: '复制' }, { role: 'paste', label: '粘贴' }, { role: 'selectAll', label: '全选' }] },
    { label: '查看', submenu: [{ role: 'reload', label: '刷新' }, { role: 'resetZoom', label: '实际大小' }, { role: 'zoomIn', label: '放大' }, { role: 'zoomOut', label: '缩小' }, { role: 'togglefullscreen', label: '全屏' }] },
    { label: '帮助', submenu: [{ label: '使用说明', click: () => shell.openPath(path.join(resources, '使用说明.txt')) }, { label: '关于', click: () => dialog.showMessageBox(mainWindow, { message: 'TikTok 工作台 2.0.0', detail: '2026-10-22 最终版 · 本地任务与视觉反推\n程序随 U 盘携带，数据保存在本机当前用户目录。' }) }] }
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(menu));
  await mainWindow.loadURL(baseUrl);
  mainWindow.show();
  log('窗口就绪');
  if (firstRun) showSettings();
}

if (!app.requestSingleInstanceLock()) app.quit();
else app.whenReady().then(start).catch(error => {
  log('启动失败：' + error.message);
  dialog.showErrorBox('工作台启动失败', error.message + '\n日志目录：' + path.join(dataRoot, 'logs'));
  quitApproved = true; app.quit();
});
