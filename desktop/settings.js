const fields = ['proxy', 'whisperModel', 'fasterModelDir', 'autoTranslate', 'aiBaseUrl', 'aiApiKey', 'aiModel', 'visionBaseUrl', 'visionApiKey', 'visionModel', 'monitorCookie', 'monitorSources'];
window.workbenchSettings.read().then(({ settings, dataRoot }) => {
  for (const key of fields) { const el = document.getElementById(key); if (el.type === 'checkbox') el.checked = settings[key]; else el.value = settings[key]; }
  document.getElementById('root').textContent = '本机数据目录：' + dataRoot;
}).catch(e => document.getElementById('status').textContent = e.message);
document.getElementById('folder').addEventListener('click', () => window.workbenchSettings.openFolder());
document.getElementById('save').addEventListener('click', async () => {
  const button = document.getElementById('save'); button.disabled = true;
  try {
    const data = Object.fromEntries(fields.map(key => { const el = document.getElementById(key); return [key, el.type === 'checkbox' ? el.checked : el.value]; }));
    await window.workbenchSettings.save(data);
  } catch (e) { document.getElementById('status').textContent = e.message; button.disabled = false; }
});
