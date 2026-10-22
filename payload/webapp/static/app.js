/* TikTok 对标工作台 · 前端逻辑 */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

/* tablekit 兜底：旧缓存 / 单元测试环境未加载 tablekit.js 时降级为无增强实现。
   业务逻辑照常跑，只是没有拖拽列宽 / 分页条 / 图表等表现层能力。 */
const TKV = window.TK || {
  num: (v) => (Number.isFinite(Number(v)) ? Number(v) : 0),
  esc: (s) => String(s == null ? "" : s),
  resizable() {}, sortable() {}, setSort() {}, expandable() {}, reveal() {}, compare() {},
  bars() {}, donut() {}, line() {}, bands() {},   // 图表降级：tablekit 未加载时静默跳过
  // 降级分页条：tablekit 缺失（未部署 / 404 / 旧缓存）时也必须能翻到全部数据，
  // 否则只剩首屏 size 行可见、旧「加载更多」又被隐藏，剩余数据就彻底无法访问。
  // 用内联样式，不依赖 tablekit.css。
  pager: (host, o) => {
    if (!host) return;
    o = o || {};
    const total = Math.max(0, Number(o.total) || 0);
    const size = Math.max(1, Number(o.size) || 50);
    const pages = Math.max(1, Math.ceil(total / size));
    const page = Math.min(Math.max(1, Number(o.page) || 1), pages);
    if (!total) { host.innerHTML = ""; return; }
    const bs = (target, txt, dis, aria) =>
      `<button type="button" data-pg="${target}" ${dis ? "disabled" : ""} aria-label="${aria}" ` +
      `style="min-width:28px;padding:3px 9px;border:1px solid #e5e0d8;background:#fff;border-radius:6px;` +
      `font:inherit;font-size:12px;line-height:1.4;cursor:${dis ? "not-allowed" : "pointer"};opacity:${dis ? ".45" : "1"}">${txt}</button>`;
    host.innerHTML = `<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:12px;` +
      `color:#78716c;padding:6px 0"><span>共 <b>${total}</b> ${o.unit || "条"} · 第 ${page} / ${pages} 页</span>` +
      `<span style="display:inline-flex;gap:4px;margin-left:auto">` +
      bs(page - 1, "‹", page <= 1, "上一页") + bs(page + 1, "›", page >= pages, "下一页") +
      `</span></div>`;
    host.querySelectorAll("[data-pg]").forEach((b) => b.addEventListener("click", () => {
      if (b.disabled) return;
      const p = Math.min(pages, Math.max(1, Number(b.getAttribute("data-pg")) || 1));
      if (p === page) return;
      if (typeof o.onChange === "function") o.onChange(p, size);
    }));
  },
  bars() {}, donut() {}, line() {},
  empty: (c, o) => `<tr><td colspan="${c}" class="muted">${(o && o.title) || "无数据"}</td></tr>`,
  skeleton: () => "",
};

let CFG = null;
let currentRun = null;
let currentRunKind = ""; // pipeline | daily | script | ledger …
let es = null;
let hotPayload = null;

let benchData = null;
let benchSig = "";        // 上一次渲染的数据指纹：相同则不重建（避免动效重播）
let bvQuiet = false;      // 增量刷新标记：静默重绘，不播图表入场动画
/* 系统「减少动态效果」偏好（tablekit 内部也有一份，这里供 app.js 传参用） */
function prefersReducedMotion() {
  try {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  } catch (_) {
    return false;
  }
}
// 账号筛选的唯一事实来源是 #bench-account 的值（见 loadBench），不再维护冗余副本。
let autoFlows = [];
let autoEs = null;
let autoRunId = null;
// 「被挂车最多的商品」当前渲染的数据（供点击展开详情时按索引取用，避免重复请求）
let benchProductsCache = [];
// 组内对标模式：非空时 loadBench 只取这些账号（对应组），下拉也锁定该子集
let benchGroupAccounts = null;
let benchGroupId = null;
// 商品列表的事件委托只绑一次（二次渲染不重复绑定）
let benchProductsBound = false;

/* ─────────── 工具 ─────────── */
const API_TIMEOUT = 20000;
async function api(url, opts) {
  // P2-5：后端 hang 时「加载中…」会永久停留，用户既看不到失败也没有重试入口。
  // 统一 20s 超时，并把中断翻译成可读错误（调用点的 catch 会走 toast / 内联错误态）。
  const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
  const rq = Object.assign({}, opts || {});
  if (ctl) rq.signal = ctl.signal;
  const timer = setTimeout(() => { if (ctl) ctl.abort(); }, API_TIMEOUT);
  let r;
  try {
    r = await fetch(url, rq);
  } catch (e) {
    throw (e && e.name === "AbortError")
      ? new Error("请求超时（" + API_TIMEOUT / 1000 + " 秒）")
      : e;
  } finally {
    clearTimeout(timer);
  }
  if (!r.ok) {
    const t = await r.text().catch(() => "");
    throw new Error(`${r.status} ${t.slice(0, 200)}`);
  }
  return r.json();
}
let toastTimer = null;
/* P2-6：原来成功与错误同色、长文案 3.6s 就消失且不可回看。
   现在按严重度着色（.error/.ok）、长错误停留更久（6–14s）、点击可手动关闭。
   错误类文案自动识别，几十个调用点无需逐个改造。 */
function toast(msg, kind) {
  const t = $("#toast");
  if (!t) return;
  const k = kind || (/失败|错误|异常|超时|无法|不能|error|failed/i.test(String(msg)) ? "error" : "");
  t.textContent = msg;
  t.className = "toast" + (k ? " " + k : "");
  void t.offsetWidth;                 // 强制回流：连续两条也能重播过渡
  t.classList.add("show");
  t.onclick = () => {
    t.classList.remove("show");
    if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  };
  if (toastTimer) clearTimeout(toastTimer);
  const ms = k === "error" ? Math.max(6000, Math.min(14000, String(msg).length * 120)) : 3600;
  toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}

/* P2-5：轮询退避 —— 后端不可用时不要每 30/60 秒各弹一次失败提示 */
const _pollBackoff = {};
function pollBlocked(key) {
  const s = _pollBackoff[key];
  return !!(s && Date.now() < s.until);
}
function pollFail(key) {
  const n = ((_pollBackoff[key] || {}).n || 0) + 1;
  const wait = Math.min(600000, 30000 * Math.pow(2, Math.min(n - 1, 4)));
  _pollBackoff[key] = { n: n, until: Date.now() + wait };
}
function pollOk(key) { delete _pollBackoff[key]; }
// 统一错误出口：没写 try/catch 的调用点也会给用户可见提示，而不是静默 rejection（P1-8）
window.addEventListener("unhandledrejection", (ev) => {
  const r = ev.reason;
  const msg = (r && (r.message || String(r))) || "未知错误";
  toast("操作失败：" + msg, "error");
});
function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-");
}
function today(offset = 0) {
  const d = new Date(Date.now() + offset * 864e5);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

/* ─────────── 导航 ─────────── */
/* 切视图的唯一入口（导航点击 / #hash 深链 / 前进后退共用一套逻辑） */
function switchView(name) {
  if (!name || !$("#view-" + name)) return;
  clearSheetHash();   // P2-3：离开表格视图时同步清掉 #sheet 路由，否则刷新会跳回旧表
  abortSheetLoads();  // 在途的表格请求作废，迟到的响应不得再写 DOM
  $$(".nav-item").forEach((x) => {
    const on = x.dataset.view === name;
    x.classList.toggle("active", on);
    if (on) x.setAttribute("aria-current", "page");
    else x.removeAttribute("aria-current");
  });
  if (name !== "sheet") {
    $$(".view").forEach((v) => v.classList.remove("active"));
    const v = $("#view-" + name);
    if (v) v.classList.add("active");
  }
  if (name === "overview") loadOverview(true);
  if (name === "reports") loadReports();
  if (name === "schedule") loadSchedule();
  if (name === "hot") loadHot();
  if (name === "reverse" && window.RV_load) window.RV_load();   // 提示词反推（reverse.js）
  if (name === "bench") loadBench(true);
  if (name === "accounts") loadAccounts(true);
}

$$(".nav-item").forEach((n) => {
  n.addEventListener("click", (e) => {
    e.preventDefault();
    const name = n.dataset.view;
    switchView(name);
    // P2-12：视图写进 URL（pushState 不触发 hashchange，不会被自己重复处理）
    try {
      if (name && location.hash !== "#" + name) history.pushState(null, "", "#" + name);
    } catch (_) { /* file:// 等环境忽略 */ }
  });
});

// 当前激活视图的 data-view 值（无匹配时回落 "overview"）
function activeViewName() {
  const n = $(".nav-item.active");
  return (n && n.dataset.view) || "overview";
}

// 流水线完成后：总览/台账总是刷；再按当前激活视图补刷对应数据，
// 避免"停在某个页面跑完却看不到新数据"（如对标分析/雷达/账号清单）。
function refreshActiveView() {
  loadOverview(true);
  // 表格视图不是导航项：它开着时只刷自己，不重复刷来源页
  const sv = $("#view-sheet");
  if (sv && sv.classList.contains("active")) { sheetViewReload(); return; }
  const v = activeViewName();
  if (v === "bench") loadBench(true);
  if (v === "reports") loadReports();
  if (v === "schedule") loadSchedule();
  if (v === "hot") loadHot();
  if (v === "run") refreshTransBadge(); // 运行页：刷新「翻译回填」按钮的待译角标
  if (v === "accounts") loadAccounts(true);
}

/* ─────────── 翻译回填（手动立即触发 15 分钟巡检的翻译动作）─────────── */
let _transPolling = false;

async function getTransTotal() {
  try {
    const d = await api("/api/translation/pending");
    return (d && d.total) || 0;
  } catch (e) { return 0; }
}

async function refreshTransBadge() {
  const badge = $("#trans-pending-badge");
  if (!badge) return;
  const n = await getTransTotal();
  badge.textContent = n ? `（待译 ${n}）` : "";
  badge.style.visibility = n ? "visible" : "hidden";
}

async function backfillTranslation() {
  if (_transPolling) return;
  _transPolling = true;
  try {
    const d = await api("/api/translation/backfill", { method: "POST" });
    toast(d.note || "已开始中文翻译并回填，可在独立任务查看进度");
    await refreshTransBadge();
  } catch (e) { toast("队列刷新失败：" + e.message); }
  finally { _transPolling = false; }
}

/* ─────────── 待接管（无人值守异常队列 · 方案五）───────────
   数据源 GET /api/pipeline/status：daily_update 的 run_summary + runs.jsonl 推导。
   无失败时整块隐藏、导航角标不显示；有失败才占位，避免常态噪声。 */
let takeoverData = null;
const TK_LEVEL = { INFO: "正常", WARN: "注意", ERROR: "异常" };

async function loadPipelineStatus(force) {
  try {
    const d = await api("/api/pipeline/status" + (force ? "?force=true" : ""));
    takeoverData = d;
    renderTakeover(d);
    return d;
  } catch (e) {
    console.warn("[takeover] 待接管状态读取失败", e);   // P2-14：失败要让排障有迹可循
    return null; // 状态接口失败不影响总览主流程
  }
}

function renderTakeover(d) {
  const list = (d && d.takeover) || [];
  const alerts = (d && d.alerts) || [];
  const n = list.length;
  // P2-1：阶段失败（前置检查/商品/台账）没有对应账号，也要占位，不能被隐藏掉
  const badgeN = n + alerts.length;
  // 2026-10-21 验收 P1-D：回执本身不可信（旧格式缺阶段/缺轮次、过期、running 卡死）时，
  // 即使队列为空也要露面 —— 「假绿」恰恰是「没有待接管 + level=INFO」这种形态。
  const doubt = !!(d && d.receipt && d.receipt.stale);
  const badge = $("#tk-nav-badge");
  if (badge) {
    // 2026-10-22 UI 整改：0 待接管一律不显示角标（回执存疑由面板自己说明），
    // 不再用「红色的 0」或「!」在侧栏制造噪音。
    badge.textContent = badgeN > 99 ? "99+" : String(badgeN);
    badge.classList.toggle("hidden", !badgeN);
  }
  const panel = $("#takeover-panel");
  if (!panel) return;
  panel.classList.toggle("hidden", !badgeN && !doubt);
  // 2026-10-22 UI 整改：0 待接管时收紧面板，不再留 120px 空白 + 可点的主操作
  panel.classList.toggle("is-empty", !badgeN);
  const retryAll = $("#tk-retry-all");
  if (retryAll) retryAll.disabled = !badgeN;
  const box = $("#tk-list");
  if (!badgeN && !doubt) {
    if (box) box.innerHTML = ""; // 清掉上一轮残留，避免隐藏后面板仍有旧条目
    return;
  }
  if (!badgeN && box && alerts.length === 0) box.innerHTML = ""; // 只报回执存疑，不留旧条目

  const cnt = $("#tk-count");
  // 只有「回执存疑」而队列为空时不要显示红色的 0（`.tk-count` 自带 display，用行内样式才盖得住）
  if (cnt) {
    cnt.textContent = badgeN;
    cnt.style.display = badgeN ? "" : "none";
  }
  const sub = $("#tk-sub");
  if (sub) {
    const s = d.summary || {};
    const rc = d.receipt || {};
    // 2026-10-22 UI 整改：原来是 `·` 拼成的一行 100+ 字（含「回执缺阶段结论（旧格式…）」），
    // 现在拆成「字段名 + 值」的小组，存疑原因单独成行，可扫读。
    const kv = [];
    const push = (k, v, cls) =>
      kv.push(`<span class="tk-kv"><span class="tk-k">${esc(k)}</span><span class="tk-v ${cls || ""}">${esc(v)}</span></span>`);
    if (s.date) push("最近一轮", s.date);
    if (s.level) push("级别", TK_LEVEL[s.level] || s.level, s.level === "ERROR" ? "tk-warn" : "");
    if (rc.rounds) push("轮次", `第 ${rc.rounds} 轮`);
    if (rc.ended_at) push("回执时间", fmtTime(rc.ended_at));
    if (rc.failed_stages && rc.failed_stages.length) push("失败阶段", rc.failed_stages.join("、"), "tk-warn");
    push("失败账号", `${n} 个`);
    const doubtReasons = rc.stale ? (rc.stale_reasons || []) : [];
    sub.innerHTML = kv.join("") + (doubtReasons.length
      ? `<span class="tk-doubt">回执存疑：${esc(doubtReasons.join("；"))}</span>` : "");
  }
  if (!box) return;
  const alertHtml = alerts.map((a) => `<div class="tk-item">
      <div class="tk-main"><b>${esc(a.stage || "阶段")}</b><span class="tk-reason">${esc(a.reason || "失败")}</span></div>
      <div class="tk-meta"><span>${esc(a.recovery || "")}</span></div>
    </div>`).join("");
  box.innerHTML = alertHtml + list.map((it) => {
    const acc = esc(it.account || "");
    const meta = [];
    if (it.attempts > 1) meta.push(`已重试 ${it.attempts} 次`);
    if (it.at) meta.push(fmtTime(it.at));
    if (it.duration_s) meta.push(`${Math.round(it.duration_s)}s`);
    return `<div class="tk-item">
      <div class="tk-main"><b>${acc}</b><span class="tk-reason">${esc(it.reason || "失败")}</span></div>
      <div class="tk-meta">${meta.map((m) => `<span>${esc(m)}</span>`).join("")}</div>
      <button class="btn ghost sm" onclick="retryTakeover(['${acc}'])">重跑</button>
    </div>`;
  }).join("");
}

async function retryTakeover(accounts) {
  const list = Array.isArray(accounts) && accounts.length ? accounts : null;
  if (!list && !(takeoverData && takeoverData.failed)) {
    toast("当前没有待接管账号");
    return;
  }
  try {
    const d = await api("/api/pipeline/retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(list ? { accounts: list } : {}),
    });
    const n = (d.accounts || []).length;
    toast(n ? `已提交重跑：${n} 个账号（含重试与超时保护）` : "已提交重跑全部账号");
    setTimeout(() => loadPipelineStatus(true), 1500);
  } catch (e) {
    toast("重跑提交失败：" + e.message);
  }
}

// 对标分析页「常驻自新」的安全门：用户正在展开/阅读某个手风琴或弹层时，
// 不做后台重渲染（renderBench 会整体重写 DOM，把展开态冲掉、内容跳动）。
// 命中任一交互态 → 本轮轮询跳过。
function benchBusy() {
  // 商品手风琴展开中
  if ($$("#bench-products .prod-head[aria-expanded='true']").length) return true;
  if ($$("#bench-products .prod-detail:not([hidden])").length) return true;
  // 账号下拉聚焦中
  const sel = $("#bench-account");
  if (sel && document.activeElement === sel) return true;
  // 任何弹层/抽屉打开中（遮罩带 hidden 切换）
  if ($(".modal:not(.hidden)")) return true;
  if ($(".drawer-mask:not(.hidden)")) return true;
  return false;
}

function pollBench() {
  if (document.visibilityState === "hidden") return;
  if (pollBlocked("bench")) return;   // P2-5：连续失败指数退避
  if (!$("#view-bench") || !$("#view-bench").classList.contains("active")) return;
  if (benchBusy()) return;
  // 鼠标停在图表/表格上时也不动（正在看 tooltip 或对比行，突然重绘最恼人）
  if (document.querySelector("#bv-viz:hover, .bv-wrap:hover, #bench-accounts:hover")) return;
  loadBench(false); // 不强制 refresh：后端 60s 缓存自然限流
}

// 标签页回到前台时补一次刷新（轮询在后台已暂停）
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") return;
  if ($("#view-bench").classList.contains("active")) pollBench();
  if ($("#view-overview").classList.contains("active") && !grpBusy()) loadOverview(false);
});

/* ─────────── 初始化 ─────────── */
/* ─────────── 区块折叠（分组 / 流水线 / 最近运行 / 自动化） ─────────── */
const FOLD_KEY_PREFIX = "wb.fold.";
const foldKey = (k) => FOLD_KEY_PREFIX + k;

function applyFold(key, collapsed) {
  const body = document.querySelector(`[data-fold-body="${key}"]`);
  const head = document.querySelector(`[data-fold-head="${key}"]`);
  if (body) body.classList.toggle("hidden", !!collapsed);
  // P2-9：读屏需要知道当前是展开还是折叠
  if (head) head.setAttribute("aria-expanded", collapsed ? "false" : "true");
  const caret = head && head.querySelector(".fold-caret");
  if (caret) caret.style.transform = collapsed ? "rotate(-90deg)" : "";
  const hint = head && head.querySelector(".fold-hint");
  if (hint) hint.textContent = collapsed ? "（已折叠）" : "";
}

function toggleFold(key) {
  let cur = false;
  try { cur = localStorage.getItem(foldKey(key)) === "1"; } catch (e) { /* 隐私模式 */ }
  const next = !cur;
  try { localStorage.setItem(foldKey(key), next ? "1" : "0"); } catch (e) { /* 忽略 */ }
  applyFold(key, next);
}

function initFolds() {
  document.querySelectorAll("[data-fold-head]").forEach((head) => {
    const key = head.dataset.foldHead;
    if (!key) return;
    let collapsed = false;
    try {
      collapsed = localStorage.getItem(foldKey(key)) === "1";
      // 兼容旧键：流水线状态曾用 wb.pipeCollapsed
      if (key === "pipeline" && localStorage.getItem("wb.pipeCollapsed") !== null) {
        collapsed = localStorage.getItem("wb.pipeCollapsed") === "1";
      }
    } catch (e) { /* 忽略 */ }
    applyFold(key, collapsed);
  });
}

/* 兼容旧调用 */
function togglePipeFold() { toggleFold("pipeline"); }

async function boot() {
  try { initFolds(); } catch (e) { /* 忽略 */ }
  try {
    CFG = await api("/api/config");
    $("#health-dot").className = "dot ok";
    $("#health-text").textContent = "已连接本机工作台";
  } catch (e) {
    $("#health-dot").className = "dot err";
    $("#health-text").textContent = "连接失败";
    return;
  }
  fillAccountSelects();
  renderSteps("#run-steps", CFG.local_steps, new Set(CFG.local_steps));
  $("#run-from").value = today(-7);
  $("#run-to").value = today();
  syncRunDates();
  $("#run-from").addEventListener("change", syncRunDates);
  $("#run-to").addEventListener("change", syncRunDates);
  $("#run-daily").addEventListener("change", syncDailyMode);
  syncDailyMode();
  await loadOverview();
  loadAutomations();

  adoptRunningOnBoot();   // P1-4：刷新后接管仍在后台跑的运行
}

function fillAccountSelects() {
  const opts = CFG.accounts.map((a) => `<option value="${a}">${a}</option>`).join("");
  $("#run-account").innerHTML = opts;
  const sel = $("#bench-account");
  if (sel) {
    // 初始只放占位项；真实选项由 benchmark 载荷重建（只含有数据的账号，
    // 避免 accounts.json 里的空壳账号混进下拉）
    sel.innerHTML = `<option value="">全部账号</option>`;
    sel.value = "";
    // fillAccountSelects() 会被 boot 反复调用，监听器只绑一次（P1-7）
    if (!sel.dataset.changeBound) {
      sel.dataset.changeBound = "1";
      sel.addEventListener("change", () => {
        loadBench(true);
      });
    }
  }
  const chips = CFG.accounts
    .map((a) => `<div class="chip" data-acc="${a}">${a}</div>`)
    .join("");
  $("#sc-accounts").innerHTML = chips;
  $$("#sc-accounts .chip").forEach((c) =>
    c.addEventListener("click", () => c.classList.toggle("on"))
  );
  renderRunMultiAccounts();   // 多账户批跑选择区
}

/* ── 多账户批跑：勾选 N 个账号顺序串行跑（一个运行任务 + 一条日志流） ── */
function renderRunMultiAccounts() {
  const box = $("#run-multi-list");
  if (!box) return;
  box.innerHTML = (CFG.accounts || []).map((a) =>
    `<label class="ma-item"><input type="checkbox" value="${esc(a)}" onchange="updateMultiCount()"> ${esc(a)}</label>`
  ).join("");
  updateMultiCount();
}
function getSelectedAccounts() {
  return [...$$("#run-multi-list input:checked")].map((c) => c.value);
}
function updateMultiCount() {
  const n = getSelectedAccounts().length;
  const badge = $("#run-multi-count");
  if (badge) badge.textContent = `已选 ${n}`;
}
function setAllAccounts(on) {
  $$("#run-multi-list input").forEach((c) => { c.checked = !!on; });
  updateMultiCount();
}
// 使用最近点击的泳道组；只填充运行清单内的启用账号。
function setAccountsByGroup() {
  const g = (groupsData && groupsData.groups || []);
  const target = g.find((x) => x.id === grpSelected);
  if (!target) {
    grpSelected = null;
    return toast("请先在总览点击一个泳道组，再按当前组填充");
  }
  const available = new Set((CFG && CFG.accounts) || []);
  const names = new Set((target.members || []).map((m) => m.account).filter((a) => available.has(a)));
  if (!names.size) return toast(`「${target.name}」暂无可运行账号，未更改勾选`);
  setAllAccounts(false);
  $$("#run-multi-list input").forEach((c) => { c.checked = names.has(c.value); });
  updateMultiCount();
  toast(`已选「${target.name}」的 ${names.size} 个账号`);
}

function renderSteps(sel, steps, onSet) {
  const box = $(sel);
  box.innerHTML = "";
  steps.forEach((s) => {
    const c = el("div", "chip" + (CFG.agent_steps.includes(s) ? " agent" : ""));
    c.textContent = `${s} ${CFG.step_labels[s] || ""}`;
    if (onSet.has(s)) c.classList.add("on");
    c.addEventListener("click", () => c.classList.toggle("on"));
    box.appendChild(c);
  });
  // ★ 2026-10-16：「翻译回填」独立开关 —— 翻译是 Agent 侧 LLM 动作（kunlun-fanyi），
  //   不是本地 pipeline 确定性步骤，故独立 chip。勾选后本次核心链跑完自动衔接翻译。
  //   ★ 该 chip 不参与本地 steps 收集（getSteps 按 .chip.on 取，必须排除本 chip，
  //   否则 s6_translate 会混进 --steps 让 pipeline.py 跑占位步 → 整链 fail）。
  if (false && sel === "#run-steps") {
    const tb = el("div", "chip agent tb-only");
    tb.id = "chip-trans-backfill";
    tb.textContent = "s6 翻译回填";
    tb.title = "翻译现已自动化：台账同步跑完即触发本地 LLM 翻译写 N 列（见 auto_translate.py）；无需勾选。此标记仅作提示。";
    tb.addEventListener("click", () => tb.classList.toggle("on"));
    box.appendChild(tb);
  }
}
function getSteps(sel) {
  // 排除独立开关 chip（.tb-only，如「翻译回填」），只收集本地 pipeline 步骤
  return $$(sel + " .chip.on:not(.tb-only)").map((c) => c.textContent.split(" ")[0]);
}

/* ─────────── 总览 ─────────── */
async function loadOverview(force) {
  try {
    const d = await api("/api/overview" + (force ? "?refresh=true" : ""));
    pollOk("overview");
    lastOv = d;   // 分组看板健康度（未跑/待译/视频数/最近更新）复用它，零新增请求
    $("#ov-updated").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN");
    const pend = (d.pending_translation || {}).total || 0;
    renderDiag(d.diagnostics, pend);
    renderAccounts(d.accounts, d.idle_accounts);
    renderRuns(d.runs);
    renderOverviewHead(d, pend);
  } catch (e) {
    pollFail("overview");
    toast("总览加载失败: " + e.message, "error");
  }
  loadGroups();
  loadPipelineStatus();
}

/* P2-3：openLedgerSheet 已删除（全站无调用方；入口统一走 openAccountLedger / openSheetView） */

function renderDiag(dg, pend) {
  const items = [
    ["pipeline 目录", dg.pipeline_exists],
    ["解释器", dg.python_exists],
    ["accounts.json", dg.accounts_exists],
    ["产物目录", dg.output_exists],
    
    ["独立任务引擎", true],
  ];
  const bad = items.filter(([, v]) => !v).length;
  const ledger = dg.ledger ? dg.ledger.split(/[\\/]/).pop() : "未找到";
  $("#diag-bar").innerHTML =
    `<span class="sl-item ${bad ? "bad" : "ok"}"><i class="sl-dot"></i>环境${bad ? ` ${bad} 项未就绪` : "就绪"}</span>` +
    `<span class="sl-sep"></span><span class="sl-item">台账：${esc(ledger)}</span>` +
    (pend ? `<span class="sl-sep"></span><span class="sl-item warn"><i class="sl-dot"></i>待翻译 ${pend} 条</span>` : "") +
    (bad ? `<span class="sl-sep"></span><span class="sl-item">${items.filter(([, v]) => !v).map(([k]) => esc(k)).join("、")}</span>` : "") +
    `<a class="sl-link" href="#pipe-panel">诊断详情</a>`;
}

/* ── 总览：一行发现 + 四格 KPI + 三段覆盖（全部取 /api/overview，零新增请求） ── */
function ovToday(ts) {
  if (!ts) return false;
  const ms = typeof ts === "number" && ts < 1e11 ? ts * 1000 : ts;   // 后端存 epoch 秒
  const d = new Date(ms), n = new Date();
  return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
}

function renderOverviewHead(d, pend) {
  const accs = d.accounts || [];
  const err = accs.filter((a) => a.severity === "error");
  const warn = accs.filter((a) => a.severity === "warn");
  const runs = (d.runs || []).filter((r) => ovToday(r.started_at));
  const done = runs.filter((r) => r.status === "done").length;
  const failed = runs.filter((r) => r.status === "failed").length;

  const f = $("#ov-finding");
  if (f) {
    const bits = [];
    bits.push(`${accs.length} 个账号，今天 ${done} 条执行成功${failed ? `、${failed} 条失败` : ""}`);
    if (err.length) bits.push(`${err.length} 条链路有异常`);
    else if (warn.length) bits.push(`${warn.length} 条链路缺口`);
    else bits.push("没有异常");
    if (pend) bits.push(`${pend} 条译文待回填`);
    f.textContent = bits.join("，") + "。";
  }

  const kpi = $("#ov-kpi");
  if (kpi) {
    const cell = (label, value, unit, cap, cls) =>
      `<div class="kpi ${cls || ""}"><div class="kpi-l">${label}</div>` +
      `<div class="kpi-v">${value}${unit ? `<span class="u">${unit}</span>` : ""}</div>` +
      `<div class="kpi-cap">${cap}</div></div>`;
    kpi.innerHTML =
      cell("账号", accs.length, "个", `启用 ${accs.filter((a) => !a.disabled).length} · 未启用 ${(d.idle_accounts || []).length}`) +
      cell("今日执行", done, "条", failed ? `失败 ${failed} 条` : "无失败", done ? "" : "") +
      cell("待处理", err.length + warn.length, "条", err.length || warn.length ? `异常 ${err.length} · 缺口 ${warn.length}` : "无异常", err.length ? "bad" : warn.length ? "warn" : "") +
      cell("待翻译", pend, "条", pend ? "巡检 15min 内回填" : "全部已译", pend ? "warn" : "");
  }
  renderStageStrip(accs);
}

/* 招牌元素：每账号 抓取→转写→入账 三段覆盖，异常优先 */
function renderStageStrip(accs) {
  const box = $("#stage-strip");
  if (!box) return;
  const rank = { error: 0, warn: 1, ok: 2 };
  const ratio = (n, d) => (d ? Math.max(0, Math.min(1, n / d)) : null);
  const seg = (n, total) => {
    const r = ratio(n, total);
    if (r === null) return `<span class="stg-cell"><span class="stg-track"><i class="none"></i></span><span class="stg-n">—</span></span>`;
    const cls = r >= 1 ? "" : r > 0 ? "part" : "none";
    return `<span class="stg-cell"><span class="stg-track"><i class="${cls}" style="width:${(r * 100).toFixed(0)}%"></i></span>` +
      `<span class="stg-n">${n}/${total}</span></span>`;
  };
  const list = accs.slice().sort((a, b) => (rank[a.severity] ?? 3) - (rank[b.severity] ?? 3));
  const shown = list.slice(0, 8);
  box.innerHTML = shown.map((a) => {
    const image = a.media_type === "image";
    const audio = a.audio_count || 0;
    // P2-1：图文账号后端同样返回真实的转写/商品/翻译统计，前端不再统一写「—」
    //（写「—」会与"状态=完整"自相矛盾）。分母取 max(音频, 抓取)：避免「抓 35/31」这类 >100% 读数。
    const base = Math.max(audio, a.scrape_items || 0);
    const num = (n, d) => (d ? `${n}/${d}` : "—");
    const st = a.severity === "error" ? "err" : a.severity === "warn" ? "warn" : "ok";
    const stText = a.severity === "error" ? `异常 ${(a.gaps || []).length}` : a.severity === "warn" ? `缺口 ${(a.gaps || []).length}` : "完整";
    const tip = (a.gaps || []).map((g) => g.msg).join("；") || "无缺口";
    const accLit = esc(JSON.stringify(a.account));
    const imgTag = image && audio ? `<span class="fx">图文历史</span>` : "";
    return `<div class="stg-row" tabindex="0" role="button" title="${esc(`${a.account}｜${tip}`)}"
      onclick="openAccountLedger(${accLit})"
      onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openAccountLedger(${accLit});}">
      <span class="stg-name">${esc(a.account)}${imgTag}</span>
      ${seg(a.scrape_items, base)}
      ${seg(a.transcript_count, audio)}
      ${seg(a.ledger_covered == null ? 0 : a.ledger_covered, a.scrape_items)}
      ${seg(a.products_count, a.scrape_items)}
      <span class="stg-st ${st}">${stText}</span>
      <span class="stg-sum">抓 ${num(a.scrape_items, base)} · 转 ${num(a.transcript_count, audio)} · 入 ${a.ledger_covered == null ? "—" : num(a.ledger_covered, a.scrape_items)} · 商 ${num(a.products_count, a.scrape_items)} · ${stText}</span>
    </div>`;
  }).join("");

  const note = $("#stage-foot-note");
  if (note) {
    const err = accs.filter((a) => a.severity === "error").length;
    const warn = accs.filter((a) => a.severity === "warn").length;
    const head = err ? `异常 ${err} 条` : warn ? `缺口 ${warn} 条（无异常）` : "全部完整";
    note.textContent = `${head}，前 ${shown.length} 条按严重度排序，其余在下方表格`;
  }
  const more = $("#stage-more");
  if (more) more.textContent = `查看全部 ${accs.length} 个账号`;
}

