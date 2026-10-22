const fs = require('node:fs');
const path = require('node:path');

const defaults = Object.freeze({ proxy: 'http://127.0.0.1:7897', whisperModel: '', fasterModelDir: '', autoTranslate: false, aiBaseUrl: '', aiApiKey: '', aiModel: '', visionBaseUrl: '', visionApiKey: '', visionModel: '', monitorCookie: '', monitorSources: '{"sources":[]}' });

function readSettings(root) {
  const file = path.join(root, 'settings.json');
  if (!fs.existsSync(file)) return { ...defaults };
  // A damaged configuration must not be silently replaced.
  const saved = JSON.parse(fs.readFileSync(file, 'utf8'));
  const settings = Object.fromEntries(Object.keys(defaults).map(key => [key, saved[key] === undefined ? defaults[key] : saved[key]]));
  for (const [key,rel] of [['monitorCookie','pipeline/cc_cookie.txt'],['monitorSources','pipeline/hot_monitor_sources.json']]) {
    if (!Object.hasOwn(saved,key) && fs.existsSync(path.join(root,rel))) settings[key]=fs.readFileSync(path.join(root,rel),'utf8');
  }
  if (!settings.aiBaseUrl || !settings.aiModel) settings.autoTranslate = false;
  return settings;
}

function saveSettings(root, input) {
  const saved = {};
  for (const key of Object.keys(defaults)) {
    if (key === 'autoTranslate') saved[key] = input[key] === true;
    else {
      if (typeof input[key] !== 'string') throw new Error('设置格式不正确');
      saved[key] = input[key].trim();
    }
  }
  if (saved.proxy && !/^https?:\/\/[^\s]+$/.test(saved.proxy)) throw new Error('代理地址应以 http:// 或 https:// 开头；不使用代理可留空');
  for (const key of ['fasterModelDir']) {
    if (saved[key] && (!path.isAbsolute(saved[key]) || !fs.statSync(saved[key]).isDirectory())) throw new Error('指定的目录不存在：' + saved[key]);
  }
  if (saved.whisperModel && (!path.isAbsolute(saved.whisperModel) || !fs.statSync(saved.whisperModel).isFile())) throw new Error('指定的模型文件不存在');
  for (const key of ['aiBaseUrl','visionBaseUrl']) if (saved[key]) {
    const url = new URL(saved[key]);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('模型 API 地址应为 http(s) 地址，不含账号密码、查询参数或片段');
  }
  const monitor = JSON.parse(saved.monitorSources);
  if (!monitor || !Array.isArray(monitor.sources)) throw new Error('监控配置必须包含 sources 数组');
  for (const source of monitor.sources) {
    if (!source || typeof source.name !== 'string' || !/^[\p{L}\p{N}_-][\p{L}\p{N}_.-]{0,63}$/u.test(source.name) || source.name.includes('..')) throw new Error('监控源名称不合法');
    if (!['cc_api','cc_ssr'].includes(source.type)) throw new Error('监控源 type 仅支持 cc_api/cc_ssr');
    if (source.params && (typeof source.params !== 'object' || Array.isArray(source.params))) throw new Error('监控源 params 必须是对象');
    if (source.type === 'cc_ssr') { const url=new URL(source.url); if(!['http:','https:'].includes(url.protocol)) throw new Error('监控 URL 必须是 http(s) 地址'); }
    else if(typeof source.path !== 'string' || !source.path.startsWith('/') || source.path.startsWith('//')) throw new Error('API 监控源需要站内 path');
  }
  if (saved.autoTranslate && (!saved.aiBaseUrl || !saved.aiModel)) throw new Error('启用自动翻译前，请填写模型 API 地址和模型名称');
  const temp = path.join(root, 'settings.json.tmp');
  fs.writeFileSync(temp, JSON.stringify(saved, null, 2), 'utf8');
  fs.renameSync(temp, path.join(root, 'settings.json'));
  return saved;
}

