const { app, BrowserWindow } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.join(__dirname, 'artifacts', 'electron-' + Date.now());
fs.mkdirSync(root, { recursive: true });
process.env.WB_DESKTOP_TEST_PROFILE = path.join(root, 'profile');
const errors = [];
app.on('web-contents-created', (_event, contents) => {
  contents.on('console-message', (_event, level, message) => { if (level === 3) errors.push(message); });
  contents.on('render-process-gone', (_event, details) => errors.push(JSON.stringify(details)));
});
require('../desktop/main');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
(async () => {
  await app.whenReady();
  let main, settings;
  for (let n = 0; n < 160; n++) {
    const windows = BrowserWindow.getAllWindows();
    main = windows.find(w => /^http:\/\/127\.0\.0\.1:/.test(w.webContents.getURL()));
    settings = windows.find(w => w.webContents.getURL().endsWith('/settings.html'));
    if (main && settings && !main.webContents.isLoading() && !settings.webContents.isLoading()) break;
    await pause(250);
  }
  assert(main && settings, 'Expected main window and first-start settings');
  await pause(1500);
  const cfg = await main.webContents.executeJavaScript('fetch("/api/config").then(r=>r.json())');
  assert.deepEqual(cfg.accounts, []);
  const text = await main.webContents.executeJavaScript('document.body.innerText');
  assert(text.includes('账号') && text.includes('台账'), text.slice(0, 300));
  assert.equal(await main.webContents.executeJavaScript('typeof require'), 'undefined');
  const settingsText = await settings.webContents.executeJavaScript('document.body.innerText');
  assert(settingsText.includes('这台电脑的工作台'));
  assert.equal(await settings.webContents.executeJavaScript('typeof window.workbenchSettings.save'), 'function');
  fs.writeFileSync(path.join(root, 'main-window.png'), (await main.webContents.capturePage()).toPNG());
  fs.writeFileSync(path.join(root, 'settings-window.png'), (await settings.webContents.capturePage()).toPNG());
  fs.writeFileSync(path.join(root, 'report.json'), JSON.stringify({ ok: errors.length === 0, accounts: cfg.accounts, errors, checks: ['main window loads', 'first-start settings', 'private backend request', 'empty accounts', 'renderer Node disabled', 'settings preload available'] }, null, 2));
  console.log(JSON.stringify({ root, errors }));
  assert.deepEqual(errors, []);
  app.quit();
})().catch(error => { fs.writeFileSync(path.join(root, 'failure.txt'), error.stack); console.error(error); app.exit(1); });