let pipeExpanded = false;
let lastPipeList = [];
function renderAccounts(list, idle) {
  const box = $("#pipe-table");
  const rows = list || [];
  lastPipeList = rows;
  const shown = pipeExpanded ? rows : rows.slice(0, 12);
  // P1-2：60s 轮询整块重建会把表内滚动打回表头（用户正读第 25 行就被弹回顶部）
  const wrap0 = box.querySelector(".table-wrap");
  const keepTop = wrap0 ? wrap0.scrollTop : 0;
  box.innerHTML = rows.length
    ? `<div class="table-wrap">${pipeTableHtml(shown, rows.length)}</div>`
    : `<div class="muted">未读取到账号配置（accounts.json）</div>`;
  const pipeTbl = box.querySelector(".pipe-tbl");
  if (pipeTbl) TKV.resizable(pipeTbl, "pipe", { min: 60 });
  if (keepTop) {
    const wrap1 = box.querySelector(".table-wrap");
    if (wrap1) wrap1.scrollTop = keepTop;
  }

  // P2-4：idle === null 表示「本次不动未启用账号区块」（expandPipe 重渲染走这条），
  // 否则点「显示全部」会把下方 #idle-wrap 清空并隐藏，只能等下一轮轮询恢复。
  if (idle === null) return;
  const idleList = idle || [];
  const iw = $("#idle-wrap");
  if (idleList.length) {
    iw.classList.remove("hidden");
    $("#idle-grid").innerHTML =
      idleList.map((a) => esc(a.account)).join("、") + "（无任何产物，从未归档）";
  } else {
    iw.classList.add("hidden");
    $("#idle-grid").innerHTML = "";
  }
}

/* 流水线状态：每账号一行，悬停看缺口详情，点击打开台账 */
function expandPipe(ev) {
  if (ev) ev.stopPropagation();
  pipeExpanded = true;
  renderAccounts(lastPipeList, null);
}

function pipeTableHtml(list, total) {
  const head = `<thead><tr>
    <th>账号</th><th class="rt">音频</th><th class="rt">抓取</th><th class="rt">转写</th>
    <th class="rt">台账</th><th class="rt">商品</th><th class="rt">翻译</th>
    <th class="rt">落地率</th><th>状态</th>
  </tr></thead>`;

  const body = list
    .map((a) => {
      const image = a.media_type === "image";
      const audio = a.audio_count;
      // 2026-10-22 UI 整改：图文历史账号「抓取 38 / 音频 31」原来被判成缺口染红，
      // 与真正缺口的红色混在一起（红=两种相反含义）。基准取 max(音频, 抓取)，
      // 只有「覆盖 < 基准」才染红，「多出基准」用中性色。
      const base = Math.max(audio || 0, a.scrape_items || 0);
      const scrapeCovered = (a.scrape_items || 0) + (a.scrape_errors || 0);
      const scrapeGap = base ? base - scrapeCovered : 0;
      const scrapeCls = scrapeGap > 0 ? "bad" : scrapeCovered > base ? "" : "ok";
      const transOk = audio ? (a.transcript_count || 0) >= audio : true;   // 转出更多不算缺口
      const prodOk = !a.scrape_items || a.products_count >= a.scrape_items;
      const ledgerOk = a.scrape_items && a.ledger_covered != null
        ? a.ledger_covered >= a.scrape_items : true;
      // 翻译：抓取视频里有多少已译（N 列有值）；未译标 amber 提醒（巡检 15min 内回填，
      // 属正常待办非异常，故用 amber 不用 bad，也不拖累 severity）
      const transCov = a.translation_covered;
      const transOk2 = (transCov != null && a.scrape_items)
        ? transCov === a.scrape_items : true;
      const scrapeCell = a.scrape_errors
        ? `${a.scrape_items}<span class="fx">+${a.scrape_errors}</span>`
        : `${a.scrape_items}`;
      // P2-1：图文账号的翻译覆盖后端有真实值，不再前端写「—」
      const transCell = transCov == null ? "—"
        : `${transCov}<span class="fx">/${a.scrape_items}</span>`;
      const transTip = transCov == null ? "翻译未统计"
        : `翻译 ${transCov}/${a.scrape_items}${transOk2 ? "（已全译）" : "（未译，待 15min 巡检回填 N 列）"}`;
      const icon = "";
      const stText = a.severity === "error"
        ? `异常 ${a.gaps.length}`
        : a.severity === "warn" ? `缺口 ${a.gaps.length}` : "完整";
      const tip = (a.gaps || []).map((g) => g.msg).join("；") || "无缺口";
      const manTip = a.manifest_exists
        ? `索引 ${a.manifest_videos} 条 · 最近成功 ${a.last_success_date || "-"}`
        : "未建索引（manifest 未生成）";

      const accLit = esc(JSON.stringify(a.account));
      // P2-10：整行点击是主入口，必须可键盘触发（Enter/Space），与泳道卡片保持一致
      return `<tr tabindex="0" role="button" onclick="openAccountLedger(${accLit})"
        onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();openAccountLedger(${accLit});}"
        title="${esc(`${a.account}｜${tip}｜${manTip}`)}">
        <td class="acc">${esc(a.account)}${image && audio ? `<span class="fx">图文历史</span>` : ""}</td>
        <td class="rt">${audio || "—"}</td>
        <td class="rt ${scrapeCls}">${scrapeCell}</td>
        <td class="rt ${audio ? (transOk ? "ok" : "bad") : ""}">${audio ? `${a.transcript_count}<span class="fx">/${audio}</span>` : "—"}</td>
        <td class="rt ${ledgerOk ? "ok" : "bad"}">${a.ledger_covered == null ? "—" : `${a.ledger_covered}<span class="fx">/${a.scrape_items}</span>`}</td>
        <td class="rt ${prodOk ? "ok" : "bad"}">${a.products_count}<span class="fx">/${a.scrape_items}</span></td>
        <td class="rt ${transCov == null ? "" : transOk2 ? "ok" : "amber"}" title="${esc(transTip)}">${transCell}</td>
        <td class="rt"><span class="mini-bar"><i class="${a.severity}" style="width:${a.landing}%"></i></span>${a.landing}%</td>
        <td><span class="st ${a.severity}">${stText}</span></td>
      </tr>`;
    })
    .join("");

  const moreRow = !pipeExpanded && total > list.length
    ? `<tr class="more-row" onclick="expandPipe(event)"><td colspan="9">显示全部 ${total} 个账号</td></tr>`
    : "";
  return `<table class="tbl pipe-tbl">${head}<tbody>${body}${moreRow}</tbody></table>`;
}

function renderRuns(runs) {
  const box = $("#recent-runs");
  if (!runs || !runs.length) {
    box.innerHTML = `<div class="muted">暂无运行记录</div>`;
    return;
  }
  // 2026-10-22 UI 整改：补 interrupted（原来原样输出英文）；未知状态不再裸奔
  const badge = { done: "green", failed: "red", running: "blue", stopped: "amber", interrupted: "amber" };
  const label = { done: "成功", failed: "失败", running: "运行中", stopped: "已终止", interrupted: "已中断" };
  box.innerHTML = runs
    .slice(0, 6)
    .map(
      (r) => `<div class="run-row">
        <span class="badge ${badge[r.status] || "gray"}">${label[r.status] || r.status}</span>
        <span class="t">${r.title || r.kind}</span>
        <span class="tm">${fmtTime(r.started_at)}${r.duration_s ? " · " + Math.round(r.duration_s) + "s" : ""}</span>
      </div>`
    )
    .join("");
}

/* ─────────── 自动化流程 ─────────── */
async function loadAutomations() {
  const box = $("#auto-grid");
  if (!box) return;
  try {
    const d = await api("/api/automations");
    autoFlows = d.items || [];
    renderAutomations();
  } catch (e) {
    box.innerHTML = `<div class="card">自动化流程读取失败：${esc(e.message)}</div>`;
  }
}

function renderAutomations() {
  const box = $("#auto-grid");
  if (!autoFlows.length) {
    box.innerHTML = `<div class="muted">暂无自动化流程定义（data/automations.json）</div>`;
    return;
  }
  const rows = autoFlows.map((f) => {
    const accs = (f.accounts || []).length ? f.accounts.join(" · ") : "全部账号";
    const steps = (f.agent_steps || []).map((s) => (CFG && CFG.step_labels[s]) || s).join(" · ");
    const runnable = f.runnable
      ? `<button class="btn sm" onclick="runAutomation(${esc(JSON.stringify(f.id))})">执行</button>`
      // 2026-10-22：操作列放不下「在独立任务中执行」，被 ellipsis 截成「需转…」→ 用短标签 + title
      : `<span class="as" title="请在独立任务中执行">独立任务</span>`;
    const ho = "";
    const state = f.enabled === false ? "停用" : "启用";
    const stateCls = f.enabled === false ? "gray" : "green";
    return `<tr>
      <td>
        <div class="an" title="${esc(f.name || f.id)}">${esc(f.name || f.id)}</div>
        <div class="as" title="${esc([f.desc || steps || "", accs].filter(Boolean).join(" · "))}">${esc([f.desc || steps || "", accs].filter(Boolean).join(" · "))}</div>
      </td>
      <td class="as">${esc(f.schedule || "手动")}</td>
      <td><span class="badge ${stateCls}">${state}</span></td>
      <td class="rt"><div class="act">${runnable}${ho}</div></td>
    </tr>`;
  }).join("");
  box.innerHTML = `<table class="tbl">
    <thead><tr><th>流程</th><th>调度</th><th>状态</th><th class="rt">操作</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

async function runAutomation(id) {
  const flow = autoFlows.find((f) => f.id === id) || { name: id };
  try {
    const run = await api(`/api/automations/${encodeURIComponent(id)}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    toast("已启动：" + (run.title || flow.name));
    attachAutoRun(run.id, run.title || flow.name);
  } catch (e) {
    toast(e.message);
  }
}

function attachAutoRun(id, title) {
  if (autoEs) autoEs.close();
  autoRunId = id;
  $("#auto-log-title").textContent = "运行日志 · " + (title || id);
  $("#auto-log-wrap").classList.remove("hidden");
  $("#auto-log").textContent = "";
  setAutoStatus("running");
  autoEs = new EventSource(`/api/runs/${id}/stream`);
  autoEs.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.line) appendAutoLog(msg.line);
    if (msg.status) {
      setAutoStatus(msg.status, msg.exit_code);
      if (msg.done) {
        autoEs.close();
        autoEs = null;
        refreshActiveView();
        loadAutomations();
      }
    }
  };
  autoEs.onerror = () => {
    if (autoEs) { autoEs.close(); autoEs = null; }
    setAutoStatus("failed");
    toast("自动化日志连接中断，请刷新页面查看结果");
  };
}

function appendAutoLog(item) {
  const line = el("span", "ln");
  const t = item.text || "";
  if (/FAIL|错误|失败|EXCEPTION|Traceback|exit_code=[1-9]/.test(t)) line.classList.add("err");
  else if (/PASS|ok=True|DONE|完成/.test(t)) line.classList.add("ok");
  else if (/^\[/.test(t)) line.classList.add("sys");
  line.textContent = `[${String(item.t).padStart(7)}s] ${t}\n`;
  const log = $("#auto-log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
  log.appendChild(line);
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function setAutoStatus(status, code) {
  const map = {
    running: ["blue", "运行中"], done: ["green", "成功"],
    failed: ["red", "失败 (exit " + code + ")"], stopped: ["amber", "已终止"],
  };
  const [cls, text] = map[status] || ["gray", "空闲"];
  const badge = $("#auto-log-status");
  badge.className = "badge " + cls;
  badge.textContent = text;
}

function clearAutoLog() { $("#auto-log").textContent = ""; }

async function stopAuto() {
  if (!autoRunId) return toast("当前无自动化任务");
  await api(`/api/runs/${autoRunId}/stop`, { method: "POST" });
  toast("已发送终止信号");
}

/* ─────────── 一键运行 ─────────── */
function presetAll() {
  $$("#run-steps .chip").forEach((c) => {
    if (!c.classList.contains("agent") && !c.classList.contains("tb-only")) c.classList.add("on");
  });
}
function presetCore() {
  const core = ["s1_download", "s2_scrape", "s5_transcribe"];
  $$("#run-steps .chip").forEach((c) => {
    if (c.classList.contains("tb-only")) return; // 翻译回填独立开关，不参与核心链预设
    const s = c.textContent.split(" ")[0];
    c.classList.toggle("on", core.includes(s));
  });
}

let runSubmitting = false;

/* 日期互校：结束不得早于开始（P2-10），输入框 min/max 同步 */
function syncRunDates() {
  const from = $("#run-from");
  const to = $("#run-to");
  if (!from || !to) return;
  if (from.value) to.min = from.value; else to.removeAttribute("min");
  if (to.value) from.max = to.value; else from.removeAttribute("max");
}

/* daily 模式下步骤/语言/后端/日期都不生效，置灰以免「以为只跑 s2」 */
function syncDailyMode() {
  const daily = $("#run-daily").checked;
  const steps = $("#run-steps");
  if (steps) steps.classList.toggle("is-off", daily);
  ["#run-lang", "#run-backend", "#run-from", "#run-to"].forEach((s) => {
    const e = $(s);
    if (e) e.disabled = daily;
  });
}

async function submitRun() {
  if (runSubmitting) return;
  runSubmitting = true;
  const daily = $("#run-daily").checked;
  // 翻译回填开关（独立 chip #chip-trans-backfill，不参与本地 steps 收集）
  const transChip = $("#chip-trans-backfill");
  const translateBackfill = transChip ? transChip.classList.contains("on") : false;
  // 有勾选时以勾选为准；无勾选时沿用主下拉。
  const sel = $("#run-account");
  const picked = getSelectedAccounts();
  const accounts = [...new Set(picked.length ? picked : [sel.value].filter(Boolean))];
  const multi = accounts.length >= 2;
  const payload = daily
    ? { kind: "daily", accounts }   // daily 本征多账号（daily_update.py --accounts）
    : {
        kind: "pipeline",
        account: accounts[0],
        ...(multi ? { accounts } : {}),   // ≥2 账号 → 后端走顺序串行驱动
        date_from: $("#run-from").value,
        date_to: $("#run-to").value,
        steps: getSteps("#run-steps"),
        lang: $("#run-lang").value,
        backend: $("#run-backend").value,
        no_resume: $("#run-no-resume").checked,
        allow_scrape_errors: $("#run-tolerant").checked,
        translate_backfill: translateBackfill,
      };
  try {
    if (!accounts.length) return toast("请先选择账号");
    if (!daily && $("#run-from").value && $("#run-to").value &&
        $("#run-from").value > $("#run-to").value) {
      return toast("开始日期不能晚于结束日期");
    }
    if (!daily && !payload.steps.length) return toast("请至少选择一个执行步骤");
    const run = await api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    toast(multi && !daily ? `已启动：${accounts.length} 个账号顺序串行` : "已启动：" + run.title);
    buildRunProgress(daily ? ["daily"] : (payload.steps || ["s1_download", "s2_scrape", "s5_transcribe"]));
    attachRun(run.id, run.agent_pending_note, daily ? "daily" : "pipeline");
  } catch (e) {
    toast("启动失败: " + e.message);
  } finally {
    runSubmitting = false;
  }
}

function clearLog() {
  $("#log").textContent = "";
}
function appendLog(item) {
  const line = el("span", "ln");
  const t = item.text || "";
  try { parseRunLine(t); } catch (e) { /* 进度面板解析失败不影响日志 */ }
  if (/FAIL|错误|失败|EXCEPTION|Traceback|exit_code=[1-9]/.test(t)) line.classList.add("err");
  else if (/PASS|ok=True|DONE|完成/.test(t)) line.classList.add("ok");
  else if (/^\[/.test(t)) line.classList.add("sys");
  line.textContent = `[${String(item.t).padStart(7)}s] ${t}\n`;
  const log = $("#log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
  log.appendChild(line);
  // 前端日志也设上限，长批跑不无限累积节点（P2-12）
  while (log.childElementCount > 3000 && log.firstElementChild) {
    log.removeChild(log.firstElementChild);
  }
  if (atBottom) log.scrollTop = log.scrollHeight;
}

function setLogStatus(status, code) {
  const map = {
    running: ["blue", "运行中"], done: ["green", "成功"],
    failed: ["red", "失败 (exit " + code + ")"], stopped: ["amber", "已终止"],
  };
  const [cls, text] = map[status] || ["gray", "空闲"];
  const badge = $("#log-status");
  badge.className = "badge " + cls;
  badge.textContent = text;
}

function attachRun(id, note, kind) {
  if (es) es.close();
  currentRun = id;
  currentRunKind = kind || "";
  // P1-4：记到 sessionStorage，刷新后能认出"这是我刚才在跑的那个任务"
  try { sessionStorage.setItem("wb.runId", id); } catch (_) { /* 隐私模式 */ }
  $("#log").textContent = "";
  if (!runProgress) { const p2 = $("#run-progress-panel"); if (p2) p2.hidden = true; }
  setLogStatus("running");
  if (note) {
    appendLog({ t: 0, text: note });
    toast(note);
  }
  es = new EventSource(`/api/runs/${id}/stream`);
  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.line) appendLog(msg.line);
    if (msg.status) {
      setLogStatus(msg.status, msg.exit_code);
      if (msg.status === 'failed') showFailAnalysis();
      if (msg.done) {
        if (runProgress && (msg.status === "failed" || msg.status === "stopped")) {
          runProgress.order.forEach((k) => {
            const state = runProgress.state[k];
            if (state.status === "running") setRunStep(k, msg.status === "stopped" ? "skip" : "fail", msg.status === "stopped" ? "用户终止" : "运行失败，详见日志");
            else if (state.status === "wait") setRunStep(k, "skip", "未执行");
          });
          setRunProgressNote(msg.status === "stopped" ? "任务已终止" :
            (runProgress.batch ? "批次失败，后续账号未执行；本批次未自动同步台账，请检查已完成账号后手动同步。" : "运行失败，请查看日志"));
        }
        if (runProgressTimer) { clearInterval(runProgressTimer); runProgressTimer = null; }
        es.close(); es = null;
        try { sessionStorage.removeItem("wb.runId"); } catch (_) { /* 忽略 */ }
        // pipeline/daily 跑完：后端已自动启动「台账同步」，前端接管它的日志，
        // 同步 done 后再强制刷新（台账明细即时更新，2026-10-15）。
        if (msg.status === "done" && (currentRunKind === "pipeline" || currentRunKind === "daily")) {
          adoptLedgerSync();
        } else {
          refreshActiveView();
        }
        if (note && msg.status === "done") toast(note);
      }
    }
  };
  es.onerror = () => {
    if (es) { es.close(); es = null; }
    setLogStatus("failed");
    toast("实时日志连接中断，请刷新页面查看结果");
  };
}

// 找到后端自动触发的「台账同步」运行并接管日志；找不到（钩子被跳过/尚未启动）则兜底刷新
async function adoptLedgerSync() {
  try {
    const d = await api("/api/runs?limit=8");
    const r = (d.runs || []).find((x) => x.kind === "ledger" && x.status === "running");
    if (r) { attachRun(r.id, null, "ledger"); return; }
  } catch (e) { console.warn("[ledger] 查找台账同步任务失败，走兜底刷新", e); }
  refreshActiveView();
  setTimeout(refreshActiveView, 10000); // 同步可能刚启动，稍后再刷一次
}

async function stopCurrent() {
  if (!currentRun) return toast("当前无运行任务");
  try {
    const result = await api(`/api/runs/${currentRun}/stop`, { method: "POST" });
    if (!result.stopped) return toast("未能终止：任务可能已结束，请查看日志状态");
    setRunProgressNote("任务已终止");
    toast("任务已终止");
  } catch (e) { toast("终止失败：" + e.message, "error"); }
}

/* P1-4：刷新 / 换标签 / 换设备后接管仍在后台跑的运行。
   旧版 currentRun 只存在页面内存里，F5 之后日志区永久「等待运行…」、
   点「终止」还会说"当前无运行任务"（后端其实还在跑）。 */
async function adoptRunningOnBoot() {
  let sid = null;
  try { sid = sessionStorage.getItem("wb.runId"); } catch (_) { /* 忽略 */ }
  let r = null;
  try {
    const d = await api("/api/runs?limit=10");
    const list = d.runs || [];
    r = sid ? list.find((x) => x.id === sid && x.status === "running") : null;
    if (!r) r = list.find((x) => x.status === "running") || null;
  } catch (e) {
    console.warn("[run] 查询运行中任务失败", e);
    return;
  }
  if (!r) {
    try { sessionStorage.removeItem("wb.runId"); } catch (_) { /* 忽略 */ }
    return;
  }
  // ★ 2026-10-22（A01）：这里**不再** nav.click() 抢路由。
  // 之前直开 /#bench 刷新会被强制切到 /#run，用户以为深链失效。
  // 现在只恢复后台状态 + 右下角提示条，点「查看运行」才跳转。
  const kind = r.kind || "";
  if (kind === "pipeline" || kind === "daily") {
    const steps = kind === "daily" ? ["daily"] : (r.steps && r.steps.length ? r.steps : null);
    if (steps) buildRunProgress(steps);
  }
  attachRun(r.id, null, kind);
  showRunResumeBanner(r);
  toast("后台任务仍在运行；当前页面保持不变，点右下角「查看运行」再过去", "ok");
}

/* 右下角「后台任务」提示条：只提示，不抢路由（A01） */
function showRunResumeBanner(r) {
  let box = document.getElementById("run-resume-banner");
  if (!box) {
    box = document.createElement("div");
    box.id = "run-resume-banner";
    box.style.cssText = "position:fixed;left:50%;transform:translateX(-50%);bottom:18px;z-index:70;" +
      "display:flex;gap:10px;align-items:center;background:#141210;color:#f5f5f4;padding:9px 13px;" +
      "border-radius:10px;box-shadow:0 8px 24px rgba(28,25,23,.28);font-size:13px;max-width:92vw";
    document.body.appendChild(box);
  }
  box.innerHTML = '<span>后台有任务在运行：<b>' + esc(r.title || r.kind || r.id) + '</b></span>';
  const go = document.createElement("button");
  go.className = "btn sm";
  go.textContent = "查看运行";
  go.onclick = () => {
    const nav = document.querySelector('.nav-item[data-view="run"]');
    if (nav) nav.click();        // 只有用户点了才切页
    box.remove();
  };
  const close = document.createElement("button");
  close.className = "btn sm ghost";
  close.textContent = "先不看";
  close.onclick = () => box.remove();
  box.appendChild(go);
  box.appendChild(close);
}

/* ─────────── 一键运行 · 快速新增对标账号 ─────────── */
function toggleRunAdd(show) {
  const f = $("#run-add-form");
  if (!f) return;
  const open = show === undefined ? f.classList.contains("hidden") : !!show;
  f.classList.toggle("hidden", !open);
  if (open) { runAddValidate(); $("#run-add-name").focus(); }
}

/* 客户端校验：与服务端 ACCOUNT_FIELD_SCHEMA 同口径 */
function runAddValidate() {
  const nameEl = $("#run-add-name");
  const hint = $("#run-add-hint");
  if (!nameEl || !hint) return false;
  const v = (nameEl.value || "").trim();
  let msg = "", bad = false;
  if (!v) { msg = "账号名必填"; bad = true; }
  else if (!/^[a-z0-9_.]+$/.test(v)) { msg = "只能含小写字母、数字、下划线、点（与 TikTok 用户名一致）"; bad = true; }
  else if (("对标记录-" + v).length > 31) { msg = "过长（Excel sheet 名上限 31 字符）"; bad = true; }
  else if (((CFG && CFG.accounts) || []).includes(v)) { msg = "该账号已在列表中"; bad = true; }
  nameEl.classList.toggle("bad", bad);
  hint.innerHTML = bad
    ? `<span class="hint-bad">✗ ${esc(msg)}</span>`
    : '<span class="hint-ok">✓ 名称可用</span>';
  return !bad;
}

/* 面向地区 → 转写语言联动（与后端 config.REGION_LANG_MAP 同口径） */
const REGION_LANG_UI = { us: "en", uk: "en", eu: "en", de: "de", fr: "fr",
                         es: "es", it: "it", cn: "zh", "cn-hk": "zh",
                         "cn-tw": "zh", jp: "jp", kr: "kr" };
function syncAddLang() {
  const regionEl = $("#run-add-region");
  const langEl = $("#run-add-lang");
  if (!regionEl || !langEl) return;
  const r = regionEl.value;
  if (r && REGION_LANG_UI[r]) langEl.value = REGION_LANG_UI[r];
  else langEl.value = "";   // 未标地区 → 交后端自动推断
}

async function submitRunAdd() {
  const name = ($("#run-add-name").value || "").trim();
  const hint = $("#run-add-hint");
  if (!runAddValidate()) {
    if (hint && !hint.innerHTML) hint.innerHTML = '<span class="hint-bad">✗ 账号名必填</span>';
    return toast("请先修正账号名");
  }
  const region = ($("#run-add-region") || {}).value || "";
  const payload = {
    account: name,
    region: region,
    lang: ($("#run-add-lang") || {}).value || "",   // 空 = 让后端按 region 推断
    nickname: ($("#run-add-nickname").value || "").trim(),
    tags: ($("#run-add-tags").value || "").trim(),
  };
  try {
    await api("/api/accounts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    if (hint) hint.innerHTML = `<span class="hint-bad">✗ 保存失败：${esc(e.message)}</span>`;
    return toast("新增失败：" + e.message);
  }
  toast("已创建并选用 " + name);
  if (hint) hint.innerHTML = `<span class="hint-ok">✓ 已创建 ${esc(name)} 并在下拉中选用。下一步：点「开始运行」拉取该账号数据（建议同时勾选核心链）</span>`;
  try {
    CFG = await api("/api/config");           // 刷新账号清单
    fillAccountSelects();
    $("#run-account").value = name;           // 选用新账号
  } catch (e) { console.warn("[accounts] 账号清单刷新失败（下次 boot 自愈）", e); }
  $("#run-add-name").value = "";
  $("#run-add-nickname").value = "";
  $("#run-add-tags").value = "";
  refreshActiveView();
}

/* ─────────── 一键运行 · 人话进度面板 ─────────── */
const STEP_META = {
  s0_check:     { name: "检查预热",   desc: "校验账号配置与磁盘空间" },
  s1_download:  { name: "音频下载",   desc: "把视频声音下载到本地" },
  s2_scrape:    { name: "视频抓取",   desc: "采集播放/点赞/商品等数据" },
  s5_transcribe:{ name: "语音转文字", desc: "把音频转成文字稿" },
  s5b_products: { name: "商品提取",   desc: "从视频文案提取挂车商品" },
  s9_report:    { name: "生成报告",   desc: "汇总当日运行报告" },
  s3_4_sheet:   { name: "台账录入",   desc: "本软件内置执行" },
  s6_translate: { name: "中文翻译",   desc: "本软件内置执行" },
  s7_backfill:  { name: "台账回填",   desc: "本软件内置执行" },
};
let runProgress = null;          // {order:[key], state:{key:{status,note,startedAt}}}
let runProgressTimer = null;

function stepDisplayName(key) {
  const m = STEP_META[key];
  return m ? m.name : key;
}

function buildRunProgress(steps) {
  const order = (steps && steps.length ? steps : ["daily"]);
  runProgress = { order, state: {}, lines: [], errLines: [] };
  order.forEach((k) => {
    runProgress.state[k] = { status: "wait", note: "", startedAt: 0 };
  });
  const fa = $("#fail-analysis");
  if (fa) { fa.classList.add("hidden"); fa.innerHTML = ""; }
  const panel = $("#run-progress-panel");
  if (panel) panel.hidden = false;
  setRunProgressNote("");
  renderRunProgress();
  if (runProgressTimer) clearInterval(runProgressTimer);
  runProgressTimer = setInterval(() => {
    if (!runProgress) return clearInterval(runProgressTimer);
    const running = runProgress.order.filter((k) => runProgress.state[k].status === "running");
    if (!running.length) return;
    running.forEach((k) => {
      const s = runProgress.state[k];
      const sec = Math.round((Date.now() - s.startedAt) / 1000);
      const el2 = document.querySelector(`[data-rp-key="${k}"] .rp-time`);
      if (el2) el2.textContent = "已运行 " + sec + " 秒";
    });
  }, 1000);
}

function renderRunProgress() {
  const box = $("#run-progress");
  if (!box || !runProgress) return;
  const ico = { wait: "⏳", running: "🔄", ok: "✅", fail: "❌", skip: "⏭" };
  const cls = { wait: "gray", running: "blue", ok: "green", fail: "red", skip: "amber" };
  box.innerHTML = runProgress.order.map((k) => {
    const s = runProgress.state[k];
    const meta = STEP_META[k] || { name: k === "daily" ? "每日增量" : k, desc: "" };
    const note = s.note ? `<div class="rp-note">${esc(s.note)}</div>` : "";
    const time = s.status === "running" ? '<span class="rp-time"></span>' : "";
    return `<div class="rp-step ${s.status} ${cls[s.status] || "gray"}" data-rp-key="${esc(k)}">
      <span class="rp-ico">${ico[s.status] || "⏳"}</span>
      <div class="rp-body">
        <div class="rp-name">${esc(meta.name)} <span class="muted">${esc(meta.desc || "")}</span> ${time}</div>
        ${note}
      </div>
    </div>`;
  }).join("");
}

function setRunProgressNote(text) {
  const el2 = $("#run-progress-note");
  if (el2) el2.textContent = text || "";
}

function setRunStep(key, status, note) {
  if (!runProgress) return;
  let k = key;
  if (!runProgress.state[k]) {
    const base = key.replace(/_.*$/, "");
    k = runProgress.order.find((x) => x === key || x.startsWith(base)) || key;
    if (!runProgress.state[k]) runProgress.state[k] = { status: "wait", note: "", startedAt: 0 };
    if (!runProgress.order.includes(k)) runProgress.order.push(k);
  }
  const s = runProgress.state[k];
  s.status = status;
  if (note !== undefined) s.note = note;
  if (status === "running" && !s.startedAt) s.startedAt = Date.now();
  renderRunProgress();
  if (status === "fail") showFailAnalysis();
}

/* ─────────── 一键运行 · 对标账号分组 ─────────── */
let groupsData = null;        // {groups:[...]}
let grpSelected = null;       // 当前选中分组 id
// 使用捕获阶段，点击卡片内阻止冒泡的控件时也能记录所在泳道。
const grpDetail = $("#grp-detail");
if (grpDetail) grpDetail.addEventListener("click", (event) => {
  const lane = event.target.closest(".lane[data-gid]");
  if (lane) grpSelected = lane.dataset.gid;
}, true);
let grpFilter = "";           // 列筛选：""=全部 / "pending" / "error"（组索引 chips）
let grpSort = "name";         // name | joined
let grpNewOpen = false;
let grpSelSet = new Set();    // ② 勾选批量移动：被勾选的账号名集合
let lastOv = null;            // 总览最近一次数据（accounts + pending_translation）：分组看板健康度零新增请求
let laneMenuGid = null;      // 当前展开的列头「⋯」菜单所属组 id
let laneMenuEl = null;       // 列头菜单 DOM
let runConfirmGid = null;     // 「跑本组」确认态
let runConfirmEl = null;

/* 组健康度：异常（severity=error）/ 待译（音频>转写）/ 运行中 —— 全部取自 lastOv，无新请求 */
function laneHealth(mems) {
  const accs = (lastOv && lastOv.accounts) || [];
  let err = 0, pend = 0, running = 0;
  for (const m of mems || []) {
    const a = accs.find((x) => x.account === m.account);
    if (!a) continue;
    if (a.running ?? a.active) running++;   // 后端未升级时降级回 active
    else if (a.severity === "error") err++;
    if (a.media_type !== "image" && typeof a.audio_count === "number"
        && a.audio_count > 0 && (a.transcript_count || 0) < a.audio_count) pend++;
  }
  return { err, pend, running };
}
/* 列宽/高度/排序用的健康快照（供组索引 chips 与排序共用） */
function laneHealthOf(g) { return laneHealth(g.members || []); }
function laneSortRank(g) {
  const h = laneHealthOf(g);
  let r = h.err ? 0 : h.pend ? 1 : 2;
  if ((g.members || []).length === 0) r = 9;   // 空组永远垫底
  return r;
}

/* 分组区交互态守卫：60s 轮询要跳过整区重建，否则搜索面板/拖拽/右键菜单会被冲掉（P1-1） */
function grpBusy() {
  if (grpNewOpen || _grpQOpen) return true;
  if ($("#grp-q-panel") || _grpCtx) return true;
  if (grpSelSet.size) return true;
  if ($("#grp-detail .acard.dragging")) return true;
  const ae = document.activeElement;
  if (ae && typeof ae.closest === "function" && ae.closest("#grp-tabs, #grp-detail")) return true;
  return false;
}
let grpOperationBusy = false;
let grpReqSeq = 0;

async function loadGroups() {
  const seq = ++grpReqSeq;
  try {
    const d = await api("/api/groups");
    if (seq !== grpReqSeq) return;
    groupsData = d;
    if (!(d.groups || []).some((g) => g.id === grpSelected)) grpSelected = null;
    const names = new Set((d.groups || []).flatMap((g) => (g.members || []).map((m) => m.account)));
    grpSelSet.forEach((a) => { if (!names.has(a)) grpSelSet.delete(a); });
    if (benchGroupId) {
      const g = (d.groups || []).find((x) => x.id === benchGroupId);
      benchGroupAccounts = (g && g.members || []).map((m) => m.account);
    }
    renderGroupTabs();
    renderGroupDetail();
  } catch (e) {
    if (seq !== grpReqSeq) return;
    const box = $("#grp-tabs");
    if (box) box.innerHTML = `<span class="muted">分组加载失败：${esc(e.message)}</span>`;
  }
}

function renderGroupTabs() {
  const box = $("#grp-tabs");
  if (!box || !groupsData) return;
  const draft = $("#grp-new-name") ? $("#grp-new-name").value : "";
  const query = $("#grp-q") ? $("#grp-q").value : "";
  // 泳道模式下，顶部 tab 只保留「＋ 新建分组」入口；分组本身以泳道列呈现
  const createInput = grpNewOpen
    ? `<input id="grp-new-name" class="ra-inp" placeholder="分组名称"
         maxlength="20" onkeydown="if(event.key==='Enter')submitGroupCreate()" />` : "";
  box.innerHTML = `
    <span class="muted" style="margin-right:6px" title="大分类 ← 分组 ← 账号：拖卡片改组即随组换大类；点卡身看台账">拖卡片改组 · 点卡身看台账</span>
    <input id="grp-q" class="ra-inp" placeholder="🔍 搜索账号快速指派" style="width:180px"
           oninput="renderGroupSearchPanel()" onfocus="toggleGrpSearch(true)" />
    <span class="grp-new">
      ${createInput}
      <button class="btn ghost sm" onclick="${grpNewOpen ? "submitGroupCreate()" : "toggleGroupCreate(true)"}">${grpNewOpen ? "确定" : "＋ 新建分组"}</button>
      ${grpNewOpen ? `<button class="btn ghost sm" onclick="toggleGroupCreate(false)">取消</button>` : ""}
    </span>`;
  if ($("#grp-new-name")) $("#grp-new-name").value = draft;
  if ($("#grp-q")) $("#grp-q").value = query;
  renderGroupSearchPanel();
}

function toggleGroupCreate(show) {
  grpNewOpen = show === undefined ? !grpNewOpen : !!show;
  renderGroupTabs();
  if (grpNewOpen && $("#grp-new-name")) $("#grp-new-name").focus();
}

async function submitGroupCreate() {
  const i = $("#grp-new-name");
  const name = i ? i.value.trim() : "";
  if (!name) return toast("请输入分组名称");
  try {
    await api("/api/groups", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) });
    toast("分组已创建：" + name);
    grpNewOpen = false;
    await loadGroups();
    // 让新列滚入视野
    const el_ = document.getElementById(`lane-${_gid(name)}`);
    if (el_) el_.scrollIntoView({ behavior: "smooth", inline: "center", block: "nearest" });
  } catch (e) { toast("创建失败：" + e.message); }
}

function _gid(name) {
  const g = (groupsData && groupsData.groups || []).find((x) => x.name === name);
  return g ? g.id : "";
}

async function renameGroup(gid) {
  const g = (groupsData.groups || []).find((x) => x.id === gid);
  const cur = g ? g.name : "";
  const name = prompt("新的分组名称（≤20 字符）：", cur);
  if (name === null) return;
  if (!name.trim()) return toast("名称不能为空");
  try {
    await api(`/api/groups/${encodeURIComponent(gid)}`, { method: "PATCH",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) });
    toast("已重命名");
    await loadGroups();
  } catch (e) { toast("重命名失败：" + e.message); }
}

async function deleteGroup(gid) {
  const g = (groupsData.groups || []).find((x) => x.id === gid);
  const n = g ? g.count : 0;
  if (!confirm(`删除分组「${g ? g.name : gid}」？\n组内 ${n} 个账号将自动回到「默认分组」，账号本身不会被删除。`)) return;
  try {
    await api(`/api/groups/${encodeURIComponent(gid)}`, { method: "DELETE" });
    toast(`分组已删除，${n} 个账号已回到默认分组`);
    await loadGroups();
  } catch (e) { toast("删除失败：" + e.message); }
}

/* ── 泳道拖拽（HTML5 DnD）：拖账号卡片跨列 = 改组 ──
   撤销栈 grpUndoStack：每步 {account, from, to}，Ctrl+Z 反做。 */
let grpUndoStack = [];
function cssEsc(s) { return String(s).replace(/"/g, '\\"'); }

/* ── 大分类（顶级分类）：类别 ← 分组 ← 账号 ──
   类别由分组继承，账号不直接挂类别，"一个账号只归属一个分组"的既有约束不变。
   类别清单来自 /api/groups.categories（按 order 排序）；新增类别不需要改前端。 */
let catFilter = "";          // "" = 全部大类
let catNewFor = null;        // 正在内联新建分组的大类 id

function catsOf() {
  const cats = (groupsData && groupsData.categories) || [];
  return cats.length ? cats : null;      // null = 旧数据 / 测试桩：退化为单带
}
function catKeyOf(g) { return (g && g.category_id) || ""; }
function catNameOf(cid) {
  const c = (catsOf() || []).find((x) => x.id === cid);
  return c ? c.name : "";
}
/* 组排序（P2-12）：异常 > 待处理 > 正常 > 空组；同档按组内最近成功日倒序；默认组垫底 */
function sortGroups(gs, ovMap) {
  const recentOf = (g) => {
    let best = null;
    for (const m of g.members || []) {
      const o = ovMap.get(m.account);
      const d = o && o.last_success_date;
      if (d && (!best || d > best)) best = d;
    }
    return best;
  };
  return gs.slice().sort((a, b) => {
    if (!!a.builtin !== !!b.builtin) return a.builtin ? 1 : -1;
    return (laneSortRank(a) - laneSortRank(b))
      || ((recentOf(b) || "").localeCompare(recentOf(a) || ""))
      || a.name.localeCompare(b.name);
  });
}

/* 单列泳道 HTML（保持 id=lane-<gid> / data-gid，拖拽与旧测试依赖不变） */
function laneHtml(g, ovMap, ovAccs) {
  const mems = (g.members || []).slice().sort((a, b) => a.account.localeCompare(b.account));
  // P2-11：iOS Safari 不支持元素级 HTML5 拖拽 —— 触屏上直接关掉 draggable，文案只宣传「⇢ 移动」
  const coarse = typeof matchMedia === "function" && matchMedia("(pointer:coarse)").matches;
  const h = laneHealthOf(g);
  const badges = [];
  if (h.err) badges.push(`<span class="lane-bd err" title="组内有抓取/转写异常">异常 ${h.err}</span>`);
  if (h.pend) badges.push(`<span class="lane-bd amber" title="音频已下、转写未完成">待译 ${h.pend}</span>`);
  if (h.running) badges.push(`<span class="lane-bd blue" title="正在跑流水线">运行中 ${h.running}</span>`);
  const ops = g.builtin
    ? `<span class="lane-ops muted" title="内置分组">内置</span>`
    : `<span class="lane-ops">
         <button class="btn ghost sm lane-run" title="对整组跑流水线（近 7 天增量）"
                 onclick="toggleRunGroup('${esc(g.id)}', this)">跑本组</button>
         <button class="ico-btn lane-more" title="更多" aria-label="更多操作" onclick="toggleLaneMenu(event, '${esc(g.id)}')">⋯</button>
         <span class="lane-confirm" id="lrc-${esc(g.id)}" hidden></span>
       </span>`;
  const laneCls = `lane ${g.builtin ? "lane-builtin" : ""}`
    + (mems.length ? "" : " lane-ghost")
    + (grpFilter === "error" && !h.err ? " lane-hide" : "")
    + (grpFilter === "pending" && !h.err && !h.pend ? " lane-hide" : "");
  const cards = mems.map((m) => {
    const accLit = esc(JSON.stringify(m.account));
    const checked = grpSelSet.has(m.account) ? "checked" : "";
    const o = ovMap.get(m.account) || {};
    const st = (o.running ?? o.active) ? "run" : o.severity === "error" ? "err" : o.severity === "warn" ? "warn" : "ok";
    const meta = ovAccs.length ? (() => {
      if (o.media_type === "image") return "图文";
      if (o.last_success_date) {
        const d = new Date(o.last_success_date + "T00:00:00");
        const day = Math.max(0, Math.floor((Date.now() - d.getTime()) / 864e5));
        const upd = day === 0 ? "今日更新" : day === 1 ? "昨日更新" : `${day} 天前更新`;
        return `<span class="ac-meta">${o.scrape_items || 0} 视频 · ${upd}</span>`;
      }
      return `<span class="ac-meta muted">未采集</span>`;
    })() : "";
    // 迁移下拉按大类分组（optgroup），跨类迁移时先看清去向
    const cats = catsOf() || [];
    const moveOpts = cats.length
      ? cats.map((c) => {
        const items = (groupsData.groups || [])
          .filter((x) => x.id !== g.id && catKeyOf(x) === c.id)
          .map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("");
        return items ? `<optgroup label="${esc(c.name)}">${items}</optgroup>` : "";
      }).join("")
      : (groupsData.groups || []).filter((x) => x.id !== g.id)
        .map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("");
    const tip = `${m.account}${o.last_success_date ? " · 最近成功 " + o.last_success_date : " · 未采集"}`;
    return `<div class="acard st-${st} ${grpSelSet.has(m.account) ? "acard-sel" : ""}"
             draggable="${coarse ? "false" : "true"}" tabindex="0"
             title="${esc(tip)}"
             data-acc="${esc(m.account)}" data-from="${esc(g.id)}" data-cat="${esc(catKeyOf(g))}"
             ondragstart="cardStart(event, this)" ondragend="cardEnd(event, this)"
             oncontextmenu="cardCtx(event, ${accLit})"
             onclick="cardClick(event, this)" onkeydown="cardKey(event, ${accLit}, this)">
            <i class="ac-st" aria-hidden="true"></i>
            <label class="ac-sel" title="勾选批量移动" onmousedown="event.stopPropagation()" ondragstart="return false">
              <input type="checkbox" ${checked} aria-label="${esc("勾选 " + m.account)}" onchange="toggleGrpSel(this, ${accLit})" />
            </label>
            <div class="ac-main">
              <div class="ac-nm">${esc(m.account)}</div>
              ${meta}
            </div>
            <div class="ac-actions">
              <button class="ac-move" title="移到其它分组" aria-label="移到其它分组"
                      onmousedown="event.stopPropagation()" onclick="event.stopPropagation();cardMoveMenu(event, ${accLit})">⇢</button>
              <select class="ac-to" title="移到分组" onchange="moveAccountTo(${accLit}, this.value, this)">
                <option value="" disabled selected>移到…</option>
                ${moveOpts}
              </select>
            </div>
          </div>`;
  }).join("");
  return `<div class="${laneCls}"
      id="lane-${esc(g.id)}"
      data-gid="${esc(g.id)}"
      ondragover="laneOver(event, this)" ondragleave="laneLeave(event, this)" ondrop="laneDrop(event, this)">
    <div class="lane-head">
      <label class="lane-sel" title="全选/取消本列">
        <input type="checkbox" ${mems.length && mems.every((m) => grpSelSet.has(m.account)) ? "checked" : ""}
               onchange="grpAllCheck(this, ${esc(JSON.stringify(g.id))})" />
      </label>
      <span class="lane-name" title="${esc(g.name)}">${esc(g.name)}</span>
      <span class="lane-cnt">${mems.length}</span>
      ${badges.join("")}
      ${ops}
    </div>
    <div class="lane-body">
      ${mems.length ? cards : `<div class="lane-empty">${coarse ? "点卡身 ⇢ 移动到此列" : "拖账号到此列"}</div>`}
    </div>
  </div>`;
}

/* 大类筛选（顶级）：""=全部；只隐藏整带，不影响组级 chips 的 .lane-hide 语义 */
function setCatFilter(v) {
  catFilter = v || "";
  renderGroupDetail();
}

function renderGroupDetail() {
  const box = $("#grp-detail");
  if (!box || !groupsData) return;
  const groups = groupsData.groups || [];
  if (!groups.length) {
    box.innerHTML = `<div class="grp-empty">暂无分组，点上方「＋ 新建分组」创建</div>`;
    return;
  }
  const ovAccs = (lastOv && lastOv.accounts) || [];
  const ovMap = new Map(ovAccs.map((a) => [a.account, a]));
  const cats = catsOf();
  // 分带：有 categories 时按大类切；否则单带（旧数据 / 测试桩）
  let bands;
  if (cats) {
    bands = cats.map((c) => ({ cat: c, gs: groups.filter((g) => catKeyOf(g) === c.id) }));
    const known = new Set(cats.map((c) => c.id));
    const lost = groups.filter((g) => !known.has(catKeyOf(g)));
    if (lost.length) bands.push({ cat: { id: "", name: "未归属", desc: "类别已失效", builtin: true }, gs: lost });
  } else {
    bands = [{ cat: null, gs: groups.slice() }];
  }

  // 组索引 chips（P2-15）：全部 / 待处理 / 异常，点击只看问题列
  const all = groups.length;
  const nErr = groups.filter((g) => laneHealthOf(g).err).length;
  const nPend = groups.filter((g) => !laneHealthOf(g).err && laneHealthOf(g).pend).length;
  const chips = `<div class="lane-chips">
      <button class="chip-filter ${grpFilter === "" ? "on" : ""}" onclick="setLaneFilter('')">全部 ${all}</button>
      <button class="chip-filter ${grpFilter === "pending" ? "on" : ""}" onclick="setLaneFilter('pending')">待处理 ${nPend}</button>
      <button class="chip-filter ${grpFilter === "error" ? "on" : ""}" onclick="setLaneFilter('error')">异常 ${nErr}</button>
      ${grpSelSet.size ? `<span class="sel-hint" id="lane-sel-hint">已选 <b>${grpSelSet.size}</b> · <a href="javascript:void 0" onclick="clearGrpSel()">清除</a></span>` : `<span class="sel-hint" id="lane-sel-hint" hidden></span>`}
    </div>`;

  // 大类 chips + 新建大类入口（独立容器，避免与组索引 chips 混在一起）
  const totalAcc = groups.reduce((s, g) => s + ((g.members || []).length), 0);
  const catChips = cats ? `<div class="lane-cats">
      <button class="chip-cat ${catFilter === "" ? "on" : ""}" onclick="setCatFilter('')">全部 <b>${totalAcc}</b></button>
      ${cats.map((c) => {
        const n = (c.account_count != null) ? c.account_count
          : groups.filter((g) => catKeyOf(g) === c.id).reduce((s, g) => s + ((g.members || []).length), 0);
        return `<button class="chip-cat ${catFilter === c.id ? "on" : ""}" data-cat-chip="${esc(c.id)}"
                  title="${esc(c.desc || "")}" onclick="setCatFilter('${esc(c.id)}')">${esc(c.name)} <b>${n}</b></button>`;
      }).join("")}
      <button class="chip-cat add" onclick="addCategory()" title="新增一个大分类（用于横向扩展）">＋ 大类</button>
    </div>` : "";

  const bandHtml = bands.map(({ cat, gs }) => {
    const sorted = sortGroups(gs, ovMap);
    const accN = sorted.reduce((s, g) => s + ((g.members || []).length), 0);
    const hidden = !!catFilter && (!cat || cat.id !== catFilter);
    let head = "";
    if (cat) {
      const creating = catNewFor === cat.id;
      head = `<header class="cat-head">
        <span class="cat-nm">${esc(cat.name)}</span>
        <span class="cat-cnt">${sorted.length} 组 · ${accN} 账号</span>
        ${cat.desc ? `<span class="cat-desc" title="${esc(cat.desc)}">${esc(cat.desc)}</span>` : ""}
        <span class="cat-ops">
          <button class="btn ghost sm ico-add" title="在「${esc(cat.name)}」下新建分组"
                  aria-label="在「${esc(cat.name)}」下新建分组"
                  onclick="toggleCatCreate('${esc(cat.id)}')">＋</button>
          ${cat.builtin ? `<span class="muted" title="内置大类：不可改名/删除">内置</span>`
            : `<button class="ico-btn" title="大类设置" onclick="toggleCatMenu(event, '${esc(cat.id)}')">⋯</button>`}
        </span>
      </header>
      ${creating ? `<div class="cat-new" id="cat-new-${esc(cat.id)}">
        <input class="ra-inp" id="cat-new-name-${esc(cat.id)}" placeholder="分组名称（≤20 字）" maxlength="20"
               onkeydown="if(event.key==='Enter')submitCatCreate('${esc(cat.id)}');if(event.key==='Escape')toggleCatCreate('${esc(cat.id)}',false)" />
        <button class="btn primary sm" onclick="submitCatCreate('${esc(cat.id)}')">创建</button>
        <button class="btn ghost sm" onclick="toggleCatCreate('${esc(cat.id)}',false)">取消</button>
      </div>` : ""}`;
    }
    const lanes = sorted.length
      ? sorted.map((g) => laneHtml(g, ovMap, ovAccs)).join("")
      : `<div class="cat-empty">${cat ? `「${esc(cat.name)}」下还没有分组` : "暂无分组"} · 点上方「＋ 新建分组」，或把已有分组「移到大类」</div>`;
    return `<section class="cat-band${hidden ? " cat-hide" : ""}" data-cat="${esc(cat ? cat.id : "")}">
      ${head}
      <div class="lanes">${lanes}</div>
    </section>`;
  }).join("");

  // P1-1：60s 轮询整块重建会让 .lanes 横滚归零（滚到第 8 列看数据，一分钟后就跳回最左）。
  // 按 data-cat 记录每条带的 scrollLeft，重建后写回。
  const lanesScroll = {};
  box.querySelectorAll(".cat-band").forEach((b) => {
    const l = b.querySelector(".lanes");
    if (l && l.scrollLeft) lanesScroll[b.getAttribute("data-cat") || ""] = l.scrollLeft;
  });
  box.innerHTML = `${catChips}${chips}<div class="cat-bands">${bandHtml}</div>`;
  box.querySelectorAll(".cat-band").forEach((b) => {
    const l = b.querySelector(".lanes");
    const k = b.getAttribute("data-cat") || "";
    if (l && lanesScroll[k]) l.scrollLeft = lanesScroll[k];
  });
  updateGrpSelUI();
}

/* ── 大类下的分组创建 / 大类自身的增删改 / 分组换大类 ── */
function toggleCatCreate(cid, show) {
  const next = show === undefined ? (catNewFor !== cid ? cid : null) : (show ? cid : null);
  catNewFor = next;
  renderGroupDetail();
  const i = document.getElementById(`cat-new-name-${cid}`);
  if (i) i.focus();
}

async function submitCatCreate(cid) {
  const i = document.getElementById(`cat-new-name-${cid}`);
  const name = i ? i.value.trim() : "";
  if (!name) return toast("请输入分组名称");
  try {
    await api("/api/groups", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, category_id: cid }) });
    toast(`分组已创建：${name}（${catNameOf(cid)}）`);
    catNewFor = null;
    await loadGroups();
  } catch (e) { toast("创建失败：" + e.message); }
}