function prepareProfile(resourceRoot, root, userHome, token, port) {
  const payload = path.join(resourceRoot, 'payload');
  fs.mkdirSync(root, { recursive: true });
  const settings = readSettings(root);
  if (!fs.existsSync(path.join(root, 'settings.json'))) saveSettings(root, settings);
  // Only packaged code is refreshed. No data files are in this manifest.
  const manifest = JSON.parse(fs.readFileSync(path.join(payload, 'manifest.json'), 'utf8'));
  for (const rel of manifest.files) {
    if (path.isAbsolute(rel) || rel.split(/[\\/]/).includes('..')) throw new Error('无效的资源清单');
    const dest = path.join(root, rel);
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    const source = fs.readFileSync(path.join(payload, rel));
    if (!fs.existsSync(dest) || !source.equals(fs.readFileSync(dest))) fs.writeFileSync(dest, source);
  }
  for (const rel of ['webapp/data', 'pipeline/output', 'pipeline/reports', 'ledger', 'logs', 'reverse', 'cache/huggingface', 'cache/whisper']) fs.mkdirSync(path.join(root, rel), { recursive: true });
  for(const [rel,value] of [['pipeline/cc_cookie.txt',settings.monitorCookie],['pipeline/hot_monitor_sources.json',settings.monitorSources]]) {
    const target=path.join(root,rel); if(!fs.existsSync(target)||fs.readFileSync(target,'utf8')!==value)fs.writeFileSync(target,value,'utf8');
  }
  const accountFile = path.join(root, 'pipeline', 'accounts.json');
  if (!fs.existsSync(accountFile)) fs.writeFileSync(accountFile, JSON.stringify({
    xlsx: path.join(root, 'ledger', 'TikTok_视频台账.xlsx'),
    img_xlsx: path.join(root, 'ledger', 'TikTok_图文台账.xlsx'),
    metrics_file: path.join(root, 'pipeline', 'output', 'daily_metrics.jsonl'),
    reports_root: path.join(root, 'pipeline', 'reports'), recall_days: 2, gmv_enabled: false, accounts: []
  }, null, 2));
  const packagedRuntime = path.join(resourceRoot,'runtime');
  const runtimeRoot = fs.existsSync(path.join(packagedRuntime,'python','python.exe')) ? packagedRuntime : (process.env.WB_BUILD_RUNTIME_ROOT || packagedRuntime);
  const python = path.join(runtimeRoot, 'python', 'python.exe');
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (/^(WB_|ECHOTIK_|PYTHON|HF_|HUGGINGFACE_|XDG_CACHE_HOME$|WHISPER_|DAILY_BATCH_REPORTS_DIR$)/i.test(key)) delete env[key];
  }
  Object.assign(env, {
    WB_PROFILE_ROOT: root, WB_PIPELINE_DIR: path.join(root, 'pipeline'), WB_PYTHON: python,
    WB_REVERSE_DIR: path.join(root,'reverse'), WB_DESKTOP_PARENT_PID: String(process.pid),
    WB_LEDGER_DIR: path.join(root, 'ledger'), WB_PROXY: settings.proxy,
    WB_AUTO_TRANSLATE: settings.autoTranslate ? '1' : '0', WB_DESKTOP_TOKEN: token,
    WB_DESKTOP_PORT: String(port), WB_WORKBENCH_URL: `http://127.0.0.1:${port}`,
    WB_WHISPER_CACHE: path.join(root, 'cache', 'whisper'),
    WB_WHISPER_MODEL: settings.whisperModel || path.join(runtimeRoot, 'models', 'medium.pt'),
    WB_FASTER_MODEL_DIR: settings.fasterModelDir,
    HF_HOME: path.join(root, 'cache', 'huggingface'), HF_HUB_OFFLINE: settings.fasterModelDir ? '1' : '0',
    XDG_CACHE_HOME: path.join(root, 'cache'), PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1',
    PATH: [path.join(runtimeRoot, 'ffmpeg'), path.dirname(python), process.env.PATH || ''].join(path.delimiter)
  });
  return { root, settings, env, python, server: path.join(root, 'webapp', 'desktop_server.py') };
}

module.exports = { defaults, readSettings, saveSettings, prepareProfile };