async function addCategory() {
  const name = prompt("新大分类名称（≤12 字），例如「合作账号」「内部测试」：", "");
  if (name === null) return;
  if (!name.trim()) return toast("名称不能为空");
  try {
    await api("/api/categories", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }) });
    toast("大分类已创建：" + name.trim());
    await loadGroups();
  } catch (e) { toast("创建失败：" + e.message); }
}

function toggleCatMenu(ev, cid) {
  ev.stopPropagation();
  closeLaneMenu();
  const c = (catsOf() || []).find((x) => x.id === cid);
  if (!c) return;
  const menu = document.createElement("div");
  menu.className = "grp-ctx";
  menu.innerHTML = [
    `<button class="ctx-i" onclick="renameCategory('${esc(cid)}');closeLaneMenu()">✎ 重命名大类</button>`,
    `<button class="ctx-i" onclick="editCategoryDesc('${esc(cid)}');closeLaneMenu()">✎ 改说明</button>`,
    `<button class="ctx-i danger" onclick="deleteCategory('${esc(cid)}');closeLaneMenu()">🗑 删除大类</button>`,
  ].join("");
  document.body.appendChild(menu);
  const r = ev.currentTarget.getBoundingClientRect();
  menu.style.left = Math.min(r.right - 180, window.innerWidth - 190) + "px";
  menu.style.top = r.bottom + 4 + "px";
  laneMenuEl = menu;
  laneMenuGid = "__cat__" + cid;
}

async function renameCategory(cid) {
  const c = (catsOf() || []).find((x) => x.id === cid);
  const name = prompt("新的大类名称（≤12 字）：", c ? c.name : "");
  if (name === null || !name.trim()) return;
  try {
    await api(`/api/categories/${encodeURIComponent(cid)}`, { method: "PATCH",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: name.trim() }) });
    toast("已重命名大类");
    await loadGroups();
  } catch (e) { toast("重命名失败：" + e.message); }
}

async function editCategoryDesc(cid) {
  const c = (catsOf() || []).find((x) => x.id === cid);
  const desc = prompt("大类说明（≤60 字）：", c ? (c.desc || "") : "");
  if (desc === null) return;
  try {
    await api(`/api/categories/${encodeURIComponent(cid)}`, { method: "PATCH",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ desc: desc.trim() }) });
    await loadGroups();
  } catch (e) { toast("保存失败：" + e.message); }
}

async function deleteCategory(cid) {
  const c = (catsOf() || []).find((x) => x.id === cid);
  if (!confirm(`删除大分类「${c ? c.name : cid}」？\n仅当其下没有分组时才可删除；分组本身可用「移到大类」搬走。`)) return;
  try {
    await api(`/api/categories/${encodeURIComponent(cid)}`, { method: "DELETE" });
    toast("大分类已删除");
    if (catFilter === cid) catFilter = "";
    await loadGroups();
  } catch (e) { toast("删除失败：" + e.message); }
}

/* 把整组挪到另一个大类（组内账号随组迁移，账号本身不动） */
async function moveGroupCategory(gid) {
  const g = (groupsData.groups || []).find((x) => x.id === gid);
  if (!g) return;
  const cats = catsOf() || [];
  const targets = cats.filter((c) => c.id !== catKeyOf(g));
  if (!targets.length) return toast("没有其它大类");
  const lines = targets.map((c, i) => `${i + 1}. ${c.name}`).join("\n");
  const ans = prompt(`把「${g.name}」移到大类（输入序号）：\n${lines}`, "1");
  if (ans === null) return;
  const pick = targets[Number(String(ans).trim()) - 1];
  if (!pick) return toast("序号无效");
  try {
    await api(`/api/groups/${encodeURIComponent(gid)}/category`, { method: "PATCH",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ category_id: pick.id }) });
    toast(`「${g.name}」已移到「${pick.name}」`);
    await loadGroups();
  } catch (e) { toast("迁移失败：" + e.message); }
}

/* 组索引筛选（P2-15）：""=全部 / "pending"=待处理 / "error"=异常 —— 复用既有 grpFilter */
function setLaneFilter(v) {
  grpFilter = v;
  renderGroupDetail();
}
/* 卡身点击 = 打开账号台账抽屉（P1-7）；勾选框/⇢/右键已有独立处理不触发 */
function cardClick(ev, el) {
  const t = ev && ev.target;
  if (t && typeof t.closest === "function" && t.closest(".ac-sel, .ac-actions")) return;
  openAccountLedger(el.dataset.acc);
}
/* 键盘：Enter 开台账，M 打开移动菜单，Space 勾选（P2-14） */
function cardKey(ev, acc, el) {
  if (ev.key === "Enter") { ev.preventDefault(); cardClick(ev, el); }
  else if (ev.key === " ") { ev.preventDefault(); toggleGrpSel(el.querySelector(".ac-sel input"), acc); }
  else if (ev.key.toLowerCase() === "m") { ev.preventDefault(); cardMoveMenu(ev, acc); }
}
/* ⇢ 移动菜单（P0-2：常驻下拉降级为 hover 按钮）：复用右键菜单的组列表逻辑 */
function cardMoveMenu(ev, account) {
  const src = fromGidOf(account);
  const opts = ((groupsData && groupsData.groups) || []).filter((x) => x.id !== src);
  const menu = document.createElement("div");
  menu.className = "grp-ctx";
  const cats = catsOf() || [];
  const grouped = cats.length
    ? cats.map((c) => {
      const items = opts.filter((x) => catKeyOf(x) === c.id);
      if (!items.length) return "";
      return `<div class="ctx-h">${esc(c.name)}</div>`
        + items.map((x) => `<button class="ctx-i" onclick="moveAccountTo(${esc(JSON.stringify(account))}, ${esc(JSON.stringify(x.id))});closeGrpCtx()">→ ${esc(x.name)}</button>`).join("");
    }).join("")
    : opts.map((x) => `<button class="ctx-i" onclick="moveAccountTo(${esc(JSON.stringify(account))}, ${esc(JSON.stringify(x.id))});closeGrpCtx()">→ ${esc(x.name)}</button>`).join("");
  menu.innerHTML = `<div class="ctx-h">将 ${esc(account)} 移到</div>` + grouped +
    `<button class="ctx-i ghost" onclick="closeGrpCtx()">取消</button>`;
  document.body.appendChild(menu);
  const p = ev && ev.clientX ? { x: ev.clientX, y: ev.clientY }
    : { x: window.innerWidth / 2, y: window.innerHeight / 2 };
  menu.style.left = Math.min(p.x, window.innerWidth - 200) + "px";
  menu.style.top = Math.min(p.y, window.innerHeight - opts.length * 32 - 40) + "px";
  _grpCtx = menu;
}

/* 组头「⋯」菜单（P2-10：收 4 图标为 1 菜单） */
function toggleLaneMenu(ev, gid) {
  ev.stopPropagation();
  if (laneMenuEl && laneMenuGid === gid) { closeLaneMenu(); return; }
  closeLaneMenu();
  const g = (groupsData.groups || []).find((x) => x.id === gid);
  const menu = document.createElement("div");
  menu.className = "grp-ctx lane-menu";
  menu.innerHTML = [
    `<button class="ctx-i" onclick="toggleGroupMetrics('${esc(gid)}');closeLaneMenu()">📊 组内指标对比</button>`,
    `<button class="ctx-i" onclick="openGroupBench('${esc(gid)}');closeLaneMenu()">⚖ 组内对标分析</button>`,
    `<button class="ctx-i" onclick="renameGroup('${esc(gid)}');closeLaneMenu()">✎ 重命名</button>`,
    `<button class="ctx-i" onclick="moveGroupCategory('${esc(gid)}');closeLaneMenu()">🏷 移到大类…</button>`,
    `<button class="ctx-i danger" onclick="deleteGroup('${esc(gid)}');closeLaneMenu()">🗑 删除分组（${(g && g.count) || 0} 个号回默认）</button>`,
  ].join("");
  document.body.appendChild(menu);
  const r = ev.currentTarget.getBoundingClientRect();
  menu.style.left = Math.min(r.right - 190, window.innerWidth - 200) + "px";
  menu.style.top = r.bottom + 4 + "px";
  laneMenuEl = menu;
  laneMenuGid = gid;
}
function closeLaneMenu() {
  if (laneMenuEl) { laneMenuEl.remove(); laneMenuEl = null; laneMenuGid = null; }
}
if (typeof document !== "undefined") document.addEventListener("click", (e) => {
  if (laneMenuEl && !laneMenuEl.contains(e.target) && !e.target.closest(".lane-more")) closeLaneMenu();
});

/* 组头「跑本组」（P1-6）：确认条列出账号 → 提交 /api/run（近 7 天、默认本地步骤） */
function toggleRunGroup(gid, btn) {
  if (runConfirmEl && runConfirmGid === gid) { doRunGroup(gid); return; }
  closeRunGroup();
  const g = (groupsData.groups || []).find((x) => x.id === gid);
  const names = ((g && g.members) || []).map((m) => m.account);
  if (!names.length) return toast("该组无账号");
  const c = document.createElement("span");
  c.className = "lane-confirm";
  c.hidden = false;
  c.innerHTML = `跑本组近 7 天（${names.length} 号）？
    <button class="btn primary sm" onclick="doRunGroup('${esc(gid)}')">开始</button>
    <button class="btn ghost sm" onclick="closeRunGroup()">取消</button>`;
  c.id = `lrc-${esc(gid)}`;
  document.getElementById(c.id).outerHTML = c.outerHTML;
  runConfirmEl = document.getElementById(c.id);
  runConfirmGid = gid;
  runConfirmEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
function closeRunGroup() {
  if (runConfirmEl) { runConfirmEl.remove(); runConfirmEl = null; runConfirmGid = null; }
}
async function doRunGroup(gid) {
  closeRunGroup();
  const g = (groupsData.groups || []).find((x) => x.id === gid);
  const accounts = ((g && g.members) || []).map((m) => m.account);
  if (!accounts.length) return toast("该组无账号");
  const payload = {
    kind: "pipeline",
    accounts,
    date_from: today(-7),
    date_to: today(),
    steps: (CFG && CFG.local_steps) || ["s1_download", "s2_scrape", "s5_transcribe", "s5b_products", "s9_report"],
    lang: $("#run-lang") && $("#run-lang").value ? $("#run-lang").value : "en",
    backend: $("#run-backend") && $("#run-backend").value ? $("#run-backend").value : "openai",
  };
  try {
    const run = await api("/api/run", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    toast(`已启动「${g.name}」：${accounts.length} 个账号顺序串行（近 7 天增量）`);
    // 切到「一键运行」视图看进度
    $$(".nav-item").forEach((x) => x.classList.remove("active"));
    $$(".view").forEach((v) => v.classList.remove("active"));
    const nav = document.querySelector('.nav-item[data-view="run"]');
    if (nav) nav.classList.add("active");
    const rv = $("#view-run");
    if (rv) rv.classList.add("active");
    buildRunProgress(payload.steps);
    attachRun(run.id, run.agent_pending_note, "pipeline");
  } catch (e) {
    toast("启动失败：" + e.message);
  }
}

/* ── 组内指标：全宽弹窗（2026-10-19 改版） ──
   旧版把 9 列表格塞进 220px 的泳道里，横竖双滚动、账号列只剩 4 个字，实际不可读；
   改为复用 #modal（可用宽度约 1180px），并在表格上方给出组级 KPI 与健康摘要。 */
let gmState = { gid: null, data: null, sort: "total_plays", dir: "desc" };

async function toggleGroupMetrics(gid) {
  const g = (groupsData.groups || []).find((x) => x.id === gid);
  const n = ((g && g.members) || []).length;
  openModal(`组内指标 · ${g ? g.name : gid}`, `${n} 个账号`, null);
  gmState = { gid, data: null, sort: "total_plays", dir: "desc" };
  const holder = $("#modal-content");
  try {
    const d = await api(`/api/groups/${encodeURIComponent(gid)}/metrics`);
    if (!gmState || gmState.gid !== gid) return;   // 期间切换了别的组，丢弃旧响应
    gmState.data = d;
    holder.innerHTML = groupMetricsHtml(d);
    gmEnhance();
  } catch (e) {
    holder.innerHTML = `<div class="chk-item error">指标加载失败：${esc(e.message)}</div>`;
  }
}

/* 表头点击排序：同列再点反向；切到新列时，数字默认降序、文本默认升序 */
function gmSortBy(key) {
  if (!gmState || !gmState.data) return;
  const numeric = key !== "account" && key !== "last_pub";
  if (gmState.sort === key) gmState.dir = gmState.dir === "asc" ? "desc" : "asc";
  else { gmState.sort = key; gmState.dir = numeric ? "desc" : "asc"; }
  const holder = $("#modal-content");
  holder.innerHTML = groupMetricsHtml(gmState.data);
  gmEnhance();
  const th = holder.querySelector(`[data-gm-sort="${key}"]`);
  if (th) th.focus();
}

/* 分组指标表的表现层增强：列宽可拖拽（弹窗重绘后重新挂） */
function gmEnhance() {
  const t = document.querySelector("#modal-content .gm-tbl2");
  if (t) TKV.resizable(t, "gm-metrics", { min: 58 });
}

function gmRunGroup() {
  const gid = gmState && gmState.gid;
  if (!gid) return;
  const g = (groupsData.groups || []).find((x) => x.id === gid);
  const n = ((g && g.members) || []).length;
  if (!n) return toast("该组无账号");
  if (!confirm(`对「${g.name}」的 ${n} 个账号跑近 7 天增量流水线？`)) return;
  closeModal();
  doRunGroup(gid);
}

function gmOpenBench() {
  const gid = gmState && gmState.gid;
  if (!gid) return;
  closeModal();
  openGroupBench(gid);
}

function groupMetricsHtml(d) {
  const accs = d.accounts || [];
  const n = accs.length;
  const sum = (k) => accs.reduce((s, a) => s + (Number(a[k]) || 0), 0);
  const totPlays = sum("total_plays");
  const avgPlays = n ? Math.round(totPlays / n) : 0;
  const totViral = sum("viral");
  const avgViral = n ? totViral / n : 0;
  const avgCov = n ? Math.round(sum("product_coverage") / n) : 0;
  const avgVideos = n ? Math.round(sum("videos") / n) : 0;
  const avgRecent = n ? Math.round(sum("recent7d") / n) : 0;

  // 健康摘要：只给计数，名单放 title —— 旧版把账号名全列出来，把表格顶下去 3 行
  const low = d.low_value || [];
  const stale = d.stale || [];
  const otherStale = stale.filter((a) => !low.includes(a));
  const unknown = accs.filter((a) => !a.videos || a.stale_days == null);
  const hs = [];
  if (d.empty) hs.push(`<span class="gm-h red">空组：无账号</span>`);
  if (low.length) hs.push(`<span class="gm-h amber" title="${esc(low.join("、"))}">低价值 ${low.length}</span>`);
  if (otherStale.length) hs.push(`<span class="gm-h amber" title="${esc(otherStale.join("、"))}">久未更新 ${otherStale.length}</span>`);
  if (unknown.length) hs.push(`<span class="gm-h amber" title="${esc(unknown.map((a) => a.account).join("、"))}">数据不足 ${unknown.length}</span>`);
  const health = hs.length ? hs.join(" ")
    : `<span class="gm-h green">组健康：无空组 / 低价值 / 久未更新</span>`;

  // 排序（表头可点）
  const key = (gmState && gmState.sort) || "total_plays";
  const dir = (gmState && gmState.dir) === "asc" ? 1 : -1;
  const sorted = accs.slice().sort((a, b) => {
    if (key === "account") return dir * a.account.localeCompare(b.account);
    if (key === "last_pub") return dir * String(a.last_pub || "").localeCompare(String(b.last_pub || ""));
    return dir * ((Number(a[key]) || 0) - (Number(b[key]) || 0));
  });
  const th = (k, label, cls = "rt") => {
    const on = key === k;
    const arrow = on ? (dir === 1 ? " ↑" : " ↓") : "";
    const aria = on ? (dir === 1 ? "ascending" : "descending") : "none";
    return `<th class="${cls} gm-th" data-gm-sort="${k}" tabindex="0" aria-sort="${aria}"
      title="按${label}排序" onclick="gmSortBy('${k}')"
      onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();gmSortBy('${k}')}">${label}${arrow}</th>`;
  };

  const rows = sorted.map((a) => {
    const tags = (a.tags || []).map((t) =>
      t === "无数据" ? `<span class="gm-tg t-red">${t}</span>`
      : t === "无爆款" ? `<span class="gm-tg t-amber">${t}</span>`
      : t.includes("未更新") ? `<span class="gm-tg t-amber">${t}</span>`
      : `<span class="gm-tg t-green">${t}</span>`).join(" ");
    return `<tr class="gm-row" onclick="openAccountLedger(${esc(JSON.stringify(a.account))})"
          title="打开 ${esc(a.account)} 的台账">
      <td class="acc">${esc(a.account)}</td>
      <td class="rt">${a.videos}</td>
      <td class="rt">${fmtNum(a.total_plays)}</td>
      <td class="rt">${a.avg_plays}</td>
      <td class="rt ${a.viral ? "" : "bad"}">${a.viral}</td>
      <td class="rt">${a.recent7d}</td>
      <td class="rt" title="${esc(a.last_pub || "无发布记录")}">${a.last_pub ? a.last_pub.slice(5, 10) : "—"}</td>
      <td class="rt">${a.product_coverage}%</td>
      <td class="gm-tags">${tags || `<span class="muted">—</span>`}</td>
    </tr>`;
  }).join("");

  const avgRow = n ? `<tr class="gm-avg">
      <td class="acc">组均 / 合计</td>
      <td class="rt">${avgVideos}</td>
      <td class="rt">${fmtNum(totPlays)}</td>
      <td class="rt">${avgPlays}</td>
      <td class="rt">${avgViral.toFixed(1)}</td>
      <td class="rt">${avgRecent}</td>
      <td class="rt">—</td>
      <td class="rt">${avgCov}%</td>
      <td class="gm-tags"><span class="muted">${n} 个账号</span></td>
    </tr>` : "";

  return `<div class="kpi-strip gm-kpis">
      <div class="kpi"><div class="kpi-l">组总播放</div><div class="kpi-v">${fmtNum(totPlays)}</div><div class="kpi-cap">${n} 个账号合计</div></div>
      <div class="kpi"><div class="kpi-l">均播放 / 号</div><div class="kpi-v">${fmtNum(avgPlays)}</div><div class="kpi-cap">横向对照基准</div></div>
      <div class="kpi"><div class="kpi-l">爆款（≥1 万）</div><div class="kpi-v">${totViral}<span class="u">条</span></div><div class="kpi-cap">组均 ${avgViral.toFixed(1)} 条</div></div>
      <div class="kpi ${hs.length ? "warn" : ""}"><div class="kpi-l">需处理</div><div class="kpi-v">${low.length + otherStale.length + unknown.length}<span class="u">个</span></div><div class="kpi-cap">低价值 / 久未更新 / 数据不足</div></div>
    </div>
    <div class="gm-healthline">${health}</div>
    <div class="table-wrap gm-tbl-wrap">
      <table class="tbl gm-tbl2"><thead><tr>
        ${th("account", "账号", "")}${th("videos", "视频")}${th("total_plays", "总播放")}${th("avg_plays", "均播放")}
        ${th("viral", "爆款(≥1万)")}${th("recent7d", "7天更新")}${th("last_pub", "最近发布")}
        ${th("product_coverage", "挂车率")}<th>健康标签</th>
      </tr></thead>
      <tbody>${avgRow}${rows || `<tr><td colspan="9" class="muted">该组无账号，拖一个账号进来或换个组</td></tr>`}</tbody></table>
    </div>
    <div class="gm-foot">
      <button class="btn primary accent" onclick="gmRunGroup()">跑本组（近 7 天）</button>
      <button class="btn" onclick="gmOpenBench()">组内对标分析</button>
      <span class="muted">点任意行 → 打开该账号台账；点表头可排序</span>
    </div>`;
}

async function openGroupBench(gid) {
  const d = groupsData;
  const g = (d && d.groups || []).find((x) => x.id === gid);
  const mems = (g && g.members || []).map((m) => m.account);
  if (!mems.length) return toast("该组无账号，无法做组内对标");
  // 切到 bench 并带组内账号子集
  $$(".nav-item").forEach((x) => x.classList.remove("active"));
  $$(".view").forEach((v) => v.classList.remove("active"));
  const nav = document.querySelector(`.nav-item[data-view="bench"]`);
  if (nav) nav.classList.add("active");
  $("#view-bench").classList.add("active");
  benchGroupAccounts = mems;   // loadBench 读取
  benchGroupId = gid;
  $("#bench-account").value = "";
  loadBench(true);
  toast(`已切到组内对标：${g.name}（${mems.length} 账号）`);
}

function cardStart(ev, el) {
  ev.dataTransfer.effectAllowed = "move";
  ev.dataTransfer.setData("text/plain", el.dataset.acc);
  el.classList.add("dragging");
}
function cardEnd(ev, el) { el.classList.remove("dragging"); }
function laneOver(ev, lane) {
  ev.preventDefault();
  ev.dataTransfer.dropEffect = "move";
  lane.classList.add("over");
}
function laneLeave(ev, lane) {
  // 只在移出本列（非进入子卡片）时去掉高亮
  if (!lane.contains(ev.relatedTarget)) lane.classList.remove("over");
}
async function laneDrop(ev, lane) {
  ev.preventDefault();
  lane.classList.remove("over");
  const acc = ev.dataTransfer.getData("text/plain");
  // 从被拖卡片取原列（dragstart 时 dataset.from），而非 drop 目标（目标恒等于 to）
  const card = document.querySelector?.(`.acard[data-acc="${cssEsc(acc)}"]`) || {};
  const from = card.dataset ? (card.dataset.from || "") : "";
  const to = lane.dataset.gid;
  if (!acc || !to || from === to) return;
  await _moveAccounts([acc], to);
}
async function grpUndo() {
  if (grpOperationBusy) return;
  const step = grpUndoStack.pop();
  if (!step) return;
  grpOperationBusy = true;
  const failed = [];
  let error = "";
  try {
    for (const s of (step.batch || [step]).slice().reverse()) {
      try {
        await api(`/api/groups/${encodeURIComponent(s.from)}/members`, { method: "POST",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ account: s.account }) });
      } catch (e) { failed.push(s); error = e.message; }
    }
    if (failed.length) grpUndoStack.push({ batch: failed.reverse() });
    await loadGroups();
    toast(failed.length ? `撤销失败 ${failed.length} 项，可重试：${error}` : "改组已撤销");
  } finally {
    grpOperationBusy = false;
  }
}

/* ───────── 快速改组（除拖拽外）：①卡片移到… ②勾选批量 ③右键菜单 ④搜索指派 ───────── */
function _grpName(gid) {
  const g = (groupsData && groupsData.groups || []).find((x) => x.id === gid);
  return gid === "default" ? "默认分组" : (g ? g.name : gid);
}
function fromGidOf(account) {
  for (const g of (groupsData && groupsData.groups || [])) {
    if ((g.members || []).some((m) => m.account === account)) return g.id;
  }
  return "default";
}

// 核心：把若干账号挪到目标组（共用所有入口），整批一步记撤销栈
async function _moveAccounts(accounts, to) {
  if (grpOperationBusy) return;
  grpOperationBusy = true;
  const fromMap = {};
  (groupsData && groupsData.groups || []).forEach((g) =>
    (g.members || []).forEach((m) => { fromMap[m.account] = g.id; }));
  const toDo = [...new Set(accounts)].filter((a) => fromMap[a] && fromMap[a] !== to);
  const moved = [];
  let error = "";
  try {
    for (const acc of toDo) {
      await api(`/api/groups/${encodeURIComponent(to)}/members`, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ account: acc }) });
      moved.push({ account: acc, from: fromMap[acc], to });
    }
  } catch (e) { error = e.message; }
  try {
    if (moved.length) grpUndoStack.push({ batch: moved });
    moved.forEach((s) => grpSelSet.delete(s.account));
    accounts.filter((a) => fromMap[a] === to).forEach((a) => grpSelSet.delete(a));
    await loadGroups();
    toast(error ? `改组失败：${error}；已移动 ${moved.length} 项（可 Ctrl+Z 撤销）`
      : `${moved.length} 个账号 → ${_grpName(to)}${moved.length ? "（Ctrl+Z 撤销）" : ""}`);
  } finally { grpOperationBusy = false; }
}

// ① 卡片「移到…」下拉
async function moveAccountTo(account, to, selEl) {
  if (!to || !account) return;
  await _moveAccounts([account], to);
  if (selEl) selEl.value = "";
}

// ② 勾选单个卡片
function toggleGrpSel(cb, account) {
  grpSelSet.has(account) ? grpSelSet.delete(account) : grpSelSet.add(account);
  const card = cb.closest(".acard");
  if (card) card.classList.toggle("acard-sel", grpSelSet.has(account));
  updateGrpSelUI();
}
// ② 整列全选（lane-head 复选框）
function grpAllCheck(cb, gid) {
  const g = (groupsData && groupsData.groups || []).find((x) => x.id === gid);
  const names = (g && g.members || []).map((m) => m.account);
  if (cb.checked) names.forEach((a) => grpSelSet.add(a));
  else names.forEach((a) => grpSelSet.delete(a));
  updateGrpSelUI();
}
function clearGrpSel() { grpSelSet.clear(); updateGrpSelUI(); }
// ② 批量移入目标组
async function batchMove() {
  const to = $("#grp-batch-to") ? $("#grp-batch-to").value : "";
  if (!to) return toast("先选目标分组");
  await _moveAccounts([...grpSelSet], to);
}
// ② 刷新勾选 UI：轻量更新——只同步批量条显隐与卡片选中态，不整区重渲染
// （整区重渲染会冲掉拖拽态/勾选焦点，且勾选本就该即时响应）
function updateGrpSelUI() {
  const box = $("#grp-detail");
  if (!box) return;
  // 卡片选中态
  box.querySelectorAll(".acard").forEach((c) => {
    c.classList.toggle("acard-sel", grpSelSet.has(c.dataset.acc));
    const cb = c.querySelector(".ac-sel input");
    if (cb) cb.checked = grpSelSet.has(c.dataset.acc);
  });
  // 列头全选框（tri-state）
  box.querySelectorAll(".lane").forEach((lane) => {
    const mems = [...(lane.querySelectorAll(".acard") || [])].map((c) => c.dataset.acc);
    const cb = lane.querySelector(".lane-sel input");
    if (!cb) return;
    const selN = mems.filter((a) => grpSelSet.has(a)).length;
    cb.checked = mems.length > 0 && selN === mems.length;
    cb.indeterminate = selN > 0 && selN < mems.length;
  });
  // 批量条显隐（P1-8：挂 body 固定视口底部，不再插入文档流 → 勾选零跳版）
  let bar = document.querySelector(".grp-batchbar");
  const groups = groupsData.groups || [];
  if (grpSelSet.size > 0) {
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "grp-batchbar";
      document.body.appendChild(bar);
    }
    bar.innerHTML = `
      <span>已选 <b>${grpSelSet.size}</b> 个账号</span>
      <select id="grp-batch-to" onchange="">
        ${groups.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("")}
      </select>
      <button class="btn primary sm" onclick="batchMove()">批量移入</button>
      <button class="btn ghost sm" onclick="clearGrpSel()">取消</button>
      <span class="muted sm">整批一步撤销：Ctrl+Z</span>`;
  } else if (bar) {
    bar.remove();
  }
  // 组索引 chips 的「已选 N」常驻提示（P2-14）：随勾选即时显隐
  const hint = document.getElementById("lane-sel-hint");
  if (hint) {
    if (grpSelSet.size) {
      hint.hidden = false;
      hint.innerHTML = `已选 <b>${grpSelSet.size}</b> · <a href="javascript:void 0" onclick="clearGrpSel()">清除</a>`;
    } else {
      hint.hidden = true;
      hint.innerHTML = "";
    }
  }
}

// ③ 右键菜单：列出所有其它组，点一下即移
let _grpCtx = null;
function cardCtx(ev, account) {
  ev.preventDefault();
  closeGrpCtx();
  const src = fromGidOf(account);
  const opts = (groupsData && groupsData.groups || []).filter((x) => x.id !== src);
  const menu = document.createElement("div");
  menu.className = "grp-ctx";
  menu.innerHTML = `<div class="ctx-h">将 ${esc(account)} 移到</div>` +
    opts.map((x) => `<button class="ctx-i" onclick="moveAccountTo(${esc(JSON.stringify(account))}, ${esc(JSON.stringify(x.id))});closeGrpCtx()">→ ${esc(x.name)}</button>`).join("") +
    `<button class="ctx-i ghost" onclick="closeGrpCtx()">取消</button>`;
  document.body.appendChild(menu);
  menu.style.left = Math.min(ev.clientX, window.innerWidth - 200) + "px";
  menu.style.top = Math.min(ev.clientY, window.innerHeight - opts.length * 32 - 40) + "px";
  _grpCtx = menu;
}
function closeGrpCtx() { if (_grpCtx) { _grpCtx.remove(); _grpCtx = null; } }

// ④ 搜索 + 快速指派：输入账号名模糊匹配 → 出候选 → 选目标组一次指派
let _grpQOpen = false;
function toggleGrpSearch(open) {
  _grpQOpen = open === undefined ? !_grpQOpen : !!open;
  const i = $("#grp-q");
  if (i && _grpQOpen && document.activeElement !== i) i.focus();
  renderGroupSearchPanel();
}
function renderGroupSearchPanel() {
  const old = $("#grp-q-panel");
  if (old) old.remove();
  if (!_grpQOpen) return;
  const inp = $("#grp-q");
  const q = (inp && inp.value || "").trim().toLowerCase();
  if (!q) return;
  const all = Object.fromEntries(
    (groupsData && groupsData.groups || []).flatMap((g) => (g.members || []).map((m) => [m.account, g.id])));
  const matches = q
    ? Object.keys(all).filter((a) => a.toLowerCase().includes(q)).slice(0, 20)
    : [];
  const panel = document.createElement("div");
  panel.id = "grp-q-panel";
  panel.className = "grp-q-panel";
  panel.innerHTML = q && !matches.length
    ? `<div class="muted sm">没有匹配「${esc(q)}」的账号</div>`
    : matches.map((a) => `<div class="qrow">
        <span class="qacc">${esc(a)}</span>
        <select class="qto" data-acc="${esc(a)}">
          ${(groupsData.groups || []).filter((g) => g.id !== all[a])
            .map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join("")}
        </select>
        <button class="btn ghost sm" onclick="moveAccountTo(${esc(JSON.stringify(a))}, this.previousElementSibling.value, this.closest('.qrow').querySelector('.qto'));toggleGrpSearch(false)">移</button>
      </div>`).join("");
  if (inp) inp.parentNode.insertBefore(panel, inp.nextSibling);
}
if (typeof document !== "undefined") document.addEventListener("click", (e) => {
  if (_grpCtx && !_grpCtx.contains(e.target)) closeGrpCtx();
});

if (typeof window !== "undefined") {
  window.addEventListener("keydown", (ev) => {
    const target = ev.target;
    // P2-15：ev.target 可能是 document / 文本节点（合成事件、扩展派发），没有 closest
    const inField = !!(target && typeof target.closest === "function"
      && target.closest("input, textarea, select"));
    if (inField || !!(target && target.isContentEditable)) return;
    if ((ev.ctrlKey || ev.metaKey) && !ev.shiftKey && ev.key.toLowerCase() === "z" && grpUndoStack.length) {
      ev.preventDefault();
      grpUndo();
    }
  });
}

function allAccountNames() {
  return (CFG && CFG.accounts) || [];
}

/* P2-3：addGroupMember / removeGroupMember 已删除（引用不存在的 #grp-add-sel / 无调用方；
   成员增删统一走 moveAccountTo + /api/groups/{gid}/transfer） */

/* ═══ 失败分析：把日志翻译成非技术用户能看懂的人话 ═══ */
const FAIL_RULES = [
  {
    match: /主页无内嵌数据|Failed to parse JSON|Expecting value|Unable to extract/,
    why: "TikTok 发现短时间内访问太频繁，暂时限制了当前网络（通常几分钟到半小时自动解除）",
    steps: [
      "等 10～30 分钟，再点一次「开始运行」",
      "或在网络工具里换一个线路，然后重新运行",
      "账号已经保存好了，不需要重新添加",
    ],
    contact: false,
  },
  {
    match: /台账被占用|~\$|锁文件/,
    why: "台账表格正被 Excel/WPS 打开着，程序没法写入",
    steps: [
      "关闭正在打开台账的 Excel/WPS 窗口",
      "再点一次「开始运行」",
    ],
    contact: false,
  },
  {
    match: /itemStruct empty|下架|私有/,
    why: "有一条视频已经被作者删除或设为私密了",
    steps: [
      "不用处理，其他视频不受影响",
      "下次运行会自动跳过这条",
    ],
    contact: false,
  },
  {
    match: /ffmpeg|venv|ModuleNotFound|No module named/,
    why: "电脑上缺少运行所需的组件（一般是换了电脑或重装系统后）",
    steps: [
      "联系帮你搭建工作台的人，把本页红色日志截图发给对方",
    ],
    contact: true,
  },
  {
    match: /NameError|SyntaxError|AttributeError|KeyError|TypeError|Traceback/,
    why: "程序自己出了内部错误（不是网络或 TikTok 的问题）",
    steps: [
      "点「终止」结束当前任务",
      "联系技术人员，并把本页红色日志截图发给对方",
    ],
    contact: true,
  },
  {
    match: /exit_code=3221225477|0xC0000005|OSError/,
    why: "程序运行中途崩溃了",
    steps: [
      "等 1～2 分钟，再点一次「开始运行」试试",
      "如果连续两次都失败，联系技术人员并发送红色日志截图",
    ],
    contact: false,
  },
];

function showFailAnalysis() {
  if (!runProgress) return;
  const fa = $("#fail-analysis");
  if (!fa) return;
  const text = [...(runProgress.errLines || []), ...(runProgress.lines || [])].join("\n");
  let rule = null;
  for (const r of FAIL_RULES) {
    if (r.match.test(text)) { rule = r; break; }
  }
  if (!rule) {
    rule = {
      why: "运行失败的具体原因暂时无法自动判断",
      steps: [
        "先点一次「开始运行」重试——很多失败是临时的，重试就能成功",
        "如果重试还是失败，联系技术人员，并把本页红色日志截图发给对方",
      ],
      contact: true,
    };
  }
  const steps = rule.steps.map((s, i) =>
    `<div class="fa-step"><span class="fa-no">${i + 1}</span>${esc(s)}</div>`).join("");
  fa.innerHTML = `
    <div class="fa-card">
      <div class="fa-title">出了什么问题？怎么办？</div>
      <div class="fa-why">${esc(rule.why)}</div>
      <div class="fa-steps">${steps}</div>
      <div class="fa-contact ${rule.contact ? "need" : "no"}">
        ${rule.contact
          ? "建议联系技术人员处理（附上本页日志截图更快）"
          : "不需要联系技术人员，按上面步骤自己就能解决"}
      </div>
    </div>`;
  fa.classList.remove("hidden");
}

/* 把一条流式日志行翻译成进度状态；无法识别的行静默忽略 */
function parseRunLine(t) {
  if (!runProgress || !t) return;
  runProgress.lines.push(t);
  if (runProgress.lines.length > 200) runProgress.lines.shift();
  // 错误行单独留一份，避免被 200 行滑动窗口挤出后归因失效（P2-7）
  if (/FAIL|Fail|错误|失败|EXCEPTION|Traceback|exit_code=[1-9]/.test(t)) {
    if (!runProgress.errLines) runProgress.errLines = [];
    runProgress.errLines.push(t);
    if (runProgress.errLines.length > 200) runProgress.errLines.shift();
  }
  let m;

  // 每个账号复用同一进度面板，切换时清空上一账号的终态。
  m = t.match(/^\[multi\] \((\d+)\/(\d+)\) 账号: (.+)$/);
  if (m) {
    runProgress.batch = { index: Number(m[1]), total: Number(m[2]), account: m[3] };
    runProgress.order.forEach((k) => {
      runProgress.state[k] = { status: "wait", note: "", startedAt: 0 };
    });
    renderRunProgress();
    setRunProgressNote(`账号 ${m[1]}/${m[2]}：${m[3]}`);
    return;
  }
  if (/^\[multi\] 全部账号完成$/.test(t)) {
    setRunProgressNote("🎉 全部账号完成");
    return;
  }
  if (/^\[multi\] .*账号失败/.test(t)) {
    setRunProgressNote(t.replace(/^\[multi\] /, ""));
    return;
  }
  m = t.match(/^\[\d{1,2}:\d{2}:\d{2}\]\s+(s\w+)\s+\|\s+已完成，跳过/);
  if (m) { setRunStep(m[1], "ok", "已完成，断点续跑跳过"); return; }

  // 步骤开始：[HH:MM:SS] s1 | 开始: ...
  m = t.match(/^\[\d{1,2}:\d{2}:\d{2}\]\s+(s\w+)\s+\|\s+开始:/);
  if (m) { setRunStep(m[1], "running"); return; }

  // 中途进度：[进度] 音频下载 3/5：完成
  m = t.match(/^\[进度\]\s+(.+)$/);
  if (m) {
    const cur = runProgress.order.find((k) => runProgress.state[k].status === "running");
    if (cur) setRunStep(cur, "running", m[1]);
    return;
  }

  // 步骤结束：[HH:MM:SS] s1_download | ok=True cost=35.6s | 门禁:PASS | ...
  m = t.match(/^\[\d{1,2}:\d{2}:\d{2}\]\s+(s\w+)\s+\|\s+ok=(True|False)\s+cost=([\d.]+)s/);
  if (m) {
    const ok2 = m[2] === "True";
    // 只有日志里出现「门禁:xxx」且不是 PASS 才算未过；缺少该字段视为不适用（如 s5b_products）
    const gateM = t.match(/门禁:([A-Za-z_]+)/);
    const gate = gateM ? gateM[1] === "PASS" : true;
    const sec = Math.round(parseFloat(m[3]));
    let note;
    const s = runProgress.state[m[1]];
    if (!ok2) note = (s && s.note) || "执行出错，详见下方日志";
    else note = (gate ? "完成" : "完成（门禁未过）") + "，耗时 " + sec + " 秒";
    setRunStep(m[1], ok2 ? "ok" : "fail", note);
    return;
  }

  // 脚本摘要 JSON（整行 JSON）→ 翻译成人话挂在当前运行中的步骤上
  const trimmed = t.trim();
  if (trimmed.startsWith("{")) {
    try {
      const j = JSON.parse(trimmed);
      const cur = runProgress.order.find((k) => runProgress.state[k].status === "running");
      if (!cur) return;
      let msg = "";
      if (typeof j.matched === "number") {
        msg = `发现 ${j.matched} 条视频，新下载 ${j.downloaded} 条` +
              (j.skipped ? `，已有跳过 ${j.skipped} 条` : "") +
              (j.failed && j.failed.length ? `，失败 ${j.failed.length} 条` : "") +
              (j.degraded ? "（TikTok 暂时限流，按已有产物计数）" : "");
      } else if (Array.isArray(j.items)) {
        msg = `抓取成功 ${j.items.length} 条` +
              (Array.isArray(j.errors) && j.errors.length ? `，失败/下架 ${j.errors.length} 条` : "");
      } else if (typeof j.total === "number" && Array.isArray(j.done)) {
        msg = `转写 ${j.done.length}/${j.total} 条完成`;
      }
      if (msg) setRunStep(cur, "running", msg);
    } catch (e) { /* 非 JSON，忽略 */ }
    return;
  }

  if (t.includes("HALT")) {
    setRunProgressNote("⚠️ 有步骤未通过检查，流程已停止（原因见下方日志）");
    return;
  }
  if (/\bDONE\b/.test(t)) {
    const batch = runProgress.batch;
    setRunProgressNote(batch ? `账号 ${batch.index}/${batch.total}：${batch.account} 已完成，等待批次结束` : "🎉 全部步骤执行完毕");
    return;
  }
}

/* P2-3：hideRunProgress 已删除（无调用方；进度面板由 attachRun / buildRunProgress 管理） */

/* ─────────── 对标分析（第二版 · A 方案） ─────────── */
/* P2-7：全站唯一的数字展示口径（<1 万用千分位原值 / 万 / 亿）。
   baNum 与分组表都复用这里，原来「218.3万 / 109.0万 / 7,541 / 737」四套写法并存；
   精确原值只放在 title（baRaw），不再参与正文展示。 */
/* 数字口径：<1 万用千分位整数（6,874 便于读精确值），≥1 万用「万」，≥1 亿用「亿」；
   原值另放在 title。2026-10-22 复核后保留千分位（去掉逗号反而更难读），
   「同列混排」改为用等宽数字 + 右对齐 + 列宽来消化。 */
function fmtNum(n) {
  n = Number(n) || 0;
  if (n >= 1e8) return (n / 1e8).toFixed(1) + "亿";
  if (n >= 1e4) return (n / 1e4).toFixed(1) + "万";
  return n.toLocaleString("zh-CN");
}

let benchReqSeq = 0;   // 请求令牌：防止慢的旧响应覆盖新选择
async function loadBench(force) {
  const sel = $("#bench-account");
  const acct = sel && sel.value ? sel.value : "";
  const mySeq = ++benchReqSeq;
  // 用 URLSearchParams 生成查询串：自动处理 ?/& 与转义，
  // 避免"参数为空时拼出 /api/benchmark&refresh=true"这类非法 URL（会 404）。
  const params = new URLSearchParams();
  if (benchGroupAccounts !== null) {
    // 组内对标模式：传账号子集（后端按子集聚合 KPI/patterns）
    params.set("accounts", acct && benchGroupAccounts.includes(acct) ? acct : benchGroupAccounts.join(","));
  } else if (acct) {
    params.set("account", acct);
  }
  if (force) params.set("refresh", "true");
  const qs = params.toString();
  const q = "/api/benchmark" + (qs ? `?${qs}` : "");
  try {
    const d = await api(q);
    pollOk("bench");
    if (mySeq !== benchReqSeq) return;   // 已有更新的请求，丢弃本次响应
    const up = $("#bench-updated");
    const sig = benchSignature(d);
    const firstPaint = !benchData;
    // 数据指纹没变 → 只更新时间戳，不重建 DOM（否则图表入场动画每轮都会重播）
    if (!force && !firstPaint && sig === benchSig) {
      if (up) up.textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN");
      return;
    }
    benchSig = sig;
    benchData = d;
    if (up) up.textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN");
    bvQuiet = !firstPaint;               // 已有内容时的增量更新：静默重绘，不播入场
    renderBench(benchData);
    bvQuiet = false;
  } catch (e) {
    if (mySeq !== benchReqSeq) return;
    const kp = $("#bench-kpis");
    pollFail("bench");
    if (kp) kp.innerHTML = `<div class="card">对标分析加载失败：${esc(e.message)}</div>`;
    toast("对标分析加载失败：" + e.message, "error");
  }
}

/* 数据指纹：只有这些值变化才值得重绘（时间戳类字段不参与，避免"看似变了"） */
function benchSignature(d) {
  const k = (d && d.kpis) || {};
  const accs = (d && d.accounts) || [];
  const vids = (d && d.videos) || [];
  const accSig = accs.slice(0, 60).map((a) =>
    [a.account, a.videos, a.total_plays, a.product_coverage].join(":")).join(",");
  return [k.accounts, k.total_videos, k.total_plays, k.avg_engagement, k.product_coverage,
    accs.length, vids.length, accSig].join("|");
}

function renderBench(d) {
  benchData = d;
  d = d || {};
  const k = d.kpis || {};
  const accounts = d.accounts || [];
  const patterns = d.patterns || {};
  renderBenchAccountOptions(d);
  renderKpis(k, accounts);
  renderBenchAccounts(accounts);
  renderBenchVideos();
  renderBenchTrend();
  renderBenchAccChart();
  renderPlayBuckets(patterns.play_buckets || []);
  renderTagCloud("#bench-tags", patterns.top_tags || []);
  renderTagCloud("#bench-hooks", (patterns.hook_words || []).map((w) => w.replace(/^./, (c) => c.toUpperCase())), true);
  renderBenchProducts(patterns.products_top || []);
  renderTopVideos(patterns.top_videos || []);
  renderTakeaways(d.takeaways || []);
}

function renderBenchAccountOptions(d) {
  // 用后端 account_options（恒为全量「有数据账号」清单，不受单账号过滤影响）
  // 重建 #bench-account：空壳账号（无 scrape 产物）不进下拉。
  // 载荷缺该字段（旧测试桩/旧接口）时不动下拉，保持向后兼容。
  const sel = $("#bench-account");
  if (!sel || !d || !Array.isArray(d.account_options)) return;
  const current = sel.value || "";
  const uniq = [...new Set((d.account_options)
    .filter((a) => a && a.account)
    .map((a) => a.account))];
  // 组内对标模式：下拉只列组内账号子集，并注入横幅；非组内则全量
  const isGroup = benchGroupAccounts !== null;
  const pool = isGroup ? uniq.filter((n) => benchGroupAccounts.includes(n)) : uniq;
  const banner = $("#bench-group-banner");
  if (isGroup && !banner) {
    const el_ = document.createElement("div");
    el_.id = "bench-group-banner";
    el_.className = "bench-group-banner";
    el_.innerHTML = `<span>组内对标模式</span>
      <button class="btn ghost sm" onclick="exitGroupBench()">退出组内对标</button>`;
    const acts = $("#view-bench .head-actions");
    if (acts) acts.parentNode.insertBefore(el_, acts);
  } else if (!isGroup && banner) {
    banner.remove();
  }
  sel.innerHTML = `<option value="">${isGroup ? "本组全部" : "全部账号"}</option>` +
    pool.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  sel.value = pool.includes(current) ? current : "";
}

function exitGroupBench() {
  benchGroupAccounts = null;
  benchGroupId = null;
  const b = $("#bench-group-banner");
  if (b) b.remove();
  $("#bench-account").value = "";
  loadBench(true);
}

function pctText(v) {
  return v === null || v === undefined || v === "" ? "—" : v + "%";
}

function renderKpis(k, accounts) {
  const box = $("#bench-kpis");
  if (!box) return;
  k = k || {};
  accounts = accounts || [];
  const cards = [
    ["对标账号", k.accounts, `共 ${k.videos} 条视频`],
    ["视频总数", k.videos, `覆盖 ${accounts.filter((a) => a.videos).length} 账号`],
    ["总播放量", fmtNum(k.total_plays), "全部视频合计"],
    ["平均互动率", pctText(k.avg_engagement), "(赞+评+转+藏)/播放"],
    ["挂车率", pctText(k.product_coverage), `${k.with_products}/${k.videos} 条带小黄车`],
  ];
  box.innerHTML = cards
    .map(
      ([label, val, sub]) =>
        `<div class="card kpi-card"><div class="kpi-l">${label}</div>` +
        `<div class="kpi-v">${val}</div><div class="kpi-s">${sub}</div></div>`
    )
    .join("");
}

/* 账号对比表：表头排序（8 列，标签/钩子列不排序）+ 列宽拖拽 + 行选中联动账号图 */
const BA_SORT_COLS = ["account", "videos", "total_plays", "avg_plays", "avg_engagement",
                      "product_coverage", null, null];
let baSort = { key: "total_plays", dir: -1 };
let baActive = "";        // 当前高亮账号（表格行 ↔ 账号图双向）

/* ── 折叠口径（方案 A）：默认只显 TOP12（按总播放，与账号条形图 BA_TOP 对齐），
   其余并入可展开的「其余 N 个账号」；选到 12 名外的账号时自动并入并标注全量名次。
   展开/收起态记忆在 localStorage（tk:baExpanded）。 ── */
const BA_TOP = 12;
// 展开/收起态记忆在 localStorage（tk:baExpanded）；jsdom 无 url（opaque origin）时
// localStorage 访问会抛 DOMException，故用 try/catch 兜底，取不到默认折叠。
let baExpanded = false;
try { baExpanded = (typeof localStorage !== "undefined" && localStorage.getItem("tk:baExpanded") === "1"); } catch (_) { baExpanded = false; }
let baAccounts = [];       // 最近一次渲染的账号全量（供展开/收起/重渲染复用，不依赖 benchData 是否已赋值）
function baToggleExpand() { baSetExpanded(!baExpanded); }
function baSetExpanded(v) {
  baExpanded = !!v;
  try { localStorage.setItem("tk:baExpanded", baExpanded ? "1" : "0"); } catch (_) {}
  renderBenchAccounts(baAccounts);
}

/* 数值展示：同列口径统一（<1 万用千分位整数，≥1 万用「万」，≥1 亿用「亿」），
   原始值放 title，避免同列出现 7541 与 2.2万 混排 */
function baRaw(n) {
  return (Number(n) || 0).toLocaleString("zh-CN");
}
/* P2-7：与 fmtNum 同一口径（保留函数名，调用点不必改） */
function baNum(n) {
  return fmtNum(n);
}
/* 标签列：最多 2 个 + 「+N」，全部名单进 title —— 旧版 nowrap 省略号只能露出半个标签 */
function baChips(list, cls) {
  const arr = (list || []).filter(Boolean);
  if (!arr.length) return `<span class="muted">—</span>`;
  const shown = arr.slice(0, 2);
  const rest = arr.length - shown.length;
  return shown.map((t) => `<span class="mini-tag ${cls}">${esc(t)}</span>`).join("") +
    (rest > 0 ? `<span class="mini-tag more" title="${esc(arr.join("、"))}">+${rest}</span>` : "");
}

function renderBenchAccounts(accounts) {
  accounts = Array.isArray(accounts) ? accounts : [];
  baAccounts = accounts;   // 记录本次渲染的全量账号，供展开/收起/选中重渲染复用
  const table = $("#bench-accounts");
  if (!table) return;   // P2-3：删掉 "#bench-accounts-box" 兼容分支（该 id 在 HTML 中不存在）
  if (!accounts.length) {
    // 不能改 outerHTML（会把 <table> 节点整个替换成 div，二次渲染时选择器失效）
    const acct = $("#bench-account") ? $("#bench-account").value : "";
    const msg = acct
      ? `账号「${esc(acct)}」暂无本地产物（缺 output/scrape_${esc(acct)}.json），请先跑 s2 抓取`
      : "暂无账号数据";
    table.innerHTML = TKV.empty(8, {
      icon: "▤", title: msg,
      desc: acct ? "" : "抓取并入库后，这里会按账号聚合播放、互动与挂车率。",
      action: `<button class="btn ghost sm" onclick="loadBench(true)">重新读取</button>`,
    });
    return;
  }
  const key = baSort.key, dir = baSort.dir;
  const sorted = accounts.slice().sort((a, b) => {
    if (key === "account") return dir * String(a.account || "").localeCompare(String(b.account || ""), "zh");
    return dir * ((Number(a[key]) || 0) - (Number(b[key]) || 0));
  });
  // 列宽按语义分配（账号宽、数字窄、标签两列等宽）。
  // 2026-10-22 R3（独立验收）：不再写死 px，改为「语义权重 → 百分比」，
  // 由 table-layout:fixed + width:100% 铺满容器；1280 桌面上整表 = 容器宽，无横向滚动。
  // 用户拖过手柄后 tablekit 会把列宽冻结成 px（此时横向滚动是明确保留的行为），重置可回到这里。
  const cols = [
    { w: 202, cls: "acc", label: "账号" },
    { w: 70, cls: "rt", label: "视频" },
    { w: 100, cls: "rt", label: "总播放" },
    { w: 92, cls: "rt", label: "均播放" },
    { w: 100, cls: "rt", label: "均互动率" },
    { w: 104, cls: "rt", label: "挂车率" },
    { w: 202, cls: "", label: "高频标签" },
    { w: 202, cls: "", label: "钩子词" },
  ];
  const sumW = cols.reduce((s, c) => s + c.w, 0);
  const head = `<thead><tr>${cols
    .map((c) => `<th class="${c.cls}" data-w="${c.w}" style="width:${((c.w * 100) / sumW).toFixed(2)}%">${c.label}</th>`)
    .join("")}</tr></thead>`;

  /* 行模板：标签列 2+「+N」，pinned 行追加「全量第 N 名」标记 */
  const rowTpl = (a, rankMark) => `<tr data-acct="${esc(a.account)}" tabindex="0"
        title="${esc(a.account)}｜单击联动右侧条形图 · 双击打开台账"
        ${rankMark ? `class="ba-pin"` : ""}>
        <td class="acc" title="${esc(a.account)}">${esc(a.account)}${a.lang ? `<span class="fx"> ${esc(a.lang)}</span>` : ""}${rankMark ? `<span class="fx ba-pin-rank">全量第 ${rankMark} 名</span>` : ""}</td>
        <td class="rt">${Number(a.videos) || 0}</td>
        <td class="rt" title="${baRaw(a.total_plays)}">${baNum(a.total_plays)}</td>
        <td class="rt" title="${baRaw(a.avg_plays)}">${baNum(a.avg_plays)}</td>
        <td class="rt">${a.avg_engagement == null ? "—" : a.avg_engagement + "%"}</td>
        <td class="rt ba-cov" title="挂车率 ${a.product_coverage == null ? "—" : a.product_coverage + "%"}">
          ${Number(a.product_coverage) ? `<span class="ba-cov-bar"><b style="width:${Math.max(0, Math.min(100, Number(a.product_coverage) || 0))}%"></b></span>` : ""}
          <span class="ba-cov-v">${a.product_coverage == null ? "—" : a.product_coverage + "%"}</span>
        </td>
        <td class="ba-tags">${baChips(a.top_tags, "")}</td>
        <td class="ba-tags">${baChips(a.top_hooks, "hook")}</td>
      </tr>`;

  /* 折叠（方案 A）：默认按总播放取 TOP12，其余并入 1 行可展开的「其余 N 个账号」；
     选中 12 名外的账号（baActive）时自动并入并标全量名次。展开态下显示全部。 */
  const byPlays = accounts.slice().sort((a, b) => (Number(b.total_plays) || 0) - (Number(a.total_plays) || 0));
  const rankOf = {}; byPlays.forEach((a, i) => { rankOf[a.account] = i + 1; });
  const topSet = new Set(byPlays.slice(0, BA_TOP).map((a) => a.account));
  const total = accounts.length;
  const allPlays = accounts.reduce((s, a) => s + (Number(a.total_plays) || 0), 0);
  const allVids = accounts.reduce((s, a) => s + (Number(a.videos) || 0), 0);

  let rows;          // 实际渲染的账号行（按当前表头排序 baSort.key）
  let hiddenN = 0, hiddenPlays = 0, hiddenVids = 0;
  if (baExpanded || total <= BA_TOP) {
    rows = sorted.map((a) => rowTpl(a));
  } else {
    const pinnedAcc = baActive && !topSet.has(baActive) ? baActive : null;
    const pinnedObj = pinnedAcc ? accounts.find((a) => a.account === pinnedAcc) : null;
    const shown = sorted.filter((a) => topSet.has(a.account));
    hiddenN = total - shown.length - (pinnedObj ? 1 : 0);
    // 折叠统计（隐藏 = 非 top12 且非 pinned）
    accounts.forEach((a) => {
      const isShown = topSet.has(a.account) || a.account === pinnedAcc;
      if (!isShown) { hiddenPlays += Number(a.total_plays) || 0; hiddenVids += Number(a.videos) || 0; }
    });
    rows = shown.map((a) => rowTpl(a));
    if (pinnedObj) rows.push(rowTpl(pinnedObj, rankOf[pinnedObj.account] || ""));
    rows.push(
      `<tr class="ba-fold" data-ba-fold="1" tabindex="0" role="button"
           title="点击展开全部账号">
        <td colspan="8" class="ba-fold-cell">
          <button type="button" class="ba-fold-btn" onclick="baToggleExpand()">
            ＋ 其余 ${hiddenN} 个账号（合计播放 ${baNum(hiddenPlays)} · ${hiddenVids} 条视频）▸ 展开
          </button>
        </td></tr>`);
  }

  /* 展开态：尾部加 1 行「收起」，可一键回到 TOP12 */
  if (baExpanded && total > BA_TOP) {
    rows.push(
      `<tr class="ba-fold on" data-ba-fold="1" tabindex="0" role="button" title="点击收起">
        <td colspan="8" class="ba-fold-cell">
          <button type="button" class="ba-fold-btn" onclick="baToggleExpand()">
            ▴ 已展开全部 ${total} 个账号 · 收起（TOP ${BA_TOP} + 其余 ${total - BA_TOP}）
          </button>
        </td></tr>`);
  }

  table.innerHTML = head + `<tbody>${rows.join("")}</tbody>`;

  const note = $("#ba-sort-note");
  if (note) {
    const i = BA_SORT_COLS.indexOf(key);
    const label = i >= 0 ? cols[i].label : "";
    let txt = label ? `按 ${label}${dir > 0 ? " 升序 ↑" : " 降序 ↓"}` : "";
    if (total > BA_TOP) {
      txt += baExpanded
        ? ` · 已展开全部 ${total} 个账号`
        : ` · 默认 TOP ${BA_TOP}，其余 ${total - BA_TOP} 个已折叠`;
    }
    note.textContent = txt;
  }

  TKV.sortable(table, (i) => baSortBy(i), { skip: [6, 7] });
  TKV.setSort(table, BA_SORT_COLS.indexOf(key), dir);
  // 2026-10-22 R3：fluid=true —— 默认按百分比铺满容器；拖过手柄后 tablekit 自动切 px 模式。
  TKV.resizable(table, "bench-accounts", { min: 72, fluid: true });
  baBindRows(table);
  TKV.reveal(table.tBodies[0]);
  renderBaHighlight();
}

/* R3：把账号表的列宽记忆清掉并重渲染（回到「铺满容器」的首次进入状态） */
function baResetColWidths() {
  const table = $("#bench-accounts");
  if (table && TKV.resetWidths) TKV.resetWidths(table, "bench-accounts");
  renderBenchAccounts((benchData && benchData.accounts) || []);
  if (typeof toast === "function") toast("列宽已重置为自适应");
}

function baSortBy(i) {
  const key = BA_SORT_COLS[i];
  if (!key) return;
  if (baSort.key === key) baSort.dir = -baSort.dir;
  else baSort = { key, dir: key === "account" ? 1 : -1 };
  renderBenchAccounts((benchData && benchData.accounts) || []);
}

/* 折叠态下的 pinned 归属：某账号在 baActive 下是否被并入表格（= 非 TOP12 且被选中）。
   供点击判断「是否需要重建表格」——只有它变化才重建，否则原地高亮。 */
function baPinnedOf(active, accounts) {
  if (!active || !Array.isArray(accounts) || accounts.length <= BA_TOP) return "";
  const top = accounts.slice().sort((a, b) => (Number(b.total_plays) || 0) - (Number(a.total_plays) || 0))
    .slice(0, BA_TOP).map((a) => a.account);
  return top.includes(active) ? "" : active;
}

function baBindRows(table) {
  if (table.dataset.baSel === "1") return;
  table.dataset.baSel = "1";
  table.addEventListener("click", (ev) => {
    const tr = ev.target.closest ? ev.target.closest("tbody tr") : null;
    if (!tr || !tr.getAttribute("data-acct")) return;
    const acc = tr.getAttribute("data-acct");
    const prev = baActive;
    baActive = baActive === acc ? "" : acc;
    // 仅当「折叠态 + 全量 > BA_TOP + 所选的 pinned 归属变化」才重建表格，
    // 否则原地高亮即可（避免正常点 TOP12 行时丢 DOM 身份 → B8 断言）。
    const needRebuild = !baExpanded && baAccounts.length > BA_TOP &&
      baPinnedOf(prev, baAccounts) !== baPinnedOf(baActive, baAccounts);
    if (needRebuild) renderBenchAccounts(baAccounts);
    else renderBaHighlight();
    renderBenchAccChart();
  });
  // 双击 / 回车：进入该账号的台账表格视图（单击仍保留联动条形图）
  const openRow = (tr) => {
    const acc = tr && tr.getAttribute("data-acct");
    if (acc) openAccountSheet(acc);
  };
  table.addEventListener("dblclick", (ev) => {
    const tr = ev.target.closest ? ev.target.closest("tbody tr") : null;
    if (tr) openRow(tr);
  });
  table.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter") return;
    const tr = ev.target.closest ? ev.target.closest("tbody tr") : null;
    if (tr) { ev.preventDefault(); openRow(tr); }
  });
}

function renderBaHighlight() {
  const table = $("#bench-accounts");
  if (!table || !table.tBodies[0]) return;
  Array.from(table.tBodies[0].rows).forEach((tr) => {
    tr.classList.toggle("tk-sel", !!baActive && tr.getAttribute("data-acct") === baActive);
  });
}

/* ─────────── 视频明细（对标分析页 · 跨账号一张表）───────────
   数据源：/api/benchmark 已返回的全量 videos（687 条），前端筛选/排序/分页，
   零新增请求；账号下拉切换时后端已按账号聚合，本表自动跟着变。 */
/* 2026-10-20 接入 tablekit：列宽拖拽 / 表头排序 / 页码分页 / 行展开 / 多选对比 /
   三图联动 / 骨架与空态。原有筛选口径（q/days/minPlays/prod/src/sort）保持不变。 */
const BV_PAGE = 50;
const BV_BANDS = [
  { key: "lt1k", label: "<1千", test: (v) => (v.plays || 0) < 1000 },
  { key: "k1w", label: "1千-1万", test: (v) => (v.plays || 0) >= 1000 && (v.plays || 0) < 10000 },
  { key: "w10", label: "1万-10万", test: (v) => (v.plays || 0) >= 10000 && (v.plays || 0) < 100000 },
  { key: "w100", label: "≥10万", test: (v) => (v.plays || 0) >= 100000 },
];
// 表头列 → 排序键（null = 该列不参与排序）
const BV_SORT_COLS = ["account", "time", "plays", "likes", "comments", "shares", "collects",
                      "engagement", "products", "source", null, null];
const BV_SORT_LABEL = { account: "账号", time: "发布时间", plays: "播放", likes: "点赞",
  comments: "评论", shares: "转发", collects: "收藏", engagement: "互动率",
  products: "挂车数", source: "来源" };

const BV_CMP_MAX = 4;   // 对比条一屏最多 4 列；选中入口按此上限拦截，不做静默截断
let bvState = { q: "", days: "", minPlays: 0, prod: "", src: "", sort: "plays_desc",
                size: BV_PAGE, page: 1, band: "", acct: "", day: "" };
let bvSel = new Set();        // 选中的 vid（跨页保留）
let bvRowsCache = [];         // 当前筛选结果，供对比条 / 展开详情复用

function bvTime(v) {
  // 后端 publish_time 形如 "2026-10-14 14:20:00"；Safari 不认空格分隔 → 换 T
  const s = (v && v.publish_time) || "";
  return s ? new Date(s.replace(" ", "T")).getTime() || 0 : 0;
}

// 排序状态拆成 {key, dir}："plays_desc" → {key:"plays", dir:-1}
function bvSortParts(sortStr) {
  const s = sortStr || bvState.sort || "plays_desc";
  const i = s.lastIndexOf("_");
  if (i < 0) return { key: s, dir: -1 };
  return { key: s.slice(0, i), dir: s.slice(i + 1) === "asc" ? 1 : -1 };
}

function bvSortValue(v, key) {
  if (key === "time") return bvTime(v);
  if (key === "account") return String(v.account || "").toLowerCase();
  if (key === "source") return String(v.source || "scrape");
  if (key === "products") return (v.products || []).length;
  return Number(v[key]) || 0;
}

// ov：临时覆盖筛选条件（图表算「去掉自身过滤后」的分布用，不影响表格口径）
function bvFiltered(ov) {
  const st = ov ? Object.assign({}, bvState, ov) : bvState;
  const all = (benchData && benchData.videos) || [];
  const q = (st.q || "").trim().toLowerCase();
  const minPlays = Number(st.minPlays) || 0;
  const cutoff = st.days ? Date.now() - Number(st.days) * 864e5 : 0;
  const band = st.band ? BV_BANDS.find((b) => b.key === st.band) : null;
  const rows = all.filter((v) => {
    if ((v.plays || 0) < minPlays) return false;
    const hasProd = (v.products || []).length > 0;
    if (st.prod === "yes" && !hasProd) return false;
    if (st.prod === "no" && hasProd) return false;
    if (st.src && (v.source || "scrape") !== st.src) return false;
    if (cutoff && bvTime(v) < cutoff) return false;
    if (band && !band.test(v)) return false;
    if (st.acct && v.account !== st.acct) return false;
    if (st.day && bvDayKey(bvTime(v)) !== st.day) return false;
    if (q) {
      const hay = `${v.title || ""} ${v.desc || ""} ${v.account || ""}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });
  const sp = bvSortParts(st.sort);
  rows.sort((a, b) => {
    const x = bvSortValue(a, sp.key), y = bvSortValue(b, sp.key);
    let c = 0;
    if (typeof x === "string" || typeof y === "string") c = String(x).localeCompare(String(y), "zh");
    else c = x - y;
    if (c) return c * sp.dir;
    // 次级键：vid 升序，保证同值行顺序稳定（与后端聚合口径一致）
    return String(a.vid || "").localeCompare(String(b.vid || ""));
  });
  return rows;
}

function bvMedian(nums) {
  if (!nums.length) return 0;
  const s = [...nums].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : Math.round((s[m - 1] + s[m]) / 2);
}

function renderBenchVideos() {
  const table = $("#bench-videos");
  if (!table) return;
  // 重绘前记住滚动位置：整表重建会把 .bv-wrap 的 scrollTop 夹回 0（增量刷新时最明显）
  const wrap = document.querySelector(".bv-wrap");
  const keepTop = wrap ? wrap.scrollTop : 0;
  const restoreTop = () => { if (wrap && keepTop) wrap.scrollTop = keepTop; };
  const rows = bvFiltered();
  bvRowsCache = rows;
  // 选中集合随筛选收敛：被筛掉的条目不再计入对比，否则会出现「已选 4 条」却只算 2 列。
  if (bvSel.size) {
    const alive = new Set(rows.map((v) => String(v.vid || "")));
    let dropped = 0;
    bvSel.forEach((id) => { if (!alive.has(id)) { bvSel.delete(id); dropped++; } });
    if (dropped) toast(`筛选后 ${dropped} 条已选视频不在当前结果中，已移出对比`);
  }
  const total = ((benchData && benchData.videos) || []).length;
  const size = bvState.size || BV_PAGE;
  const pages = Math.max(1, Math.ceil(rows.length / size));
  if (bvState.page > pages) bvState.page = pages;
  if (bvState.page < 1) bvState.page = 1;
  const page = bvState.page;
  const shown = rows.slice((page - 1) * size, page * size);
  const plays = rows.map((v) => v.plays || 0);

  const cnt = $("#bv-count");
  if (cnt) {
    const lo = (benchData && benchData.kpis && benchData.kpis.ledger_only) || 0;
    cnt.textContent = `（${rows.length} / ${total} 条${lo ? ` · 含台账补录 ${lo}` : ""}）`;
  }
  const sum = $("#bv-summary");
  if (sum) {
    const tag = [];
    if (bvState.band) tag.push("分档");
    if (bvState.acct) tag.push(bvState.acct);
    if (bvState.day) tag.push(bvState.day.slice(5));
    sum.textContent = rows.length
      ? `合计 ${fmtNum(plays.reduce((a, b) => a + b, 0))} 播放 · 中位 ${fmtNum(bvMedian(plays))} · 均互动 ${
          (rows.reduce((a, v) => a + (v.engagement || 0), 0) / rows.length).toFixed(2)}%${
          tag.length ? ` · 图筛：${tag.join(" / ")}` : ""}`
      : "无匹配视频";
  }
  const more = $("#bv-more");
  // 页码分页接管；TKV 降级时也有原生上/下一页，不会只剩首屏
  if (more) more.classList.add("hidden");

  const head = `<thead><tr>
    <th>账号</th><th>发布时间</th><th class="num">播放</th><th class="num">点赞</th>
    <th class="num">评论</th><th class="num">转发</th><th class="num">收藏</th>
    <th class="num">互动率</th><th class="num">挂车</th><th>来源</th><th>视频链接</th><th>标题 / 文案</th>
  </tr></thead>`;

  if (!rows.length) {
    table.innerHTML = head + `<tbody>${TKV.empty(12, {
      icon: "⌕",
      title: "没有符合条件的视频，试试放宽筛选",
      desc: "当前关键词 / 时间 / 播放量 / 挂车 / 来源条件叠加后无结果；可点「重置」回到全量。",
      action: `<button class="btn ghost sm" onclick="bvReset()">重置筛选</button>
               <button class="btn ghost sm" onclick="bvExportCsv()">导出当前结果</button>`,
    })}</tbody>`;
    TKV.pager($("#bv-pager"), { total: 0, page: 1, size });
    renderBvViz();
    renderBvCompare();
    restoreTop();
    return;
  }

  const body = shown.map((v) => {
    const title = (v.title || v.desc || "").replace(/\s+/g, " ").slice(0, 90);
    // 视频链接：后端 v.url 优先；缺 url 时用 @账号 + vid 兜底拼 TikTok 原链，保证每条都能点开
    const vurl = String(v.url || "").trim()
      || (v.vid ? `https://www.tiktok.com/@${v.account || ""}/video/${v.vid}` : "");
    const link = v.url
      ? `<a href="${esc(v.url)}" target="_blank" rel="noopener" title="${esc(v.desc || v.title || "")}">${esc(title)}</a>`
      : esc(title);
    const vcell = vurl
      ? `<a class="vid-link" href="${esc(vurl)}" target="_blank" rel="noopener"`
        + ` title="${esc(vurl)}" data-vid="${esc(String(v.vid || ""))}">↗ 打开`
        + (v.vid ? `<span class="vid-code">${esc(String(v.vid).slice(-6))}</span>` : "")
        + `</a>`
      : `<span class="muted">—</span>`;
    const nProd = (v.products || []).length;
    const src = v.source === "ledger"
      ? `<span class="mini-tag src-ledger" title="台账有、抓取未覆盖（抓取失败/下架/补录）">台账</span>`
      : `<span class="mini-tag src-scrape">抓取</span>`;
    const selCls = bvSel.has(String(v.vid || "")) ? " tk-sel" : "";
    // 2026-10-22 UI 整改：12 列挤在 1098px 里，时间不再带秒（原 `2026-10-14 14:20:07` 占 143px）
    const pub = v.publish_time ? String(v.publish_time).replace("T", " ").slice(0, 16) : "—";
    return `<tr data-vid="${esc(String(v.vid || ""))}" class="${selCls.trim()}">
      <td class="acc"><button type="button" class="tk-exp-btn" data-tk-exp aria-expanded="false"
            aria-label="展开该视频详情" title="展开详情">▸</button>${esc(v.account || "")}</td>
      <td title="${esc(v.publish_time || "")}">${esc(pub)}</td>
      <td class="num">${fmtNum(v.plays)}</td>
      <td class="num">${fmtNum(v.likes)}</td>
      <td class="num">${fmtNum(v.comments)}</td>
      <td class="num">${fmtNum(v.shares)}</td>
      <td class="num">${fmtNum(v.collects)}</td>
      <td class="num">${v.engagement != null ? v.engagement + "%" : "—"}</td>
      <td class="num">${nProd ? `<span class="mini-tag">${nProd}</span>` : "—"}</td>
      <td>${src}</td>
      <td class="vidcol">${vcell}</td>
      <td title="${esc(v.desc || v.title || "")}">${link}</td>
    </tr>`;
  }).join("");

  table.innerHTML = head + `<tbody>${body}</tbody>`;

  /* ── 表现层增强（tablekit）────────────────────────────── */
  TKV.sortable(table, (i) => bvSortBy(i), { skip: [10, 11] });
  const sp = bvSortParts();
  TKV.setSort(table, BV_SORT_COLS.indexOf(sp.key), sp.dir);
  TKV.resizable(table, "bv-videos-v2", { min: 64,
    // 2026-10-22：固定列宽，把宽度让给标题列（原来 12 列平均分配，标题列只剩 260px）
    widths: [116, 104, 64, 58, 56, 56, 56, 64, 52, 56, 56, 0] });
  TKV.expandable(table.tBodies[0], {
    colspan: 12,
    rowOf: (tr) => bvRowsCache.find((v) => String(v.vid || "") === tr.getAttribute("data-vid")),
    html: (v) => bvDetailHtml(v),
  });
  bvBindSelect(table);
  if (!bvQuiet) TKV.reveal(table.tBodies[0]);   // 增量刷新不重播逐行入场
  TKV.pager($("#bv-pager"), {
    total: rows.length, page, size, unit: "条",
    onChange: (p, s) => { bvState.page = p; if (s) bvState.size = s; renderBenchVideos(); },
  });
  renderBvViz();
  renderBvCompare();
  restoreTop();
}

/* 表头点击排序：同一列再点切方向；其它列先降序（视频分析默认先看高值） */
function bvSortBy(i) {
  const key = BV_SORT_COLS[i];
  if (!key) return;
  const cur = bvSortParts();
  const dir = cur.key === key && cur.dir === -1 ? "asc" : "desc";
  bvState.sort = `${key}_${dir}`;
  bvState.page = 1;
  bvSyncSortSelect();
  renderBenchVideos();
}

/* 下拉 #bv-sort 与表头排序保持同一事实来源：下拉里没有的列临时补一项 */
function bvSyncSortSelect() {
  const sel = $("#bv-sort");
  if (!sel) return;
  if (!Array.from(sel.options).some((o) => o.value === bvState.sort)) {
    const sp = bvSortParts();
    const o = document.createElement("option");
    o.value = bvState.sort;
    o.textContent = `${BV_SORT_LABEL[sp.key] || sp.key} ${sp.dir === 1 ? "低→高" : "高→低"}`;
    o.dataset.tmp = "1";
    sel.appendChild(o);
    Array.from(sel.options).forEach((x) => { if (x.dataset.tmp === "1" && x.value !== bvState.sort) x.remove(); });
  }
  sel.value = bvState.sort;
}

/* 行展开详情：完整文案 / 标签 / 商品 / 链接，全部来自该条视频自身字段 */
function bvDetailHtml(v) {
  if (!v) return "";
  const url = String(v.url || "").trim()
    || (v.vid ? `https://www.tiktok.com/@${v.account || ""}/video/${v.vid}` : "");
  const desc = String(v.desc || v.title || "");
  const tags = (desc.match(/#[\w\u4e00-\u9fa5]+/g) || []).slice(0, 14);
  const prods = v.products || [];
  const prodHtml = prods.length
    ? prods.map((p) => {
        const t = p.title || p.product_id || "商品";
        const id = p.product_id ? `<span class="muted"> · ${esc(p.product_id)}</span>` : "";
        return `<div>${esc(t)}${id}</div>`;
      }).join("")
    : `<span class="muted">未挂车</span>`;
  return `<div class="tk-detail">
    <div class="full"><div class="k">标题 / 文案</div><div class="v">${esc(v.title || desc || "—")}</div></div>
    <div class="full"><div class="k">完整文案</div><div class="v">${esc(desc || "—")}</div></div>
    <div><div class="k">话题标签</div><div class="v">${tags.length ? tags.map((t) => `<span class="mini-tag">${esc(t)}</span>`).join(" ") : `<span class="muted">无</span>`}</div></div>
    <div><div class="k">视频 ID</div><div class="v" style="font-variant-numeric:tabular-nums">${esc(v.vid || "—")}</div></div>
    <div><div class="k">原始链接</div><div class="v">${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(url)}</a>` : "—"}</div></div>
    <div><div class="k">挂车商品 · ${prods.length}</div><div class="v">${prodHtml}</div></div>
    <div><div class="k">数据来源</div><div class="v">${v.source === "ledger" ? "台账补录（抓取未覆盖）" : "抓取快照"}</div></div>
    <div><div class="k">互动拆分</div><div class="v">赞 ${fmtNum(v.likes)} · 评 ${fmtNum(v.comments)} · 转 ${fmtNum(v.shares)} · 藏 ${fmtNum(v.collects)}</div></div>
  </div>`;
}

/* 行选择（多选 → 对比条）；点在链接 / 按钮上不触发选择 */
function bvBindSelect(table) {
  if (table.dataset.bvSel === "1") return;
  table.dataset.bvSel = "1";
  table.addEventListener("click", (ev) => {
    const tr = ev.target.closest ? ev.target.closest("tbody tr") : null;
    if (!tr || tr.classList.contains("tk-exp")) return;
    if (ev.target.closest("a,button,input,select,label")) return;
    const vid = tr.getAttribute("data-vid");
    if (!vid) return;
    if (bvSel.has(vid)) {
      bvSel.delete(vid);
      tr.classList.remove("tk-sel");
    } else {
      // 第五次选择直接拦下并告知：选中集合 = 参与计算的对象，不能「高亮了却不算」
      if (bvSel.size >= BV_CMP_MAX) {
        toast(`对比最多 ${BV_CMP_MAX} 条，请先取消一条再选`);
        return;
      }
      bvSel.add(vid);
      tr.classList.add("tk-sel");
    }
    renderBvCompare();
  });
}

function renderBvCompare() {
  const host = $("#bv-cmp");
  if (!host) return;
  // 上限已在选中入口（bvBindSelect）拦截，这里只保留防御性裁剪，
  // 保证「行高亮数 = 计数文案 = 对比列数」三者恒等。
  const picks = bvRowsCache.filter((v) => bvSel.has(String(v.vid || ""))).slice(0, BV_CMP_MAX);
  // 表头用「账号 · 标题片段」，避免同账号多条时分不清是哪一条
  TKV.compare(host, picks, null, (v) => {
    const t = String(v.title || v.desc || "").replace(/\s+/g, " ").slice(0, 14);
    return `${v.account || "—"}${t ? " · " + t : ""}`;
  });
}

/* 选中项清除（对比条上的「清除选择」回调） */
window.tkClearSel = function () {
  bvSel.clear();
  if (typeof renderBenchVideos === "function") renderBenchVideos();
};

/* ── 三图联动：播放分档 / 账号占比 / 近 14 天播放 ──
   每张图用「去掉自身过滤」的集合算分布，这样选中某段后其它段仍可切换；
   点击后回写 bvState 并重渲染表格（表格 → 图 与 图 → 表格 双向一致）。 */
function bvToggleFilter(key, val) {
  bvState[key] = bvState[key] === val ? "" : val;
  bvState.page = 1;
  renderBenchVideos();
}

/* 近 14 天摘要：峰值（含日期）+ 日均，替掉原来只有"点端点筛选当天"的空提示 */
function daySummary(dayData) {
  const vals = (dayData || []).map((d) => Number(d.value) || 0);
  if (!vals.length) return "近 14 天";
  const total = vals.reduce((a, b) => a + b, 0);
  let mi = 0;
  vals.forEach((v, i) => { if (v > vals[mi]) mi = i; });
  const d = dayData[mi] || {};
  return `峰值 ${baNum(vals[mi])}（${d.label || d.key || ""}）· 日均 ${baNum(Math.round(total / vals.length))}`;
}

/* 容器宽度变化时按新宽度重画（SVG 用真实像素，不靠 CSS 缩放，字号才稳定） */
let bvVizW = 0;
let bvVizRO = null;
function ensureBvVizResize() {
  const box = $("#bv-viz");
  if (!box || bvVizRO || typeof ResizeObserver === "undefined") return;
  bvVizRO = new ResizeObserver(() => {
    const w = box.clientWidth;
    if (!w || Math.abs(w - bvVizW) < 24 || !box.offsetParent) return;
    bvVizW = w;
    renderBvViz();
  });
  bvVizRO.observe(box);
}

function renderBvViz() {
  const box = $("#bv-viz");
  if (!box) return;
  const bandRows = bvFiltered({ band: "" });
  const acctRows = bvFiltered({ acct: "" });
  const dayRows = bvFiltered({ day: "" });

  const bandData = BV_BANDS.map((b) => ({ key: b.key, label: b.label, value: bandRows.filter(b.test).length }));

  const byAcct = new Map();
  acctRows.forEach((v) => {
    const k = v.account || "—";
    byAcct.set(k, (byAcct.get(k) || 0) + (v.plays || 0));
  });
  let acctData = [...byAcct.entries()].sort((a, b) => b[1] - a[1]);
  if (acctData.length > 6) {
    const rest = acctData.slice(6).reduce((s, x) => s + x[1], 0);
    acctData = acctData.slice(0, 6).concat([["__other", rest]]);
  }
  const acctTotal = acctRows.reduce((s, v) => s + (v.plays || 0), 0);

  const tsList = dayRows.map(bvTime).filter((t) => t > 0);
  const end = tsList.length ? Math.max.apply(null, tsList) : Date.now();
  const dayData = [];
  for (let i = 13; i >= 0; i--) {
    const key = bvDayKey(end - i * 864e5);
    const val = dayRows
      .filter((v) => bvDayKey(bvTime(v)) === key)
      .reduce((s, v) => s + (v.plays || 0), 0);
    dayData.push({ key, label: key.slice(5), value: val });
  }

  const chip = (key, label) =>
    bvState[key]
      ? `<span class="tk-filter-tag" title="点击清除该条件">${esc(label)}
           <button type="button" data-clr="${key}" aria-label="清除该筛选">×</button></span>`
      : "";

  box.innerHTML = `
    <div class="tk-viz-card">
      <div class="tk-viz-h"><b>播放量分档</b>
        <span class="muted">${bandRows.length} 条</span>
        ${chip("band", (BV_BANDS.find((b) => b.key === bvState.band) || {}).label || "")}</div>
      <div class="tk-viz-body" id="bv-viz-band" title="点档位可按该区间筛选"></div>
    </div>
    <div class="tk-viz-card">
      <div class="tk-viz-h"><b>账号播放占比</b>
        <span class="muted">${acctRows.length} 条</span>
        ${chip("acct", bvState.acct)}</div>
      <div class="tk-viz-body" id="bv-viz-acct" title="点环形或图例可按账号筛选"></div>
    </div>
    <div class="tk-viz-card">
      <div class="tk-viz-h"><b>近 14 天播放</b>
        <span class="muted">${daySummary(dayData)}</span>
        ${chip("day", bvState.day ? bvState.day.slice(5) : "")}</div>
      <div class="tk-viz-body" id="bv-viz-day" title="点端点可按当天筛选"></div>
    </div>`;

  TKV.bands($("#bv-viz-band"), bandData, {
    activeKey: bvState.band,
    animate: !bvQuiet && !prefersReducedMotion(),
    valueFmt: (v) => v + " 条",
    onPick: (k) => bvToggleFilter("band", k),
  });
  TKV.donut($("#bv-viz-acct"), acctData.map(([k, v]) => ({ key: k, label: k === "__other" ? "其他" : k, value: v })), {
    activeKey: bvState.acct,
    animate: !bvQuiet && !prefersReducedMotion(),
    valueFmt: (v) => fmtNum(v) + " 播放",
    centerValue: fmtNum(acctTotal),
    centerLabel: "总播放",
    onPick: (k) => { if (k !== "__other") bvToggleFilter("acct", k); },
  });
  // 折线按容器实际宽高绘制（而不是 520×132 被 CSS 拉伸）：
  // 固定 viewBox 缩放会把 9.5px 的轴标签压到 7px，并留下半张卡片的空白。
  const dayEl = $("#bv-viz-day");
  const dayW = Math.max(280, Math.floor(dayEl.clientWidth || 460));
  const dayH = Math.max(140, Math.floor(dayEl.clientHeight || 168));
  TKV.line(dayEl, dayData, {
    width: dayW,
    height: dayH,
    activeKey: bvState.day,
    animate: !bvQuiet && !prefersReducedMotion(),
    showAxis: true,
    valueFmt: (v) => fmtNum(v) + " 播放",
    onPick: (k) => bvToggleFilter("day", k),
  });
  bvVizW = box.clientWidth;
  ensureBvVizResize();

  box.querySelectorAll("[data-clr]").forEach((b) =>
    b.addEventListener("click", () => {
      bvState[b.getAttribute("data-clr")] = "";
      bvState.page = 1;
      renderBenchVideos();
    }));

  // 交叉淡入（方案 A 增强）：仅「静默刷新」时给新图做一次 120ms 淡入，
  // 让数据变化有「更新了」的感知，又不产生位移与入场动画。（用户操作/首屏走原入场动画，不叠加）
  if (bvQuiet && !prefersReducedMotion()) {
    box.classList.remove("bv-fading");
    // 强制回流后重新加类，保证连续两次静默刷新都能重播这段过渡
    void box.offsetWidth;
    box.classList.add("bv-fading");
  }
}

/* P2-8：视频明细搜索原来每敲一个字符就全量重算（737 条 ×3 张图 + 重建表格 + 横滚复位）；
   账号搜索早有 150ms 防抖，这里统一 250ms */
let bvQTimer = null;
function bvChangeDebounced(key, value) {
  clearTimeout(bvQTimer);
  bvQTimer = setTimeout(() => bvChange(key, value), 250);
}

function bvChange(key, value) {
  bvState[key] = key === "minPlays" ? Number(value) || 0 : value;
  bvState.page = 1;   // 换筛选条件回到第一页
  renderBenchVideos();
}

/* 兼容旧入口：等价「下一页」（分页条接管后仍保留，便于脚本/测试调用） */
function bvMore() {
  const pages = Math.max(1, Math.ceil(bvRowsCache.length / (bvState.size || BV_PAGE)));
  if (bvState.page < pages) {
    bvState.page += 1;
    renderBenchVideos();
  }
}

function bvReset() {
  bvState = { q: "", days: "", minPlays: 0, prod: "", src: "", sort: "plays_desc",
              size: BV_PAGE, page: 1, band: "", acct: "", day: "" };
  bvSel.clear();
  [["#bv-q", ""], ["#bv-days", ""], ["#bv-plays", "0"], ["#bv-prod", ""], ["#bv-src", ""],
   ["#bv-sort", "plays_desc"]]
    .forEach(([sel, val]) => { const e = $(sel); if (e) e.value = val; });
  renderBenchVideos();
}

function bvExportCsv() {
  const rows = bvFiltered();
  if (!rows.length) { toast("当前筛选没有可导出的视频"); return; }
  const esc_ = (v) => `"${String(v == null ? "" : v).replace(/"/g, '""')}"`;
  const head = ["账号", "发布时间", "视频ID", "播放", "点赞", "评论", "转发", "收藏",
                "互动率%", "挂车数", "链接", "标题", "文案"];
  const lines = [head.map(esc_).join(",")].concat(rows.map((v) => [
    v.account, v.publish_time, v.vid, v.plays, v.likes, v.comments, v.shares, v.collects,
    v.engagement, (v.products || []).length, v.url, v.title, v.desc,
  ].map(esc_).join(",")));
  const csv = "\uFEFF" + lines.join("\r\n");
  try {
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `视频明细_${today()}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    toast(`已导出 ${rows.length} 条视频`);
  } catch (e) {
    toast("导出失败：" + e.message);
  }
}

/* ─────────── 图表（与视频明细同源，纯 SVG 零依赖）─────────── */
/* 2026-10-22 UI 整改：趋势折线原来用 teal（第二个强调色，与 v2「唯一强调色」自相矛盾），
   改为深墨，与琥珀柱形成明度差而不是色相差。 */
const BV_PRI = "#b45309", BV_TEAL = "#1c1917", BV_INK = "#1c1917", BV_SUB = "#78716c";

function bvDayKey(ts) {
  const d = new Date(ts);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// 每日发布条数 + 当日发布视频的播放合计（近 N 天；0 = 全部区间）
function bvTrendSeries(days) {
  const all = ((benchData && benchData.videos) || []).filter((v) => bvTime(v));
  if (!all.length) return [];
  const times = all.map(bvTime);
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const n = Number(days) || 0;
  const start = n ? today.getTime() - (n - 1) * 864e5 : Math.min(...times);
  const end = Math.max(today.getTime(), Math.max(...times));
  const buckets = new Map();
  all.forEach((v) => {
    const t = bvTime(v);
    if (t < start) return;
    const k = bvDayKey(t);
    const b = buckets.get(k) || { count: 0, plays: 0 };
    b.count += 1;
    b.plays += v.plays || 0;
    buckets.set(k, b);
  });
  const out = [];
  for (let t = start; t <= end; t += 864e5) {
    const k = bvDayKey(t);
    const b = buckets.get(k) || { count: 0, plays: 0 };
    out.push({ date: k, count: b.count, plays: b.plays });
  }
  return out;
}

/* P2-2：趋势图 / 账号条形图原来写死 560 宽靠 CSS 缩放 —— 390px 屏上 9px 轴标签实际只有 5.2px，
   桌面宽屏又被放大。改为按容器实测宽度绘制 + ResizeObserver 重绘（与三图联动同一做法）。 */
let benchChartRO = null;
const _bcW = { trend: 0, acc: 0 };
function ensureBenchChartResize() {
  if (benchChartRO || typeof ResizeObserver === "undefined") return;
  benchChartRO = new ResizeObserver(() => {
    const t = $("#bv-trend"), a = $("#bv-accchart");
    const tw = t ? t.clientWidth : 0, aw = a ? a.clientWidth : 0;
    if (tw && Math.abs(tw - _bcW.trend) >= 24) { _bcW.trend = tw; renderBenchTrend(); }
    if (aw && Math.abs(aw - _bcW.acc) >= 24) { _bcW.acc = aw; renderBenchAccChart(); }
  });
  const t = $("#bv-trend"), a = $("#bv-accchart");
  if (t) benchChartRO.observe(t);
  if (a) benchChartRO.observe(a);
}

function renderBenchTrend() {
  const box = $("#bv-trend");
  if (!box) return;
  const rangeEl = $("#bv-trend-range");
  const s = bvTrendSeries(rangeEl ? rangeEl.value : 30);
  if (!s.length) {
    box.innerHTML = `<div class="muted">暂无带发布时间的视频</div>`;
    return;
  }
  const W = Math.max(320, Math.floor(box.clientWidth || 560));
  const H = 200, padL = 38, padR = 42, padT = 12, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;
  const maxC = Math.max(1, ...s.map((x) => x.count));
  const maxP = Math.max(1, ...s.map((x) => x.plays));
  const bw = iw / s.length;
  const y = (v, max) => padT + ih - (v / max) * ih;

  const bars = s.map((x, i) => {
    const h = Math.max(0, (x.count / maxC) * ih);
    const bx = padL + i * bw + bw * 0.18;
    return `<rect x="${bx.toFixed(1)}" y="${(padT + ih - h).toFixed(1)}" width="${Math.max(1, bw * 0.64).toFixed(1)}" ` +
      `height="${h.toFixed(1)}" rx="1.5" fill="${BV_PRI}" opacity=".82">` +
      `<title>${x.date}：${x.count} 条 · ${fmtNum(x.plays)} 播放</title></rect>`;
  }).join("");
  const line = s.map((x, i) => `${(padL + i * bw + bw / 2).toFixed(1)},${y(x.plays, maxP).toFixed(1)}`).join(" ");
  const dots = s.filter((x) => x.count).map((x, i) => {
    const idx = s.indexOf(x);
    return `<circle cx="${(padL + idx * bw + bw / 2).toFixed(1)}" cy="${y(x.plays, maxP).toFixed(1)}" r="2" fill="${BV_TEAL}"><title>${x.date}：${fmtNum(x.plays)} 播放</title></circle>`;
  }).join("");
  const step = Math.max(1, Math.ceil(s.length / 6));
  const xlabels = s.map((x, i) => (i % step === 0 || i === s.length - 1)
    ? `<text x="${(padL + i * bw + bw / 2).toFixed(1)}" y="${H - 8}" font-size="9" fill="${BV_SUB}" text-anchor="middle">${x.date.slice(5)}</text>`
    : "").join("");
  // 2026-10-22：补 3 条网格线 + 左右刻度（原来整图只有一个 max 孤值，读不出量级）
  const grid = [0.25, 0.5, 0.75].map((f) => {
    const gy = (padT + ih - f * ih).toFixed(1);
    return `<line x1="${padL}" y1="${gy}" x2="${W - padR}" y2="${gy}" stroke="rgba(28,25,23,.07)"/>` +
      `<text x="${padL - 6}" y="${(Number(gy) + 3).toFixed(1)}" font-size="9" fill="${BV_SUB}" text-anchor="end">${Math.round(maxC * f)}</text>` +
      `<text x="${W - padR + 6}" y="${(Number(gy) + 3).toFixed(1)}" font-size="9" fill="${BV_SUB}" text-anchor="start">${fmtNum(maxP * f)}</text>`;
  }).join("");
  const axis = grid +
    `<line x1="${padL}" y1="${padT + ih}" x2="${W - padR}" y2="${padT + ih}" stroke="rgba(28,25,23,.18)"/>` +
    `<text x="${padL - 6}" y="${padT + 8}" font-size="9" fill="${BV_SUB}" text-anchor="end">${maxC} 条</text>` +
    `<text x="${W - padR + 6}" y="${padT + 8}" font-size="9" fill="${BV_SUB}" text-anchor="start">${fmtNum(maxP)}</text>`;

  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="bv-svg" role="img" aria-label="每日发布与播放趋势">${axis}${bars}` +
    `<polyline points="${line}" fill="none" stroke="${BV_TEAL}" stroke-width="1.6"/>${dots}${xlabels}</svg>` +
    `<div class="bv-legend"><span><i class="sw" style="background:${BV_PRI}"></i>发布条数</span>` +
    `<span><i class="sw" style="background:${BV_TEAL}"></i>当日发布视频播放合计</span></div>`;
  _bcW.trend = box.clientWidth || 0;
  ensureBenchChartResize();
}

const BV_METRICS = {
  total_plays: { label: "总播放", fmt: (v) => fmtNum(v) },
  avg_plays: { label: "平均播放", fmt: (v) => fmtNum(v) },
  videos: { label: "视频数", fmt: (v) => String(v) },
  avg_engagement: { label: "平均互动率", fmt: (v) => v + "%" },
  product_coverage: { label: "挂车率", fmt: (v) => v + "%" },
};

/* 2026-10-22 独立验收 R2：0 / 未知 / 微小正值必须分开。
   旧实现 `r[key] || 0` 把 null 混成 0，`Math.max(6, …)` 又把 0 抬成 6px 实心条，
   于是「0% 挂车率」也画出一条正宽条形，与 #8「0% 不画空条」自相矛盾。
   现在：null/undefined/""/NaN → 未知（不画条、显示「未知」）；
   0 → 不画条、显示 0；仅 >0 的有效值才吃 6px 可见性下限。 */
const BV_BAR_MIN = 6;
function bvMetricVal(row, key) {
  if (!row) return null;
  const v = row[key];
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function renderBenchAccChart() {
  const box = $("#bv-accchart");
  if (!box) return;
  const sel = $("#bv-metric");
  const key = (sel && sel.value) || "total_plays";
  const meta = BV_METRICS[key] || BV_METRICS.total_plays;
  const rows = ((benchData && benchData.accounts) || []).slice();
  if (!rows.length) {
    box.innerHTML = `<div class="muted">当前范围暂无账号数据</div>`;
    return;
  }
  /* 账号表展示全量，图只画 TOP12：选中 12 名之外的账号时，若只把其余条形变暗，
     就变成「有选择、无对应条」的静默失联。这里把所选账号临时并入图内
     （末行、标注全量名次、虚线分隔），保证「选中必有对应条」。 */
  const BA_TOP = 12;
  // 未知值排最后，不让 null 参与大小比较（旧版 `b[key] || 0` 会把未知当 0 排在 0 里）
  const ranked = rows.slice().sort((a, b) => {
    const va = bvMetricVal(a, key), vb = bvMetricVal(b, key);
    return (vb == null ? -Infinity : vb) - (va == null ? -Infinity : va);
  });
  let top = ranked.slice(0, BA_TOP);
  let pinned = null;
  let selMissing = false;
  if (baActive) {
    const idx = ranked.findIndex((r) => r.account === baActive);
    if (idx < 0) selMissing = true;
    else if (idx >= BA_TOP) {
      pinned = Object.assign({}, ranked[idx], { __rank: idx + 1 });
      top = top.concat([pinned]);
    }
  }
  const W = Math.max(320, Math.floor(box.clientWidth || 560));
  const rowH = 24, padT = 6, labelW = 128, valW = 62;
  const positives = top.map((r) => bvMetricVal(r, key)).filter((v) => v != null && v > 0);
  const max = positives.length ? Math.max(...positives) : 0;
  const avail = Math.max(1, W - labelW - valW - 8);
  const H = padT * 2 + top.length * rowH;
  const bars = top.map((r, i) => {
    const v = bvMetricVal(r, key);
    const unknown = v == null;
    const visible = !unknown && v > 0;      // ← 只有正的有效值才占一条可见条
    const w = visible ? Math.max(BV_BAR_MIN, (v / max) * avail) : 0;
    const y = padT + i * rowH;
    const on = !!baActive && baActive === r.account;
    const dim = !!baActive && !on;
    const valTxt = unknown ? "未知" : meta.fmt(v);
    const title = unknown ? `${r.account}：未知（当前数据无该指标）` : `${r.account}：${meta.fmt(v)}`;
    // 0 与未知都不出 <rect>：避免「零值照样一条正宽实心条」；值标签仍照常显示 0 / 未知。
    return `<g class="ba-bar${on ? " on" : ""}${r.__rank ? " pin" : ""}" data-acc="${esc(r.account)}" role="button" tabindex="0"
        style="cursor:pointer;opacity:${dim ? ".38" : "1"}">
      ${r.__rank ? `<line x1="0" y1="${y}" x2="${W}" y2="${y}" stroke="rgba(28,25,23,.18)" stroke-dasharray="3 3"/>` : ""}
      <text x="0" y="${y + 15}" font-size="11" fill="${BV_INK}" font-family="var(--font-data)">${esc(r.account)}</text>
      ${visible ? `<rect x="${labelW}" y="${y + 4}" width="${w.toFixed(1)}" height="13" rx="3"
        fill="${BV_PRI}" opacity="${on ? "1" : ".8"}"${on ? ` stroke="${BV_INK}" stroke-width="1"` : ""}><title>${esc(title)}</title></rect>` : ""}
      <text x="${W - 2}" y="${y + 15}" font-size="11" fill="${BV_SUB}" text-anchor="end"${unknown ? ' class="ba-unknown"' : ""}>${esc(valTxt)}</text>
    </g>`;
  }).join("");
  const topN = Math.min(BA_TOP, ranked.length);
  const head = pinned ? `TOP ${topN} + 所选` : `TOP ${topN}`;
  const tail = !baActive ? " · 点条形可高亮"
    : selMissing ? ` · 已选 ${esc(baActive)}（当前范围无该账号数据）`
    : pinned ? ` · 已选 ${esc(baActive)}（全量第 ${pinned.__rank} 名，已并入末行）`
    : ` · 已选 ${esc(baActive)}`;
  const zeroNote = positives.length ? "" : " · 当前指标全部为 0 或未知，故不绘条形";
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="bv-svg" role="img" aria-label="账号${meta.label}对比">${bars}</svg>` +
    `<div class="bv-legend"><span>按${meta.label}排序 · ${head}${tail}${zeroNote}</span>` +
    `<span class="muted">0 与未知不画条 · 正值最小 ${BV_BAR_MIN}px</span></div>`;
  box.querySelectorAll(".ba-bar").forEach((g) => {
    const pick = () => {
      const acc = g.getAttribute("data-acc");
      baActive = baActive === acc ? "" : acc;
      // 重建表格（含 12 名外所选账号的 pinned 行，见方案 A）+ 高亮，再重绘条形图
      renderBenchAccounts((benchData && benchData.accounts) || []);
      renderBenchAccChart();
      const tr = document.querySelector(`#bench-accounts tbody tr[data-acct="${cssEsc(acc)}"]`);
      if (tr && baActive) tr.scrollIntoView({ block: "nearest" });
    };
    g.addEventListener("click", pick);
    g.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
  });
  _bcW.acc = box.clientWidth || 0;
  ensureBenchChartResize();
}

function renderPlayBuckets(buckets) {
  const box = $("#bench-play-buckets");
  if (!box) return;
  buckets = buckets || [];
  const max = Math.max(1, ...buckets.map((b) => b.count));
  box.innerHTML =
    `<h2>播放量分布</h2>` +
    buckets
      .map(
        (b) => `<div class="hbar">
          <span class="hbar-l">${b.label}</span>
          <div class="hbar-track"><i style="width:${Math.round(100 * b.count / max)}%"></i></div>
          <span class="hbar-v">${b.count} · ${b.pct}%</span>
        </div>`
      )
      .join("");
}

function renderTagCloud(sel, arr, isWords = false) {
  const box = $(sel);
  if (!box) return;
  if (!arr || !arr.length) {
    box.innerHTML = `<div class="muted">暂无数据</div>`;
    return;
  }
  const items = arr.map((x) => (typeof x === "string" ? { tag: x, count: "", pct: "" } : x));
  box.innerHTML = items
    .map(
      (x) =>
        `<span class="tag" title="${x.count != null ? x.count + " 次" : ""}">` +
        `${isWords ? "" : "#"}${esc(x.tag)}</span>`
    )
    .join("");
}

/* ─── 「被挂车最多的商品」：点击展开商品详情 + 跳转 FastMoss 商品页 ───
   数据全部来自后端 patterns.products_top（product_id / 挂车视频数 / 关联账号 /
   关联视频 / fastmoss_url），前端不臆造任何数字；缺字段时如实降级。 */

// FastMoss 视频详情页模板（来自 MCP fastmoss_detail_url_examples，非臆造）。
// 该页即「视频带货商品列表」：带商品 / 总销量 / 总销售额 / 商品分类 / 佣金比例 / 商品评分 ——
// 就是「视频出单量」要看的东西，所以「视频出单量」按钮直接跳这里。
const FASTMOSS_VIDEO_URL = "https://www.fastmoss.com/media-source/video/{vid}";
function renderBenchProducts(prods) {
  const box = $("#bench-products");
  if (!box) return;
  benchProductsCache = Array.isArray(prods) ? prods : [];
  if (!benchProductsCache.length) {
    box.innerHTML = `<div class="muted">暂无商品数据</div>`;
    return;
  }
  box.innerHTML = benchProductsCache
    .map((p, i) => {
      const title = (p.title || "").trim() || (p.product_id ? "商品 " + p.product_id : "未命名商品");
      const short = title.length > 46 ? title.slice(0, 46) + "…" : title;
      const fm = p.fastmoss_url || (p.product_id ? "https://www.fastmoss.com/e-commerce/detail/" + p.product_id : "");
      // 行尾 ↗ 是独立 <a>（不能嵌在 <button> 里 —— 交互元素不可嵌套）。
      // 这样"点一下商品就跳转"是真的一步到位，不必先展开。
      const jump = fm
        ? `<a class="prod-jump" data-fm="${esc(fm)}" data-open="${esc(fm)}" href="${esc(fm)}" target="_blank" rel="noopener"
              title="在 FastMoss 打开商品页" aria-label="在 FastMoss 打开商品页">FastMoss ↗</a>`
        : `<span class="prod-jump disabled" title="该商品缺少 product_id，无法生成 FastMoss 链接">无链接</span>`;
      return `<div class="prod-item" data-idx="${i}">
        <div class="prod-row">
          <button type="button" class="prod-head" data-idx="${i}" aria-expanded="false"
                  aria-controls="prod-detail-${i}" title="${esc(title)}">
            <span class="nm">${esc(short)}</span>
            <span class="tk-mini" title="占全部挂车视频 ${esc(p.pct)}%" aria-hidden="true">
              <i style="width:${Math.max(2, Math.min(100, Number(p.pct) || 0))}%"></i></span>
            <span class="sub">${esc(p.vids)} 条 · ${esc(p.pct)}%<i class="caret" aria-hidden="true">▾</i></span>
          </button>
          ${jump}
        </div>
        <div class="prod-detail" id="prod-detail-${i}" hidden></div>
      </div>`;
    })
    .join("");
  if (!benchProductsBound) {
    benchProductsBound = true;
    box.addEventListener("click", onBenchProductsClick);
  }
}

function onBenchProductsClick(e) {
  const t = e.target;
  if (!t || typeof t.closest !== "function") return;
  // data-open = 通用「点击打开外部链接」钩子（FastMoss 商品页 / 视频页 / TikTok 原视频都用它）；
  // data-fm 是 FastMoss 专用标记，兼容保留。取 URL 时两个属性都认。
  const jump = t.closest("[data-open]") || t.closest("[data-fm]");
  if (jump) {
    openFastmoss(jump.getAttribute("data-open") || jump.getAttribute("data-fm"), e);
    return;
  }
  const copyBtn = t.closest("[data-copy]");
  if (copyBtn) {
    copyText(copyBtn.dataset.copy, "商品ID 已复制");
    return;
  }
  const btn = t.closest(".prod-head");
  if (btn) toggleProdDetail(btn);
}

/* 打开 FastMoss 商品页。
   为什么不用纯 <a target="_blank">：内置预览面板 / 浏览器弹窗拦截会让它静默失败，
   表现为「点了没反应」。这里显式 window.open，失败时退回当前窗口导航（一定跳得了）。
   注意：window.open 若传 noopener 会恒返回 null，无法判断是否被拦截 —— 因此不带
   noopener 打开，成功后手动清掉 opener（等效安全）。 */
function openFastmoss(url, ev) {
  if (!url) return;
  // 中键 / Ctrl / Cmd / Shift 点击 → 交回浏览器原生行为（后台标签页等）
  if (ev && (ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey)) return;
  if (ev) {
    ev.preventDefault();
    if (ev.stopPropagation) ev.stopPropagation();
  }
  let win = null;
  try {
    win = window.open(url, "_blank");
  } catch (err) {
    win = null;
  }
  if (win) {
    try { win.opener = null; } catch (err) { /* 跨域时忽略 */ }
    return;
  }
  toast("新窗口被拦截，已在当前页面打开 FastMoss");
  fastmossFallback(url);
}

/* 单独抽出便于测试替换（window.open 被拦截时的兜底） */
function fastmossFallback(url) {
  window.location.href = url;
}

function copyText(text, okMsg) {
  if (!text) return;
  try {
    navigator.clipboard.writeText(text).then(
      () => toast(okMsg || "已复制"),
      () => toast("复制失败，请手动选中")
    );
  } catch (err) {
    toast("复制失败，请手动选中");
  }
}

function toggleProdDetail(btn) {
  const wrap = btn.closest(".prod-item");
  if (!wrap) return;
  const detail = wrap.querySelector(".prod-detail");
  if (btn.getAttribute("aria-expanded") === "true") {
    btn.setAttribute("aria-expanded", "false");
    wrap.classList.remove("open");
    if (detail) detail.hidden = true;
    return;
  }
  // 手风琴：同时只展开一条，避免面板被撑长
  $$("#bench-products .prod-head[aria-expanded='true']").forEach((o) => {
    o.setAttribute("aria-expanded", "false");
    const w = o.closest(".prod-item");
    if (w) {
      w.classList.remove("open");
      const d = w.querySelector(".prod-detail");
      if (d) d.hidden = true;
    }
  });
  if (detail) {
    if (!detail.dataset.filled) {
      detail.innerHTML = prodDetailHtml(benchProductsCache[Number(btn.dataset.idx)] || {});
      detail.dataset.filled = "1";
    }
    detail.hidden = false;
  }
  btn.setAttribute("aria-expanded", "true");
  wrap.classList.add("open");
}

function prodDetailHtml(p) {
  const title = (p.title || "").trim();
  const pid = p.product_id ? String(p.product_id) : "";
  const fm = p.fastmoss_url || (pid ? "https://www.fastmoss.com/e-commerce/detail/" + pid : "");
  const accounts = Array.isArray(p.accounts) ? p.accounts : [];
  const videos = Array.isArray(p.videos) ? p.videos : [];

  const rows = [
    `<div class="pd-row"><span class="pd-k">商品ID</span><span class="pd-v mono">${esc(pid || "—")}</span></div>`,
    `<div class="pd-row"><span class="pd-k">挂车视频</span><span class="pd-v">${esc(p.vids)} 条（占挂车视频 ${esc(p.pct)}%）</span></div>`,
    `<div class="pd-row"><span class="pd-k">关联账号</span><span class="pd-v">${
      accounts.length ? accounts.map((a) => `<span class="mini-tag">${esc(a)}</span>`).join(" ") : "—"
    }</span></div>`,
    `<div class="pd-row"><span class="pd-k">关联播放</span><span class="pd-v">${
      p.total_plays === null || p.total_plays === undefined ? "—" : fmtNum(p.total_plays) + "（挂车视频合计）"
    }</span></div>`,
  ];
  if (p.anchor_type !== null && p.anchor_type !== undefined) {
    rows.push(`<div class="pd-row"><span class="pd-k">锚点类型</span><span class="pd-v mono">${esc(p.anchor_type)}</span></div>`);
  }

  // 关联视频：标题本身可点击 → 打开 TikTok 原视频（与商品跳转共用 openFastmoss，
  // 同样享受"弹窗被拦截则退回当前窗口"的兜底）。
  const vlist = videos
    .map((v) => {
      const vt = (v.title || "").slice(0, 44) || v.vid || "—";
      const tHtml = v.url
        ? `<a class="pvd-t" data-open="${esc(v.url)}" href="${esc(v.url)}" target="_blank" rel="noopener" title="${esc(v.title)}（点击打开原视频）">${esc(vt)}</a>`
        : `<span class="pvd-t" title="${esc(v.title)}">${esc(vt)}</span>`;
      const meta = [
        esc(v.account || "—"),
        fmtNum(v.plays || 0) + " 播放",
        v.publish_time ? esc(v.publish_time) : "",
      ].filter(Boolean).join(" · ");
      const acts = [];
      // FastMoss 视频详情页 = 该视频的「视频带货商品列表 / 总销量 / 总销售额」→ 即出单量。
      // 只需要 vid（TikTok 视频 ID），所以没有抓到 url 的视频也能查。
      const fmv = v.vid ? FASTMOSS_VIDEO_URL.replace("{vid}", encodeURIComponent(v.vid)) : "";
      if (fmv) {
        acts.push(`<button type="button" class="pvd-sales" data-open="${esc(fmv)}"
          title="在 FastMoss 打开该视频页，查看视频出单量（视频带货商品列表 · 总销量 / 总销售额）">视频出单量</button>`);
      }
      if (v.url) {
        acts.push(`<a class="pvd-a" data-open="${esc(v.url)}" href="${esc(v.url)}" target="_blank" rel="noopener">TikTok ↗</a>`);
      }
      return `<li class="${v.url ? "clickable" : ""}">
        ${tHtml}
        <div class="pvd-m">
          <span class="pvd-meta">${meta}</span>
          <span class="pvd-acts">${acts.join("")}</span>
        </div>
      </li>`;
    })
    .join("");

  return `
    <div class="pd-title">${esc(title || "（该商品无标题）")}</div>
    ${rows.join("")}
    <div class="pd-videos-t">关联视频（按播放降序，展示 ${videos.length} 条 · 点标题开原视频 · 点「视频出单量」看该视频 FastMoss 出单数据）</div>
    ${vlist ? `<ul class="pd-videos">${vlist}</ul>` : `<div class="muted">无关联视频明细</div>`}
    <div class="pd-actions">
      ${fm
        ? `<a class="btn primary sm" data-fm="${esc(fm)}" data-open="${esc(fm)}" href="${esc(fm)}" target="_blank" rel="noopener">在 FastMoss 打开商品页 ↗</a>`
        : `<span class="muted">该商品缺少 product_id，无法生成 FastMoss 链接</span>`}
      ${pid ? `<button type="button" class="btn ghost sm" data-copy="${esc(pid)}">复制商品ID</button>` : ""}
    </div>`;
}

function renderTopVideos(videos) {
  const box = $("#bench-top-videos");
  if (!box) return;
  if (!videos || !videos.length) {
    box.innerHTML = `<div class="muted">暂无数据</div>`;
    return;
  }
  const vmax = Math.max(1, ...videos.map((v) => Number(v.plays) || 0));
  box.innerHTML = videos
    .map((v, i) => {
      const vt = v.title || "";  // 后端 title 可能为 null，直接 .length 会中断整块渲染（P2-13）
      const w = Math.max(2, Math.round((Number(v.plays) || 0) / vmax * 100));
      return `<div class="item tk-topv" style="--i:${Math.min(i, 12)}">
        <span class="nm" title="${esc(vt)}">${esc(vt.length > 44 ? vt.slice(0, 44) + "…" : vt) || "—"}</span>
        <span class="tk-mini wide" aria-hidden="true"><i style="width:${w}%"></i></span>
        <span class="sub">${esc(v.account)} · ${fmtNum(v.plays)} 播放</span>
      </div>
      ${v.url ? `<div class="muted topv-link"><a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.url)}</a></div>` : ""}`;
    })
    .join("");
}

function renderTakeaways(items) {
  const box = $("#bench-takeaways");
  if (!box) return;
  if (!items || !items.length) {
    box.innerHTML = `<div class="muted">暂无可推导结论</div>`;
    return;
  }
  box.innerHTML = items
    .map(
      (t) => `<div class="chk-item ${t.confidence === "启发式" ? "info" : "ok"}" style="margin-top:8px">
        <b>[${esc(t.confidence)}]</b> ${esc(t.text)}
        <div class="card-meta" style="margin-top:3px">依据：${esc(t.basis)}</div>
      </div>`
    )
    .join("");
}

function copyBenchReport() {
  if (!benchData || !benchData.kpis) return toast("暂无可复制的分析数据");
  if (!benchData) return toast("请先加载对标分析");
  const d = benchData;
  const k = d.kpis;
  const lines = [
    "# 对标分析结论（自动生成）",
    `生成时间：${new Date().toLocaleString("zh-CN")}`,
    `数据源：scrape_*.json + *_products.json + 台账（${k.videos} 条视频 / ${k.accounts} 账号）`,
    "",
    "## 关键指标",
    `- 总播放量 ${fmtNum(k.total_plays)}，平均互动率 ${pctText(k.avg_engagement)}，挂车率 ${pctText(k.product_coverage)}`,
    `- 播放最高账号：${(k.top_accounts_by_plays || []).join("、")}`,
    "",
    "## 爆款规律",
    ...((d.patterns || {}).play_buckets || []).map((b) => `- 播放 ${b.label}：${b.count} 条（${b.pct}%）`),
    `- 高频标签：${(((d.patterns || {}).top_tags) || []).map((t) => "#" + t.tag).join(" ")}`,
    `- 文案钩子：${(((d.patterns || {}).hook_words) || []).join(", ")}`,
    "",
    "## 可借鉴点",
    ...(d.takeaways || []).map((t) => `- [${t.confidence}] ${t.text}（依据：${t.basis}）`),
    "",
    "## 数据边界",
    `- 出单/GMV：${(d.data_sources || {}).gmv_status || "—"}`,
  ];
  const text = lines.filter((l) => l !== false).join("\n");
  navigator.clipboard.writeText(text).then(
    () => toast("结论已复制到剪贴板，可粘贴给任意 AI 分析"),
    () => toast("复制失败，请手动选择")
  );
}

/* ─────────── 报表与台账 ─────────── */
async function loadReports() {
  let d;
  try {
    d = await api("/api/reports");
  } catch (e) {
    // P2-5：失败静默时页面停在初始文案，与"reports/ 暂无日期目录"无法区分
    const msg = apiErrMsg(e);
    const dayBox = $("#report-days");
    if (dayBox) {
      dayBox.innerHTML = `<div class="muted">报表列表读取失败：${esc(msg)}
        <button class="btn ghost sm" onclick="loadReports()">重试</button></div>`;
    }
    return toast("报表列表读取失败：" + msg, "error");
  }
  const dayBox = $("#report-days");
  dayBox.innerHTML = d.days.length
    ? d.days
        .map(
          (x) => `<div class="item">
            <span class="nm" onclick="openReport(${esc(JSON.stringify(x.path))}, ${esc(JSON.stringify(x.date))})">${x.date}</span>
            <span class="sub">${x.files.join(" · ")}</span>
          </div>`
        )
        .join("")
    : `<div class="muted">reports/ 暂无日期目录</div>`;

  $("#benchmark-list").innerHTML = d.benchmark_reports.length
    ? d.benchmark_reports
        .map(
          (x) => `<div class="item">
            <span class="nm" onclick="openReport(${esc(JSON.stringify(x.path))}, ${esc(JSON.stringify(x.name))})">${esc(x.name)}</span>
            <span class="sub">${x.size_kb} KB</span>
          </div>`
        )
        .join("")
    : `<div class="muted">未发现 *.html 报表</div>`;

  await loadLedgerInfo();
}

async function openReport(path, title) {
  $("#frame-title").textContent = title;
  $("#frame-open").href = "/api/reports/view?path=" + encodeURIComponent(path);
  $("#report-frame").src = "/api/reports/view?path=" + encodeURIComponent(path);
  $("#report-frame-wrap").classList.remove("hidden");
  $("#report-frame-wrap").scrollIntoView({ behavior: "smooth" });
}
function closeFrame() {
  $("#report-frame-wrap").classList.add("hidden");
  $("#report-frame").src = "about:blank";
}

async function loadLedgerInfo() {
  const d = await api("/api/ledger");
  if (!d.exists) {
    $("#ledger-info").innerHTML = `<div class="muted">台账不存在</div>`;
    return;
  }
  $("#ledger-info").innerHTML =
    `<div class="item"><span class="sub">路径</span><span class="sub">${esc(d.path)}</span></div>` +
    (d.error ? `<div class="pill bad">${d.error}</div>` : "") +
    (d.sheets || [])
      .map(
        (s) => `<div class="item">
          <span class="nm" onclick="openSheet(${esc(JSON.stringify(s.name))})">${s.name}</span>
          <span class="sub">${s.rows} 行 × ${s.cols} 列</span>
        </div>`
      )
      .join("");
}

function esc(s) {
  return String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, '&#39;');
}

function checksHtml(checks) {
  const icon = { error: "⛔", warn: "⚠", info: "ⓘ" };
  return `<div class="checks">${checks
    .map((c) => `<div class="chk-item ${c.level}">${icon[c.level] || "ⓘ"} ${esc(c.msg)}</div>`)
    .join("")}</div>`;
}

async function openSheet(name) {
  await loadSheetInto("#ledger-view", name);
}

let sheetState = null;
// P1-1：共享表格加载器的请求序号。旧请求（成功或失败）不得覆盖新请求的状态与 DOM。
let sheetReqSeq = 0;

// 台账预览首屏只渲染 200 行，其余按需「加载更多」（P1-3：接口本就支持 offset/limit）
const SHEET_PAGE = 200;

async function loadSheetInto(sel, name, limit = SHEET_PAGE, file = "", ctx = null) {
  const req = ++sheetReqSeq;
  const q =
    `/api/ledger/sheet?name=${encodeURIComponent(name)}&limit=${limit}&offset=0` +
    (file ? `&file=${encodeURIComponent(file)}` : "");
  let d;
  try {
    d = await api(q);
  } catch (e) {
    if (req !== sheetReqSeq) return null;   // 过期失败响应：忽略，避免弹错误态抢新表
    throw e;
  }
  if (req !== sheetReqSeq) return null;     // 过期成功响应：忽略，避免 B 标题 + A 数据
  sheetState = {
    sel,
    name,
    file,
    reqId: req,
    account: (ctx && ctx.account) || "",
    ctxQ: (ctx && ctx.q) || "",
    header: d.header || [],
    rows: d.rows || [],
    filter: "",
    sortCol: -1,
    sortDir: 1,
    fileLabel: d.file || "",
    checks: d.checks || [],
    hiddenCols: d.hidden_cols || [],
    total: d.total || 0,
    page: limit,
  };
  renderSheet();
  return sheetState;
}

function renderSheet() {
  const st = sheetState;
  if (!st) return;
  const host = $(st.sel);
  if (!host) return;   // 弹窗已关闭等：容器不在就跳过，避免 null 报错
  const head = st.header
    .map((h, i) => `<th>${esc(h)}</th>`)
    .join("");
  const hiddenNote = (st.hiddenCols || []).length
    ? `<span class="muted">隐藏列 ${st.hiddenCols.length} 个（已按 Excel 设置过滤）</span>`
    : "";
  // 弹窗里的表格：右侧给一个「进入表格」——切到全屏视图（视图内自身不再显示，避免自我循环）
  const jump = st.sel === "#sheet-host"
    ? ""
    : `<button type="button" class="btn sm sheet-open" onclick="openSheetViewFromSheet()"
         title="在整页表格视图中打开（可收藏、可返回）">进入表格 ↗</button>`;
  host.innerHTML = `
    <div class="sheet-toolbar">
      <input type="text" placeholder="搜索本表内容…" value="${esc(st.filter)}" oninput="filterSheet(this.value)" />
      <span class="muted sheet-count"></span>
      ${hiddenNote}
      <span class="sheet-tools">
        ${jump}
        <span class="muted sheet-file">${esc(st.fileLabel)}</span>
      </span>
    </div>
    ${(st.checks || []).length ? checksHtml(st.checks) : ""}
    <div class="table-wrap"><table class="tbl tk-sheet"><thead><tr>${head}</tr></thead><tbody></tbody></table></div>
    <div class="sheet-more" style="margin-top:10px"></div>`;
  // 表现层增强：表头排序（仍复用 sortSheet，口径不变）+ 列宽拖拽（按表名记忆）
  const stTable = host.querySelector("table");
  if (stTable) {
    TKV.sortable(stTable, (i) => sortSheet(i));
    if (st.sortCol >= 0) TKV.setSort(stTable, st.sortCol, st.sortDir);
    TKV.resizable(stTable, "sheet:" + st.name, { min: 56 });
  }
  updateSheetBody();
  focusSheetTarget(st.sel);   // 数据就位后把视口带到表格本身（全屏 / 弹窗 / 台账预览）
}

/* ── 表格定位：进入表格后自动滚到表格，而不是停在原来的滚动位置 ──
   触发：① 全屏表格视图（#sheet-host）② 台账弹窗（#modal-content）③ 报表页台账预览（#ledger-view）
   目标：全屏 = #view-sheet 顶部；弹窗 = 弹窗内容顶部；内联 = 容器顶部 */
function focusSheetTarget(sel) {
  if (sel === "#sheet-host") {
    const view = $("#view-sheet");
    if (!view || !view.classList.contains("active")) return;
    const top = view.getBoundingClientRect().top + (window.scrollY || 0) - 10;
    window.scrollTo(0, Math.max(0, top));
    return;
  }
  if (sel === "#modal-content") {
    const body = document.querySelector("#modal .modal-body");
    if (body) body.scrollTop = 0;
    return;
  }
  const host = $(sel);
  if (host) host.scrollIntoView({ block: "start" });
}

// 分页「加载更多」：追加下一页；过滤/排序态下同样保留按钮（追加后自动重新过滤/排序）
function renderSheetMore() {
  const st = sheetState;
  if (!st) return;
  const host = $(st.sel);
  const box = host && host.querySelector(".sheet-more");
  if (!box) return;
  const loaded = st.rows.length;
  const hasMore = st.total > loaded;
  // P2-1：搜索/排序只影响「已加载窗口」，但不能因此拿掉继续加载的入口
  const scope = st.filter || st.sortCol >= 0
    ? `<span class="muted">搜索/排序仅作用于已加载的 ${loaded} / ${st.total} 行</span>`
    : "";
  if (!hasMore) {
    box.innerHTML = scope;
    return;
  }
  box.innerHTML =
    scope +
    `<button type="button" class="btn ghost sm" onclick="loadMoreSheet()">加载更多（已加载 ${loaded} / ${st.total} 行）</button>`;
}

async function loadMoreSheet() {
  const st = sheetState;
  if (!st) return;
  const btn = $(st.sel) && $(st.sel).querySelector(".sheet-more button");
  if (btn) { btn.disabled = true; btn.textContent = "加载中…"; }
  try {
    const d = await api(
      `/api/ledger/sheet?name=${encodeURIComponent(st.name)}&limit=${st.page}&offset=${st.rows.length}` +
      (st.file ? `&file=${encodeURIComponent(st.file)}` : "")
    );
    // 追加期间若又发起了整表加载（或已切表），丢弃这批数据
    if (sheetState !== st || st.reqId !== sheetReqSeq) return;
    st.rows = st.rows.concat(d.rows || []);
    st.total = d.total || st.total;
    updateSheetBody();
    renderSheetMore();
  } catch (e) {
    if (sheetState !== st || st.reqId !== sheetReqSeq) return;
    toast("加载失败：" + e.message);
    if (btn) { btn.disabled = false; btn.textContent = "加载更多"; }
  }
}

function _visibleRows() {
  const st = sheetState;
  let rows = st.rows;
  if (st.filter) {
    const f = st.filter.toLowerCase();
    rows = rows.filter((r) => r.some((c) => String(c || "").toLowerCase().includes(f)));
  }
  if (st.sortCol >= 0) {
    const c = st.sortCol;
    const dir = st.sortDir;
    rows = rows.slice().sort((a, b) => {
      const x = a[c] || "";
      const y = b[c] || "";
      const nx = parseFloat(String(x).replace(/,/g, ""));
      const ny = parseFloat(String(y).replace(/,/g, ""));
      if (!isNaN(nx) && !isNaN(ny)) return (nx - ny) * dir;
      return String(x).localeCompare(String(y), "zh") * dir;
    });
  }
  return rows;
}

function updateSheetBody() {
  const st = sheetState;
  if (!st) return;
  const rows = _visibleRows();
  const tb = $(st.sel).querySelector("tbody");
  if (tb) {
    tb.innerHTML = rows.length
      ? rows.map((r) => `<tr>${r.map((c) => {
          const txt = String(c === null || c === undefined ? "" : c);
          // 数字列右对齐：纯数字（可带千分位/百分号）走 num，读数更易比对
          const isNum = txt !== "" && /^-?[\d,]+(\.\d+)?%?$/.test(txt.trim());
          return `<td class="${isNum ? "num" : ""}" title="${esc(txt)}">${esc(txt)}</td>`;
        }).join("")}</tr>`).join("")
      : TKV.empty(st.header.length || 1, {
          icon: "⌕", title: "没有匹配的行",
          desc: `「${st.filter}」在已加载的 ${st.rows.length} 行里没有命中；可清空搜索或继续加载更多。`,
          action: `<button class="btn ghost sm" onclick="filterSheet('')">清空搜索</button>`,
        });
    TKV.reveal(tb);
  }
  const cnt = $(st.sel).querySelector(".sheet-count");
  if (cnt) cnt.textContent = `显示 ${rows.length} / ${st.total || st.rows.length} 行`;
  renderSheetMore();
}

function filterSheet(v) {
  if (!sheetState) return;
  sheetState.filter = v;
  updateSheetBody();
}

function sortSheet(i) {
  const st = sheetState;
  if (!st) return;
  if (st.sortCol === i) st.sortDir = -st.sortDir;
  else {
    st.sortCol = i;
    st.sortDir = 1;
  }
  const table = $(st.sel).querySelector("table");
  if (table) TKV.setSort(table, st.sortCol, st.sortDir);
  updateSheetBody();
}

async function openInExcel(file, sheet) {
  toast("正在唤起 Excel…");
  try {
    const r = await api("/api/ledger/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file, sheet }),
    });
    if (r.ok) toast(`已用 Excel 打开：${r.method === "excel-com" ? sheet : r.file}`);
    else toast("打开失败：" + (r.error || "未知错误"));
  } catch (e) {
    toast("打开失败：" + e.message);
  }
}

/* ─────────── 弹窗：账号 → 台账 ─────────── */
let modalReturnFocus = null;
function openModal(title, sub = "", excelCtx = null) {
  modalReturnFocus = document.activeElement;
  $("#modal-title").textContent = title;
  $("#modal-sub").textContent = sub;
  $("#modal-content").innerHTML = `<div class="muted">加载中…</div>`;
  const btn = $("#modal-excel");
  if (btn) {
    btn.style.display = excelCtx ? "" : "none";
    btn.onclick = excelCtx ? () => openInExcel(excelCtx.file, excelCtx.sheet) : null;
  }
  // 「进入表格」：同一上下文切到全屏表格视图（可深链、可返回）
  const shBtn = $("#modal-sheet");
  if (shBtn) {
    shBtn.style.display = excelCtx ? "" : "none";
    shBtn.onclick = excelCtx
      ? () => {
          // P2-2：file/sheet 之外还要带上来源账号与预筛词，否则进全屏会丢上下文
          const c = {
            file: excelCtx.file || "", sheet: excelCtx.sheet || "",
            account: excelCtx.account || "", q: excelCtx.q || "",
          };
          closeModal();
          openSheetView(c);
        }
      : null;
  }
  $("#modal").classList.remove("hidden");
  // P1-7：aria-modal 已声明，但 Tab 会走到侧栏与背后的表格 —— 打开时把背景设为 inert
  const app = document.querySelector(".app");
  if (app && "inert" in app) app.inert = true;
  const first = $("#modal-content").querySelector("button, input, select, textarea, a[href]");
  if (first) first.focus();
}

function closeModal() {
  $("#modal").classList.add("hidden");
  $("#modal-content").innerHTML = "";
  // P1-7：先解除背景 inert，否则焦点还原会被浏览器忽略
  const app = document.querySelector(".app");
  if (app && "inert" in app) app.inert = false;
  // 焦点还原到打开弹窗的元素上（P2-4）
  if (modalReturnFocus && typeof modalReturnFocus.focus === "function") {
    modalReturnFocus.focus();
  }
  modalReturnFocus = null;
}

/* P1-7：弹窗焦点陷阱 —— Tab / Shift+Tab 在弹窗内循环，不再"消失"到背景内容 */
const MODAL_FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]),'
  + ' select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
function modalFocusables() {
  return $$("#modal " + MODAL_FOCUSABLE)
    .filter((e) => e.offsetParent !== null || e === document.activeElement);
}
if (typeof document !== "undefined" && $("#modal")) {
  $("#modal").addEventListener("keydown", (ev) => {
    if (ev.key !== "Tab") return;
    const f = modalFocusables();
    if (!f.length) return;
    const host = $("#modal");
    const first = f[0], last = f[f.length - 1], cur = document.activeElement;
    const inside = host.contains(cur);
    if (ev.shiftKey && (cur === first || !inside)) { ev.preventDefault(); last.focus(); }
    else if (!ev.shiftKey && (cur === last || !inside)) { ev.preventDefault(); first.focus(); }
  });
}

// 弹窗里已有未提交输入时，Esc 不再静默关掉（P2-4）
function modalDirty() {
  return $$("#modal-content textarea, #modal-content input").some(
    (i) => i.value && i.value.trim()
  );
}

async function openAccountLedger(account) {
  try {
    const r = await api(`/api/ledger/resolve?account=${encodeURIComponent(account)}`);
    if (r.sheet) {
      // 弹窗与全屏共用这份上下文：file/sheet + 来源账号 + 预筛词
      const ctx = { file: r.file || "", sheet: r.sheet, account, q: "@" + account };
      openModal(`台账 · ${r.sheet}`, `${account} · ${r.file || ""}`, ctx);
      await loadSheetInto("#modal-content", r.sheet, 200, r.file, ctx);
      return;
    }
    // 无对应 sheet：弹窗列出可选项，供手动查看
    openModal(`台账 · 未找到 ${account}`, "该账号暂无台账 sheet");
    const list = (r.candidates || [])
      .map(
        (n) =>
          `<div class="item"><span class="nm" onclick="openSheetFromModal(${esc(JSON.stringify(n))})">${n}</span><span class="sub">点击查看</span></div>`
      )
      .join("");
    $("#modal-content").innerHTML =
      `<div class="muted" style="margin-bottom:10px">台账目录中未找到「${account}」对应的 sheet（该账号尚未归档）。现有 sheet：</div>` +
      `<div class="list">${list || '<div class="muted">台账内无可用 sheet</div>'}</div>`;
  } catch (e) {
    toast("打开台账失败: " + e.message);
  }
}

async function openSheetFromModal(name, file = "") {
  openModal(`台账 · ${name}`, file || "", { file, sheet: name });
  await loadSheetInto("#modal-content", name, undefined, file);
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if ($("#modal").classList.contains("hidden")) return;
  if (modalDirty()) {
    toast("弹窗内还有未提交的内容，已保留；要放弃请点「关闭」");
    return;
  }
  closeModal();
});
document.addEventListener("click", (e) => {
  if (e.target && e.target.id === "modal") closeModal();
});

/* ─────────── 定时 ─────────── */
async function loadSchedule() {
  let s;
  try {
    s = await api("/api/schedule");
  } catch (e) {
    const el2 = $("#sc-last");
    if (el2) el2.textContent = "定时配置读取失败：" + apiErrMsg(e);
    return toast("定时配置读取失败：" + apiErrMsg(e), "error");
  }
  $("#sc-enabled").checked = !!s.enabled;
  $("#sc-time").value = s.time || "09:30"; $("#sc-task").value = s.task || "collect";
  $("#sc-last").textContent = s.last_run ? "上次自动执行：" + s.last_run : "尚未自动执行";
  $$("#sc-accounts .chip").forEach((c) =>
    c.classList.toggle("on", (s.accounts || []).includes(c.dataset.acc))
  );
}
async function saveSchedule() {
  const payload = {
    enabled: $("#sc-enabled").checked, task: $("#sc-task").value,
    time: $("#sc-time").value,
    accounts: $$("#sc-accounts .chip.on").map((c) => c.dataset.acc),
    last_run: ($("#sc-last").textContent.match(/\d{4}-\d{2}-\d{2}/) || [null])[0],
  };
  try {
    await api("/api/schedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    toast("定时配置已保存", "ok");
  } catch (e) {
    toast("保存失败：" + apiErrMsg(e), "error");
  }
}
async function triggerDaily() {
  let run;
  try {
    run = await api("/api/schedule/trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accounts: $$("#sc-accounts .chip.on").map((c) => c.dataset.acc) }),
    });
  } catch (e) {
    return toast("触发失败：" + apiErrMsg(e), "error");
  }
  toast("已触发每日增量更新", "ok");
  $$(".nav-item").find((n) => n.dataset.view === "run").click();
  attachRun(run.id);
}

/* ─────────── 爆品监控 ─────────── */
async function loadHot() {
  let d;
  try {
    d = await api("/api/hot/latest");
  } catch (e) {
    const box0 = $("#hot-candidates");
    if (box0) box0.innerHTML = `<div class="muted">爆品数据读取失败：${esc(apiErrMsg(e))}</div>`;
    return toast("爆品数据读取失败：" + apiErrMsg(e), "error");
  }
  hotPayload = d;
  $("#hot-cookie").textContent = d.cookie_exists ? "cc_cookie.txt 就绪" : "缺少 cc_cookie.txt";
  const c = d.candidates;
  if (!c) {
    $("#hot-candidates").innerHTML = `<div class="muted">暂无差分结果</div>`;
  } else {
    const arr = Array.isArray(c) ? c : c.candidates || c.items || [];
    $("#hot-candidates").innerHTML = arr.length
      ? arr
          .slice(0, 30)
          .map(
            (x) => `<div class="item">
              <span class="nm">${esc(x.name || x.item_id || x.id || JSON.stringify(x).slice(0, 40))}</span>
              <span class="sub">${x.growth_pct ? "+" + x.growth_pct + "%" : x.source || ""}</span>
            </div>`
          )
          .join("")
      : `<div class="muted">候选清单为空</div>`;
  }
  $("#hot-snaps").innerHTML = (d.snapshots || []).length
    ? d.snapshots.map((s) => `<div class="item"><span class="nm">${s.date}</span><span class="sub">${s.sources.join(" · ")}</span></div>`).join("")
    : `<div class="muted">暂无快照</div>`;
}

async function hotAction(action) {
  try {
    const run = await api("/api/hot/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    toast("已启动爆品监控：" + action);
    $$(".nav-item").find((n) => n.dataset.view === "run").click();
    attachRun(run.id);
  } catch (e) {
    toast(e.message);
  }
}

/* ═════════════════ 账号管理（2026-10-19） ═════════════════
   入口：侧栏「账号管理」。数据源 accounts.json（唯一配置源），
   读写接口与 /api/accounts* 契约一致；统计复用 /api/overview 的缓存。 */
let acctCache = { accounts: [], counts: {}, ts: 0 };
let acctLoadedAt = 0;
let acctFormMode = "create";
const ACCT_NAME_RE = /^[a-z0-9_.]+$/;
const ACCT_SHEET_PREFIX = "对标记录-";
const ACCT_REGION_LABEL = {
  "": "未标注", us: "美国", uk: "英国", eu: "欧洲", de: "德国", fr: "法国",
  es: "西语区", it: "意区", cn: "中国大陆", "cn-hk": "中国香港", "cn-tw": "中国台湾",
  jp: "日本", kr: "韩国",
};
const ACCT_REGION_LANG = {
  us: "en", uk: "en", eu: "en", de: "de", fr: "fr", es: "es", it: "it",
  cn: "zh", "cn-hk": "zh", "cn-tw": "zh", jp: "jp", kr: "kr",
};
const ACCT_LANGS = [["en", "en（英文）"], ["de", "de（德语）"], ["fr", "fr（法语）"],
  ["es", "es（西语）"], ["it", "it（意语）"], ["zh", "zh（中文）"],
  ["jp", "jp（日语）"], ["kr", "kr（韩语）"]];

/* 后端错误体是 {"detail":"..."}，api() 抛出的是 "400 {...}"：拆出人话 */
function apiErrMsg(e) {
  const raw = String((e && e.message) || e || "");
  const body = raw.replace(/^\d{3}\s*/, "");
  try {
    const j = JSON.parse(body);
    return j.detail || j.message || body || raw;
  } catch (_) {
    return body || raw;
  }
}

/* 账号名校验：与 config.validate_account_name 同规则，前端先拦一道 */
function acctNameError(v, opts) {
  const name = String(v || "").trim();
  if (!name) return "账号名必填";
  if (!ACCT_NAME_RE.test(name)) return "只能用小写字母、数字、下划线、点";
  if ((ACCT_SHEET_PREFIX + name).length > 31) return "太长：Excel sheet 名「对标记录-xxx」上限 31 字符";
  if (/[[\]:*?/\\]/.test(name)) return "不能包含 [ ] : * ? / \\";
  if (opts && opts.self !== name) {
    const known = (acctCache.accounts || []).some((a) => a.account === name);
    if (known) return "该账号已存在";
  }
  return "";
}

async function loadAccounts(force) {
  const box = $("#acct-list");
  if (!box) return;
  if (!force && acctCache.accounts.length && Date.now() - acctLoadedAt < 15000) {
    renderAccountTable();
    return;
  }
  box.innerHTML = `<div class="acct-state muted">加载中…</div>`;
  try {
    const d = await api("/api/accounts");
    acctCache = d;
    acctLoadedAt = Date.now();
    renderAccountKpis();
    renderAccountGroupFilter();
    renderAccountTable();
    const el = $("#acct-updated");
    if (el) el.textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (e) {
    box.innerHTML = `<div class="acct-state">
      <div class="hint-bad">账号清单加载失败</div>
      <div class="muted">${esc(apiErrMsg(e))}</div>
      <div style="margin-top:10px"><button class="btn sm" onclick="loadAccounts(true)">重试</button></div>
    </div>`;
    toast("加载失败：" + apiErrMsg(e));
  }
}

function renderAccountKpis() {
  const box = $("#acct-kpis");
  if (!box) return;
  const c = acctCache.counts || {};
  const items = [
    ["账号总数", c.all || 0, "accounts.json 记录数", ""],
    ["启用中", c.active || 0, "参与流水线", ""],
    ["图文账号", c.image || 0, "跳过音频与转写", ""],
    ["有产物", c.artifacts || 0, "不可直接删除", (c.all || 0) > (c.artifacts || 0) ? "" : "warn"],
  ];
  box.innerHTML = items.map(([l, v, cap, cls]) =>
    `<div class="kpi ${cls}"><div class="kpi-l">${l}</div><div class="kpi-v">${v}</div>
     <div class="kpi-cap">${cap}</div></div>`).join("");
}

function renderAccountGroupFilter() {
  const sel = $("#acct-group");
  if (!sel) return;
  const prev = sel.value;
  const seen = new Map();
  (acctCache.accounts || []).forEach((x) => {
    if (x.group_id) seen.set(x.group_id, x.group_name || x.group_id);
  });
  sel.innerHTML = `<option value="">全部分组</option>` +
    [...seen.entries()].map(([id, name]) => `<option value="${esc(id)}">${esc(name)}</option>`).join("");
  if (prev && (prev === "" || seen.has(prev))) sel.value = prev;
}

function acctFiltered() {
  const q = (($("#acct-q") || {}).value || "").trim().toLowerCase();
  const st = (($("#acct-status") || {}).value) || "";
  const tp = (($("#acct-type") || {}).value) || "";
  const gp = (($("#acct-group") || {}).value) || "";
  return (acctCache.accounts || []).filter((x) => {
    if (q) {
      const hay = [x.account, x.nickname, (x.tags || []).join(" "), x.group_name]
        .join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (st === "active" && !x.active) return false;
    if (st === "inactive" && x.active) return false;
    if (tp && x.media_type !== tp) return false;
    if (gp && x.group_id !== gp) return false;
    return true;
  });
}

/* 账号表排序键（null = 该列不排序） */
const ACCT_SORT_COLS = ["account", null, "media_type", "group_name", "active",
                        "scrape_items", "audio_count", "transcript_count", "ledger_rows",
                        "landing", null];
let acctSort = { key: "", dir: 1 };   // 空 = 保持后端返回顺序

function renderAccountTable() {
  const box = $("#acct-list");
  if (!box) return;
  const all = acctCache.accounts || [];
  const rows = acctFiltered();
  const hint = $("#acct-hint");
  if (hint) hint.textContent = all.length ? `显示 ${rows.length} / ${all.length}` : "—";

  if (!all.length) {
    box.innerHTML = `<div class="acct-state">
      <div style="font-weight:600;margin-bottom:6px">还没有账号</div>
      <div class="muted">账号来自 accounts.json；新增后会出现在分组看板与一键运行的下拉里</div>
      <div style="margin-top:12px;display:flex;gap:8px;justify-content:center">
        <button class="btn primary accent" onclick="openAccountForm('create')">＋ 新增账号</button>
        <button class="btn" onclick="openAccountImport()">⤓ 批量导入</button>
      </div></div>`;
    return;
  }
  if (!rows.length) {
    box.innerHTML = `<div class="acct-state muted">没有匹配的账号，试试清空筛选条件</div>`;
    return;
  }

  const key = acctSort.key;
  const dir = acctSort.dir;
  const sorted = key
    ? rows.slice().sort((a, b) => {
        if (key === "account" || key === "group_name" || key === "media_type") {
          return dir * String(a[key] || "").localeCompare(String(b[key] || ""), "zh");
        }
        if (key === "active") return dir * ((a.active ? 1 : 0) - (b.active ? 1 : 0));
        return dir * ((Number(a[key]) || 0) - (Number(b[key]) || 0));
      })
    : rows;

  box.innerHTML = `<table class="tbl acct-tbl"><thead><tr>
      <th>账号</th><th>地区 · 语言</th><th>类型</th><th>分组</th><th>状态</th>
      <th class="rt">抓取</th><th class="rt">音频</th><th class="rt">转写</th><th class="rt">台账</th>
      <th>健康</th><th class="rt">操作</th>
    </tr></thead><tbody>${sorted.map(acctRowHtml).join("")}</tbody></table>`;

  const tbl = box.querySelector("table");
  if (tbl) {
    TKV.sortable(tbl, (i) => acctSortBy(i), { skip: [1, 10] });
    if (key) TKV.setSort(tbl, ACCT_SORT_COLS.indexOf(key), dir);
    TKV.resizable(tbl, "acct-list-v2", { min: 56,
      // 2026-10-22：11 列平均分配时操作列只剩 ~110px，5 个按钮被裁到表外；
      // 固定前 10 列、把剩余宽度全给操作列。
      widths: [150, 90, 72, 96, 64, 58, 58, 58, 58, 92, 0] });
    TKV.reveal(tbl.tBodies[0]);
  }
}

function acctSortBy(i) {
  const key = ACCT_SORT_COLS[i];
  if (!key) return;
  if (acctSort.key === key) acctSort.dir = -acctSort.dir;
  else acctSort = { key, dir: key === "account" || key === "group_name" || key === "media_type" ? 1 : -1 };
  renderAccountTable();
}

function acctRowHtml(x) {
  const lit = esc(JSON.stringify(x.account));
  // 2026-10-22 UI 整改：标签里重复昵称/自身重复时只留一个（原来渲染成「杜桂月 杜桂月」）
  const seen = new Set([String(x.nickname || "").trim()].filter(Boolean));
  const tags = (x.tags || [])
    .filter((t) => {
      const k = String(t || "").trim();
      if (!k || seen.has(k)) return false;
      seen.add(k);
      return true;
    })
    .slice(0, 2)
    .map((t) => `<span class="tag-mini">${esc(t)}</span>`).join("");
  const st = x.severity === "error"
    ? `<span class="st error">异常</span>`
    : x.severity === "warn" ? `<span class="st warn">待处理</span>` : `<span class="st">正常</span>`;
  return `<tr data-acc="${esc(x.account)}">
    <td class="acc"><div class="acct-nm">${esc(x.account)}</div>
      <div class="acct-sub">${x.nickname ? esc(x.nickname) : `<span class="muted">无昵称</span>`}${tags}</div></td>
    <td>${esc(ACCT_REGION_LABEL[x.region] || x.region || "未标注")}
      <span class="muted">${esc(x.lang || "—")}</span></td>
    <td>${x.media_type === "image" ? `<span class="tag info">图文</span>` : `<span class="tag">视频</span>`}</td>
    <td>${esc(x.group_name || "未分组")}</td>
    <td>${x.active ? `<span class="badge green">启用</span>` : `<span class="badge gray">停用</span>`}</td>
    <td class="rt">${x.scrape_items || 0}</td>
    <td class="rt">${x.audio_count || 0}</td>
    <td class="rt">${x.transcript_count || 0}</td>
    <td class="rt">${x.ledger_rows || 0}</td>
    <td>${st}<span class="muted"> ${Number(x.landing) || 0}%</span></td>
    <td class="rt acct-ops">
      <button class="btn-link" onclick="openAccountSheet(${lit})">表格</button>
      <button class="btn-link" onclick="openAccountDetail(${lit})">详情</button>
      <button class="btn-link" onclick="openAccountForm('edit', ${lit})">编辑</button>
      <button class="btn-link" onclick="toggleAccountActive(${lit}, ${x.active ? "false" : "true"})">${x.active ? "停用" : "启用"}</button>
      <button class="btn-link danger" onclick="deleteAccount(${lit})">删除</button>
    </td></tr>`;
}

/* 详情：读 /api/accounts/{name}（视图统计 + 配置 + 字段 schema） */
async function openAccountDetail(name) {
  openModal(`账号详情 · ${name}`, "配置 + 产物统计", null);
  const box = $("#modal-content");
  try {
    const d = await api(`/api/accounts/${encodeURIComponent(name)}`);
    const lit = esc(JSON.stringify(name));
    const gaps = (d.gaps || []).map((g) =>
      `<div class="acct-gap ${esc(g.level || "info")}"><b>${esc(g.label || "缺口")}</b> · ${esc(g.msg || "")}</div>`).join("");
    const kv = [
      ["账号", esc(d.account)],
      ["昵称", d.nickname ? esc(d.nickname) : `<span class="muted">—</span>`],
      ["地区 / 语言", `${esc(ACCT_REGION_LABEL[d.region] || d.region || "未标注")} / ${esc(d.lang || "—")}`],
      ["类型", d.media_type === "image" ? "图文账号" : "视频账号"],
      ["配置状态", d.active === false ? "已停用" : "启用中"],
      ["标签", (d.tags || []).length ? (d.tags || []).map((t) => `<span class="tag-mini">${esc(t)}</span>`).join(" ") : `<span class="muted">—</span>`],
      ["抓取 / 音频 / 转写", `${d.scrape_items || 0} / ${d.audio_count || 0} / ${d.transcript_count || 0}`],
      ["台账行 / 已覆盖", `${d.ledger_rows || 0} / ${d.ledger_covered == null ? "—" : d.ledger_covered}`],
      ["商品锚点", `${d.products_count || 0} 条视频`],
      ["落地率", `${Number(d.landing) || 0}%${d.severity === "error" ? "（异常）" : d.severity === "warn" ? "（待处理）" : ""}`],
      ["最近成功", d.last_success_date ? esc(d.last_success_date) : `<span class="muted">未采集</span>`],
      ["备注", d.note ? esc(d.note) : `<span class="muted">—</span>`],
    ];
    box.innerHTML = `<dl class="acct-kv">${kv.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>
      ${gaps || `<div class="acct-gap info">没有检出缺口</div>`}
      <div class="acct-detail-actions">
        <button class="btn primary accent" onclick="openAccountForm('edit', ${lit})">编辑</button>
        <button class="btn" onclick="toggleAccountActive(${lit}, ${d.active === false ? "true" : "false"})">${d.active === false ? "启用" : "停用"}</button>
        <button class="btn danger" onclick="deleteAccount(${lit})">删除</button>
        <button class="btn ghost" onclick="openAccountLedger(${lit})">打开台账</button>
      </div>`;
  } catch (e) {
    box.innerHTML = `<div class="chk-item error">读取失败：${esc(apiErrMsg(e))}</div>`;
  }
}

/* 新增 / 编辑表单（共用） */
function openAccountForm(mode, name) {
  acctFormMode = mode;
  const x = mode === "edit" ? (acctCache.accounts || []).find((a) => a.account === name) : null;
  if (mode === "edit" && !x) return toast("账号不在当前列表，请先刷新");
  openModal(
    mode === "edit" ? `编辑账号 · ${name}` : "新增账号",
    mode === "edit" ? "改动会立即写入 accounts.json" : "账号名 = Excel sheet 后缀，创建后不可修改",
    null
  );
  const regionOpts = Object.entries(ACCT_REGION_LABEL)
    .map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  const langOpts = ACCT_LANGS
    .map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
  const v = x || {};
  $("#modal-content").innerHTML = `<div class="acct-form">
    <label class="field wide"><span>账号名（必填）</span>
      <input id="af-account" value="${esc(v.account || "")}" ${mode === "edit" ? "readonly" : ""}
             placeholder="例：gutpickde（小写字母/数字/下划线/点）" autocomplete="off" />
      <div class="field-hint" id="af-account-hint"></div>
    </label>
    <label class="field"><span>昵称</span>
      <input id="af-nickname" value="${esc(v.nickname || "")}" placeholder="展示名，可空" /></label>
    <label class="field"><span>面向地区</span>
      <select id="af-region" onchange="acctFormSyncLang()">${regionOpts}</select></label>
    <label class="field"><span>转写语言</span>
      <select id="af-lang">${langOpts}</select>
      <div class="field-hint muted">默认跟随地区推断，可手动覆盖</div></label>
    <label class="field"><span>类型</span>
      <select id="af-type">
        <option value="video">视频账号（音频 → 转写）</option>
        <option value="image">图文账号（跳过音频与转写）</option>
      </select></label>
    <label class="field"><span>标签（逗号分隔）</span>
      <input id="af-tags" value="${esc((v.tags || []).join(","))}" placeholder="例：德国AI对标,家居" /></label>
    <label class="field"><span>配置状态</span>
      <select id="af-active">
        <option value="1">启用（参与流水线）</option>
        <option value="0">停用（保留历史，不再运行）</option>
      </select></label>
    <label class="field wide"><span>备注</span>
      <textarea id="af-note" rows="2" placeholder="可空">${esc(v.note || "")}</textarea></label>
    <details class="adv wide"><summary>高级：自定义产物目录</summary>
      <div class="acct-form" style="margin-top:10px">
        <label class="field"><span>抓取产物目录 out_dir</span>
          <input id="af-out" value="${esc(v.out_dir || "")}" placeholder="留空 = pipeline/output" /></label>
        <label class="field"><span>音频目录 audio_dir</span>
          <input id="af-audio" value="${esc(v.audio_dir || "")}" placeholder="留空 = output/{account}_audio" /></label>
      </div>
    </details>
    <div class="field-hint wide hint-bad" id="af-error"></div>
    <div class="acct-detail-actions wide">
      <button class="btn primary accent" id="af-save" onclick="saveAccountForm()">保存</button>
      <button class="btn ghost" onclick="closeModal()">取消</button>
      <span class="muted">${mode === "edit" ? "主键与产物目录改动会影响后续采集路径" : "创建后可随时在列表里编辑"}</span>
    </div>
  </div>`;

  $("#af-region").value = v.region || "";
  $("#af-lang").value = v.lang || ACCT_REGION_LANG[v.region || ""] || "en";
  $("#af-type").value = v.media_type === "image" ? "image" : "video";
  $("#af-active").value = v.active === false ? "0" : "1";

  const accEl = $("#af-account");
  if (mode === "create") {
    accEl.focus();
    accEl.addEventListener("input", () => {
      accEl.value = accEl.value.toLowerCase().replace(/\s+/g, "");
      const err = acctNameError(accEl.value);
      const h = $("#af-account-hint");
      h.textContent = err || (accEl.value ? "可用" : "");
      h.className = "field-hint " + (err ? "hint-bad" : "hint-ok");
    });
  }
}

function acctFormSyncLang() {
  const r = $("#af-region").value;
  const l = ACCT_REGION_LANG[r];
  if (l) $("#af-lang").value = l;
}

async function saveAccountForm() {
  const btn = $("#af-save");
  const errBox = $("#af-error");
  const mode = acctFormMode;
  const name = ($("#af-account").value || "").trim().toLowerCase();
  const bad = acctNameError(name, { self: mode === "edit" ? name : "" });
  if (bad && mode === "create") {
    errBox.textContent = bad;
    $("#af-account-hint").textContent = bad;
    $("#af-account-hint").className = "field-hint hint-bad";
    return;
  }
  const payload = {
    nickname: ($("#af-nickname").value || "").trim().slice(0, 64),
    region: $("#af-region").value,
    lang: $("#af-lang").value,
    tags: ($("#af-tags").value || "").split(",").map((s) => s.trim()).filter(Boolean).slice(0, 8),
    note: ($("#af-note").value || "").trim().slice(0, 500),
    active: $("#af-active").value === "1",
    media_type: $("#af-type").value,
    out_dir: ($("#af-out").value || "").trim(),
    audio_dir: ($("#af-audio").value || "").trim(),
  };
  if (mode === "create") payload.account = name;

  btn.disabled = true;
  btn.textContent = "保存中…";
  errBox.textContent = "";
  try {
    if (mode === "create") {
      await api("/api/accounts", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    } else {
      await api(`/api/accounts/${encodeURIComponent(name)}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
    toast(mode === "create" ? `已创建 ${name}` : `已保存 ${name}`);
    closeModal();
    await refreshAccountsEverywhere();
  } catch (e) {
    const msg = apiErrMsg(e);
    errBox.textContent = msg;
    toast("保存失败：" + msg);
    btn.disabled = false;
    btn.textContent = "保存";
  }
}

async function toggleAccountActive(name, next) {
  try {
    await api(`/api/accounts/${encodeURIComponent(name)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active: !!next }),
    });
    toast(`${next ? "已启用" : "已停用"} ${name}`);
    await refreshAccountsEverywhere();
  } catch (e) {
    toast("操作失败：" + apiErrMsg(e));
  }
}

async function deleteAccount(name) {
  const x = (acctCache.accounts || []).find((a) => a.account === name);
  if (x && x.has_artifacts) {
    toast(`${name} 已有产物（抓取/音频/台账），请改用「停用」`);
    return;
  }
  if (!confirm(`删除账号「${name}」？\n\n只删 accounts.json 里的这条配置，不删任何文件、不删台账。\n仅允许删除没有任何产物的空壳账号。`)) return;
  try {
    await api(`/api/accounts/${encodeURIComponent(name)}`, { method: "DELETE" });
    toast("已删除 " + name);
    closeModal();
    await refreshAccountsEverywhere();
  } catch (e) {
    toast("删除失败：" + apiErrMsg(e));
  }
}

/* 批量导入：支持 JSON（数组 / {accounts:[...]}）、带表头 CSV、每行 @handle / 主页链接 / 账号名
   返回 [{account, nickname?, region?, lang?, tags?, media_type?}]，按 account 去重 */
const ACCT_FIELD_KEYS = {
  account: "account", accounts: "account", handle: "account", username: "account",
  name: "account", "账号": "account", "账号名": "account", "用户名": "account",
  nickname: "nickname", "昵称": "nickname",
  region: "region", "地区": "region",
  lang: "lang", "语言": "lang",
  tags: "tags", "标签": "tags",
  media_type: "media_type", type: "media_type", "类型": "media_type",
};

function _acctNormalize(v) {
  let s = String(v === null || v === undefined ? "" : v).trim();
  const m = s.match(/@([a-z0-9_.]+)/i);
  if (m) s = m[1];
  else s = s.split(/[,;\t]/)[0].trim().replace(/^https?:\/\/[^/]+\//i, "");
  return s.replace(/^@/, "").trim().toLowerCase();
}

function _acctTags(v) {
  const arr = Array.isArray(v) ? v : String(v || "").split(/[|/、,]/);
  return arr.map((t) => String(t).trim()).filter(Boolean);
}

function _acctFromObject(x) {
  if (typeof x === "string") return { account: _acctNormalize(x) };
  if (!x || typeof x !== "object") return { account: "" };
  const it = { account: _acctNormalize(x.account || x.handle || x.username || x.name || x["账号"] || "") };
  ["nickname", "region", "lang"].forEach((k) => { if (x[k]) it[k] = String(x[k]).trim(); });
  if (x.tags) { const t = _acctTags(x.tags); if (t.length) it.tags = t; }
  const mt = String(x.media_type || x.type || "").toLowerCase();
  if (mt === "image" || mt === "video") it.media_type = mt;
  return it;
}

function parseAccountLines(text) {
  const raw = String(text || "").trim();
  if (!raw) return [];

  // 1) JSON 优先：数组 / {accounts:[...]} / 单个对象
  if (raw[0] === "{" || raw[0] === "[") {
    try {
      const j = JSON.parse(raw);
      const arr = Array.isArray(j) ? j : (Array.isArray(j.accounts) ? j.accounts : [j]);
      const out = [], seen = {};
      arr.forEach((x) => {
        const it = _acctFromObject(x);
        if (!it.account || seen[it.account]) return;
        seen[it.account] = 1;
        out.push(it);
      });
      return out;
    } catch (_) { /* 不是合法 JSON，退回按行解析 */ }
  }

  const lines = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  if (!lines.length) return [];

  // 2) CSV/TSV 表头：首行有分隔符且首列是已知字段名 → 按列映射，且不把表头当账号
  let cols = null;
  const firstCells = lines[0].split(/[,;\t]/).map((s) => s.trim());
  if (firstCells.length >= 2 && ACCT_FIELD_KEYS[firstCells[0].toLowerCase()]) {
    cols = firstCells.map((h) => ACCT_FIELD_KEYS[h.toLowerCase()] || "");
    lines.shift();
  }

  const out = [], seen = {};
  lines.forEach((line) => {
    let it;
    if (cols) {
      const cells = line.split(/[,;\t]/).map((s) => s.trim());
      it = {};
      cols.forEach((k, i) => { if (k && cells[i]) it[k] = cells[i]; });
      if (it.account) it.account = _acctNormalize(it.account);
      if (it.tags) it.tags = _acctTags(it.tags);
    } else {
      it = { account: _acctNormalize(line) };
    }
    if (!it.account || seen[it.account]) return;
    seen[it.account] = 1;
    out.push(it);
  });
  return out;
}

function openAccountImport() {
  openModal("批量导入账号", "支持 JSON / CSV（带表头）/ 每行 @handle；已存在的自动跳过", null);
  $("#modal-content").innerHTML = `<div class="acct-form">
    <label class="field wide"><span>账号清单</span>
      <textarea id="ai-text" rows="8" placeholder="@gutpickde&#10;https://www.tiktok.com/@noah.hd57&#10;lealife90&#10;--- 也支持 ---&#10;account,region&#10;new_a,de&#10;[{&quot;account&quot;:&quot;new_b&quot;,&quot;region&quot;:&quot;de&quot;}]"></textarea>
      <div class="field-hint muted">JSON / CSV 会保留 account、nickname、region、lang、tags、media_type；其余字段忽略。只建配置，不会自动抓取数据</div>
    </label>
    <div class="acct-detail-actions wide">
      <button class="btn primary accent" id="ai-go" onclick="doAccountImport()">开始导入</button>
      <button class="btn ghost" onclick="closeModal()">取消</button>
    </div>
    <div class="acct-import-res wide" id="ai-result"></div>
  </div>`;
  $("#ai-text").focus();
}

async function doAccountImport() {
  const items = parseAccountLines($("#ai-text").value);
  if (!items.length) return toast("请先粘贴至少一个账号");
  const btn = $("#ai-go");
  btn.disabled = true;
  btn.textContent = "导入中…";
  const res = [];
  for (const it of items) {
    const n = it.account;
    const bad = acctNameError(n);
    if (bad) { res.push({ n, ok: false, msg: bad }); continue; }
    if ((acctCache.accounts || []).some((a) => a.account === n)) {
      res.push({ n, ok: true, msg: "已存在，跳过" }); continue;
    }
    const body = { account: n };
    ["nickname", "region", "lang", "tags", "media_type"].forEach((k) => {
      const v = it[k];
      if (v === undefined || v === "" || (Array.isArray(v) && !v.length)) return;
      body[k] = v;
    });
    try {
      await api("/api/accounts", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      res.push({ n, ok: true, msg: "已创建" });
    } catch (e) {
      res.push({ n, ok: false, msg: apiErrMsg(e) });
    }
  }
  btn.disabled = false;
  btn.textContent = "开始导入";
  const okN = res.filter((r) => r.ok).length;
  $("#ai-result").innerHTML = res.map((r) =>
    `<div>${r.ok ? `<span class="hint-ok">✓</span>` : `<span class="hint-bad">✗</span>`}
      <b>${esc(r.n)}</b> <span class="muted">${esc(r.msg)}</span></div>`).join("");
  toast(`导入完成：成功 ${okN} / 共 ${res.length}`);
  await refreshAccountsEverywhere();
}

/* 任何账号写操作后：刷新配置下拉 + 账号列表 + 分组看板 */
async function refreshAccountsEverywhere() {
  try {
    CFG = await api("/api/config");
    fillAccountSelects();
  } catch (e) { console.warn("[accounts] 配置刷新失败（下次进入页面自愈）", e); }
  await loadAccounts(true);
  try { await loadOverview(false); } catch (e) { /* 同 */ }
}

/* 账号管理页的筛选事件（视图常驻 DOM，脚本加载时绑一次） */
if ($("#acct-q")) {
  let acctSearchTimer = null;
  $("#acct-q").addEventListener("input", () => {
    clearTimeout(acctSearchTimer);
    acctSearchTimer = setTimeout(renderAccountTable, 150);
  });
  ["#acct-status", "#acct-type", "#acct-group"].forEach((sel) => {
    const el = $(sel);
    if (el) el.addEventListener("change", renderAccountTable);
  });
}

/* ═════════════════ 表格视图（2026-10-19） ═════════════════
   入口：账号管理行「表格」/ 台账弹窗「进入表格」/ 组内指标行 → 台账弹窗 → 进入表格
   路由：#sheet?file=<file>&sheet=<sheet>&account=<account>&q=<kw>（可收藏、可前进后退）
   只读：不提供任何写操作；数据走 /api/ledger/sheet（支持 file + offset/limit） */
let sheetViewCtx = { file: "", sheet: "", account: "", q: "" };
let sheetReturnView = "overview";
let sheetReturnScroll = 0;   // 进入表格前的滚动位置，返回时恢复
let sheetViewSeq = 0;   // P1-1：全屏视图请求序号

function sheetHashOf(ctx) {
  const p = new URLSearchParams();
  if (ctx.file) p.set("file", ctx.file);
  if (ctx.sheet) p.set("sheet", ctx.sheet);
  if (ctx.account) p.set("account", ctx.account);
  if (ctx.q) p.set("q", ctx.q);
  return "#sheet?" + p.toString();
}

function parseSheetHash(hash) {
  const h = String(hash || "");
  if (!h.startsWith("#sheet")) return null;
  const i = h.indexOf("?");
  const p = new URLSearchParams(i >= 0 ? h.slice(i + 1) : "");
  const sheet = p.get("sheet");
  if (!sheet) return null;
  return { file: p.get("file") || "", sheet, account: p.get("account") || "", q: p.get("q") || "" };
}

function activateView(name) {
  $$(".view").forEach((v) => v.classList.remove("active"));
  const v = $("#view-" + name);
  if (v) v.classList.add("active");
}

function openSheetView(ctx) {
  const from = activeViewName();
  if (from !== "sheet") {
    sheetReturnView = from;
    sheetReturnScroll = window.scrollY || 0;   // 记住来源位置，返回时回到原处
  }
  const target = {
    file: (ctx && ctx.file) || "", sheet: (ctx && ctx.sheet) || "",
    account: (ctx && ctx.account) || "", q: (ctx && ctx.q) || "",
  };
  if (!target.sheet) return toast("缺少表格名，无法打开");
  const hash = sheetHashOf(target);
  if (location.hash === hash) { renderSheetView(target); return; }
  location.hash = hash;   // 交给 hashchange 统一渲染
}

async function renderSheetView(ctx) {
  // P1-1：全屏视图自己的请求序号。A→B 快速切换时，A 的迟到响应不得再写标题之外的状态。
  const my = ++sheetViewSeq;
  sheetViewCtx = { ...ctx };
  activateView("sheet");
  const title = $("#sheet-title");
  const sub = $("#sheet-sub");
  const meta = $("#sheet-meta");
  const host = $("#sheet-host");
  if (title) title.textContent = "台账 · " + ctx.sheet;
  if (sub) {
    sub.textContent = [ctx.file || "", ctx.account ? "@" + ctx.account : ""]
      .filter(Boolean).join(" · ") || "本地台账 sheet";
  }
  if (meta) meta.textContent = "只读视图";
  host.innerHTML = `<div class="sheet-state"><span class="muted">加载中…</span></div>`;

  try {
    const st = await loadSheetInto("#sheet-host", ctx.sheet, SHEET_PAGE, ctx.file,
      { account: ctx.account, q: ctx.q });
    if (my !== sheetViewSeq) return;   // 已被更新的表格视图取代
    if (!st) return;
    if (ctx.q) {
      st.filter = ctx.q;
      const inp = $(st.sel).querySelector(".sheet-toolbar input");
      if (inp) inp.value = ctx.q;
      updateSheetBody();
      if (!_sheetVisibleCount()) {
        // 上下文行没命中就退回全表，避免用户以为表是空的
        st.filter = "";
        if (inp) inp.value = "";
        updateSheetBody();
        toast(`未在表中找到 ${ctx.account || ctx.q} 的行，已显示全表`);
      }
    }
    if (meta) meta.textContent = `共 ${st.total} 行 · 只读`;
    if (!st.total) {
      host.insertAdjacentHTML("beforeend",
        `<div class="sheet-empty"><div class="sheet-empty-t">这张表还没有数据</div>
         <div class="muted">流水线入账后才会出现行；可以「← 返回」去跑一次采集。</div></div>`);
    }
    // 内容高度已确定，再对齐一次（loading → 200 行会让页面高度变化）
    focusSheetTarget("#sheet-host");
  } catch (e) {
    if (my !== sheetViewSeq) return;   // 过期失败：不要用旧错误覆盖新表
    const msg = apiErrMsg(e);
    host.innerHTML = `<div class="sheet-state">
      <div class="hint-bad">表格加载失败</div>
      <div class="muted">${esc(msg)}</div>
      <div style="margin-top:12px;display:flex;gap:8px;justify-content:center">
        <button class="btn" onclick="sheetViewReload()">重试</button>
        <button class="btn ghost" onclick="sheetViewBack()">返回</button>
      </div></div>`;
    focusSheetTarget("#sheet-host");
  }
}

function _sheetVisibleCount() {
  try { return _visibleRows().length; } catch (_) { return 0; }
}

/* 表格工具栏「进入表格」：用当前 sheetState 的 file/sheet 切到全屏视图 */
function openSheetViewFromSheet() {
  if (!sheetState || !sheetState.name) return toast("当前没有可打开的表格");
  const account = sheetState.account || "";
  // P2-2：把弹窗里的来源账号与当前搜索词一起带过去（搜索词优先，其次是打开时的预筛词）
  const q = sheetState.filter || sheetState.ctxQ || (account ? "@" + account : "");
  const ctx = { file: sheetState.file || "", sheet: sheetState.name, account, q };
  const inModal = sheetState.sel === "#modal-content";
  if (inModal) closeModal();
  openSheetView(ctx);
}

function sheetViewReload() {
  if (sheetViewCtx.sheet) renderSheetView(sheetViewCtx);
}
function sheetViewExcel() {
  if (sheetViewCtx.sheet) openInExcel(sheetViewCtx.file, sheetViewCtx.sheet);
}
function sheetViewBack() {
  const target = sheetReturnView || "overview";
  if (String(location.hash || "").startsWith("#sheet")) {
    history.back();       // 先退掉 hash，hashchange 里兜底切回来源视图
    setTimeout(() => {
      const v = $("#view-sheet");
      if (v && v.classList.contains("active")) {
        showReturnView(target);
        restoreSheetReturnScroll();
      }
    }, 80);
  } else {
    showReturnView(target);
    restoreSheetReturnScroll();
  }
}
function restoreSheetReturnScroll() {
  window.scrollTo(0, Math.max(0, sheetReturnScroll || 0));
}
/* P2-3：离开表格时清掉 #sheet 路由（replace，不新增历史），避免刷新跳回旧表 */
function clearSheetHash() {
  if (!String(location.hash || "").startsWith("#sheet")) return;
  try {
    history.replaceState(null, "", location.pathname + location.search);
  } catch (_) {
    location.hash = "";
  }
}

/* 作废两张表（预览 / 全屏）在途的加载请求：返回或切视图后，迟到响应只丢弃 */
function abortSheetLoads() {
  sheetReqSeq++;
  sheetViewSeq++;
}

function showReturnView(name) {
  clearSheetHash();
  abortSheetLoads();
  activateView(name);
  const nav = document.querySelector(`.nav-item[data-view="${name}"]`);
  if (nav) {
    $$(".nav-item").forEach((x) => { x.classList.remove("active"); x.removeAttribute("aria-current"); });
    nav.classList.add("active");
    nav.setAttribute("aria-current", "page");
  }
  if (name === "accounts") loadAccounts(true);
  if (name === "overview") loadOverview(true);
  if (name === "bench") loadBench(true);
}

/* 账号 → 台账 sheet：先 resolve 再跳；q 用来定位到该账号所在行 */
async function openAccountSheet(name, q) {
  try {
    const r = await api(`/api/ledger/resolve?account=${encodeURIComponent(name)}`);
    if (!r.sheet) return toast("该账号暂无台账 sheet");
    openSheetView({ file: r.file || "", sheet: r.sheet, account: name, q: q || "@" + name });
  } catch (e) {
    toast("打开表格失败：" + apiErrMsg(e));
  }
}

window.addEventListener("hashchange", () => {
  const p = parseSheetHash(location.hash);
  if (p) { renderSheetView(p); return; }
  const v = $("#view-sheet");
  if (v && v.classList.contains("active")) {
    showReturnView(sheetReturnView || "overview");
    restoreSheetReturnScroll();   // 浏览器后退：回到进入表格前的位置
  }
});

/* P2-12：视图也进 URL —— 后退键回到上一个视图，链接可以把「对标分析」直接发给别人 */
window.addEventListener("popstate", () => {
  const p = parseSheetHash(location.hash);
  if (p) { renderSheetView(p); return; }
  const name = String(location.hash || "").replace(/^#/, "");
  if (name && $("#view-" + name)) { switchView(name); return; }
  const v = $("#view-sheet");
  if (v && v.classList.contains("active")) {
    showReturnView(sheetReturnView || "overview");
    restoreSheetReturnScroll();
  }
});

/* 深链直开：#sheet?sheet=对标记录-xxx&file=... */
(function initSheetRoute() {
  const p = parseSheetHash(location.hash);
  if (p) renderSheetView(p);
})();

/* P2-12：首屏按 #view 直接落到对应视图（#overview / #bench / #accounts / #run …） */
(function initViewRoute() {
  const name = String(location.hash || "").replace(/^#/, "");
  if (!name || name === "sheet") return;
  if ($("#view-" + name)) switchView(name);
})();

boot();
setInterval(() => {
  if (document.visibilityState === "hidden") return;
  if (!$("#view-overview").classList.contains("active")) return;
  if (grpBusy()) return; // P1-1：用户正在分组区操作（搜索/拖拽/勾选/右键），本轮不重建
  if (pollBlocked("overview")) return;   // P2-5：连续失败后指数退避，不再每 60s 打扰
  loadOverview(false);
}, 60000); // 60s：降低台账 sheet 预览的高频读取（多标签叠加时尤其明显）
// 对标分析常驻自新：仅在该页激活、且用户无展开/弹层交互时轮询（见 benchBusy）。
// 不传 refresh → 后端 60s 缓存自然限流，不会每 15s 真算一次。
/* 数据 60s 才更新（后端缓存），所以 30s 轮询 + 指纹比对：最多 1/2 次拿到新数据，
   其余直接跳过重绘，图表入场动画不会每轮重播（2026-10-20 动效整改）。 */
setInterval(pollBench, 30000);

async function backfillTranslation(){ window.location.href="/standalone"; }
