/* ══════════════════════════════════════════════════════════════════════
   tablekit.js — 表格分析增强层（2026-10-20）
   零依赖 · 纯 DOM/SVG。职责单一：给既有表格补「表现层能力」，
   不接管任何业务口径（筛选/排序/聚合仍在 app.js 里，原逻辑不变）。

   提供：
     TK.resizable(table, key)      列宽拖拽 + 双击自适应 + localStorage 记忆
     TK.sortable(table, handler)   表头点击排序（三态循环由调用方决定）
     TK.setSort(table, col, dir)   外部同步排序箭头
     TK.pager(host, opts)          分页条（页码 + 每页条数 + 总数）
     TK.empty(colspan, opts)       空状态行
     TK.skeleton(colspan, n)       骨架屏行
     TK.expandable(tbody, spec)    行展开（detail 惰性渲染）
     TK.compare(host, rows, spec)  选中行对比条（Δ 高亮）
     TK.reveal(tbody)              入场错峰动画（>200 行自动降级）
     TK.bars / TK.donut / TK.line  微型 SVG 图表 + tooltip + 点击回调
     TK.virtualize(table, opts)    大数据量窗口化（>阈值才启用）
   ══════════════════════════════════════════════════════════════════════ */
(function (global) {
  "use strict";

  const NS = "http://www.w3.org/2000/svg";
  const RM = global.matchMedia ? global.matchMedia("(prefers-reduced-motion: reduce)") : null;
  const reduceMotion = () => !!(RM && RM.matches);

  /* ── 小工具 ───────────────────────────────────────────── */
  const esc = (s) =>
    String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const num = (v) => {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  };

  function debounce(fn, ms) {
    let t = 0;
    return function () {
      const args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(() => fn.apply(self, args), ms);
    };
  }

  // 分片渲染：把重活切成 rAF 批次，避免主线程长任务
  function rafBatch(items, each, done, perFrame) {
    const step = perFrame || 200;
    let i = 0;
    function run() {
      const end = Math.min(items.length, i + step);
      for (; i < end; i++) each(items[i], i);
      if (i < items.length) requestAnimationFrame(run);
      else if (done) done();
    }
    run();
  }

  function store(key, value) {
    try {
      if (value === undefined) return JSON.parse(localStorage.getItem("tk:" + key) || "null");
      localStorage.setItem("tk:" + key, JSON.stringify(value));
      return value;
    } catch (e) {
      return null;
    }
  }

  function tip() {
    let el = document.querySelector(".tk-tip");
    if (!el) {
      el = document.createElement("div");
      el.className = "tk-tip";
      el.setAttribute("role", "status");
      document.body.appendChild(el);
    }
    return el;
  }

  function showTip(html, x, y) {
    const t = tip();
    t.innerHTML = html;
    t.classList.add("on");
    const w = t.offsetWidth, h = t.offsetHeight;
    const vw = document.documentElement.clientWidth;
    t.style.left = Math.min(Math.max(8, x - w / 2), vw - w - 8) + "px";
    t.style.top = Math.max(8, y - h - 12) + "px";
  }
  function hideTip() {
    const t = document.querySelector(".tk-tip");
    if (t) t.classList.remove("on");
  }

  /* ── 列宽：拖拽 / 双击自适应 / 记忆 ─────────────────────── */
  function _ths(table) {
    const head = table.tHead || (table.querySelector("thead") || { rows: [] });
    const row = head.rows && head.rows[0];
    return row ? Array.from(row.cells) : [];
  }

  // 首次挂载：把实测宽度固化成 px，再切 fixed，避免 fixed 后列被均分导致跳动
  // ★ P0-1：容器窄于 TK_FIX_MIN 时，量到的是"被浏览器压扁后的最小宽度"（中文列约 27px）。
  //   此时若固化 + fixed，列宽会被永久锁死，表格宽度恰好等于容器宽（scrollWidth==clientWidth），
  //   连横向滚动条都不出现——手机端整张表变成"一列一个字"。窄容器一律不固化、不切 fixed，
  //   交给 auto 布局 + nowrap 自然撑开，由 .table-wrap 提供横向滚动。
  const TK_FIX_MIN = 720;
  function _isFluid(table) { return table.dataset.tkFluid === "1"; }
  /* 2026-10-22 独立验收 R3：流体模式。
     默认（未手工调过列宽）列宽由调用方给的百分比决定 + 表宽 100%，1280 桌面正好铺满容器；
     一旦用户拖手柄 / 双击自适应，就「冻结」成 px 并把表宽放大到列宽之和 ——
     此时横向滚动是明确保留的行为，与首次进入不混淆。 */
  function _applyFluid(table) {
    table.dataset.tkFluid = "1";
    table.classList.add("tk-fluid");
    table.classList.remove("tk-fixed");
    table.style.tableLayout = "fixed";
    table.style.width = "100%";
    // 不要写 inline min-width:0 —— 那会把 CSS 里 @media(max-width:1279px) 的 900px 兜底压掉，
    // 窄视口下 8 列会被压成「一列两个字」。清空让样式表的 min-width 生效。
    table.style.minWidth = "";
  }
  /* 退出流体：表宽交回列宽之和（有记忆 / 手工拉宽时不允许再被 100% 挤压） */
  function _clearFluid(table) {
    if (!_isFluid(table)) return;
    table.dataset.tkFluid = "";
    table.classList.remove("tk-fluid");
    table.style.width = "";
    table.style.minWidth = "";
  }
  /* px 模式下让表宽跟着列宽之和走：拖宽一列时整表变宽、容器出现横向滚动，
     而不是把别的列压回去（那样「手工拉宽」就没有可见结果）。 */
  function _syncWidth(table) {
    const ths = _ths(table);
    let total = 0;
    ths.forEach((th) => { total += Math.round(parseFloat(th.style.width) || th.getBoundingClientRect().width || 0); });
    if (total > 0) {
      table.style.width = total + "px";
      table.style.minWidth = total + "px";
    }
  }
  function _freeze(table) {
    if (!_isFluid(table)) return;
    const ths = _ths(table);
    const ws = ths.map((th) => Math.round(th.getBoundingClientRect().width));
    table.dataset.tkFluid = "";
    table.classList.remove("tk-fluid");
    table.classList.add("tk-fixed");
    table.dataset.tkWidths = "1";
    ths.forEach((th, i) => { if (ws[i] > 0) th.style.width = ws[i] + "px"; });
    _syncWidth(table);
  }
  function _adoptWidths(table) {
    if (table.dataset.tkWidths === "1") return;
    if (_isFluid(table)) return;             // 流体模式不固化 px，否则窗口一窄就再也收不回来
    const ths = _ths(table);
    if (!ths.length) return;
    const wrap = table.closest(".table-wrap") || table.parentElement;
    const avail = wrap ? Math.round(wrap.clientWidth) : 0;
    if (avail && avail < TK_FIX_MIN) return;   // 窄容器：等变宽后再固化
    const ws = ths.map((th) => Math.round(th.getBoundingClientRect().width));
    if (!ws.some((w) => w > 0)) return;      // 表格隐藏（display:none）时量不到，跳过
    ths.forEach((th, i) => {
      if (!th.style.width && ws[i] > 0) th.style.width = ws[i] + "px";
    });
    table.style.tableLayout = "fixed";
    table.classList.add("tk-fixed");
    table.dataset.tkWidths = "1";
  }

  /* 重置列宽：清掉记忆 + 退回流体（未手工调过即铺满容器）。调用方随后重渲染即可。 */
  function resetWidths(table, key) {
    if (!table) return table;
    const KEY = key || table.id || "tbl";
    try { localStorage.removeItem("tk:" + KEY); } catch (e) { /* noop */ }
    table.dataset.tkWidths = "";
    table.dataset.tkFluid = "";
    table.classList.remove("tk-fixed");
    table.style.width = "";
    table.style.minWidth = "";
    table.style.tableLayout = "";
    _ths(table).forEach((th) => { th.style.width = ""; });
    return table;
  }

  function resizable(table, key, opts) {
    if (!table) return table;
    opts = opts || {};
    const thsNow = _ths(table);
    if (!thsNow.length) return table;
    const KEY = key || table.id || "tbl";
    const saved = store(KEY);
    const fluid = !!opts.fluid && !(saved && saved.length);
    if (fluid) _applyFluid(table);
    else _clearFluid(table);
    // 表头未重建（已有手柄）→ 直接复用；重建过就重挂一次（innerHTML 会把 span 冲掉）
    if (thsNow[0].querySelector(".tk-grip")) return table;
    table.dataset.tkResize = "1";
    table.classList.add("tk-resizable");
    if (!fluid) _adoptWidths(table);
    // 窄屏首屏不固化 —— 窗口拉宽后补一次（同一个表只挂一次监听）
    if (!table.dataset.tkWatch && typeof window !== "undefined") {
      table.dataset.tkWatch = "1";
      window.addEventListener("resize", debounce(() => {
        if (table.isConnected && !_isFluid(table)) _adoptWidths(table);
      }, 300));
    }
    if (saved && saved.length) {
      thsNow.forEach((th, i) => {
        if (saved[i] > 24) th.style.width = saved[i] + "px";
      });
      _syncWidth(table);                    // 记忆是 px：表宽 = 列宽之和，容器负责横向滚动
    } else if (!fluid && opts.widths && opts.widths.length) {
      // 2026-10-22：允许调用方给一套默认列宽（首次进入 / 换 key 后生效）。
      // 不传的区域仍是原来的「实测后固化」，行为不变。0 / undefined 表示该列自适应剩余宽度。
      thsNow.forEach((th, i) => {
        const w = opts.widths[i];
        if (w > 24) th.style.width = w + "px";
        else th.style.width = "";
      });
    }
    const persist = debounce(() => {
      store(KEY, _ths(table).map((th) => Math.round(th.getBoundingClientRect().width)));
    }, 400);

    thsNow.forEach((th, i) => {
      if (th.querySelector(".tk-grip")) return;
      const g = document.createElement("span");
      g.className = "tk-grip";
      g.setAttribute("aria-hidden", "true");
      g.title = "拖动调整列宽 · 双击自适应";
      th.appendChild(g);

      let startX = 0, startW = 0, dragging = false, pid = null;
      const onMove = (ev) => {
        if (!dragging) return;
        const w = Math.max(opts.min || 56, startW + (ev.clientX - startX));
        th.style.width = w + "px";
        _syncWidth(table);
      };
      const onUp = () => {
        if (!dragging) return;
        dragging = false;
        document.body.classList.remove("tk-resizing");
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
        if (g.releasePointerCapture && pid != null) {
          try { g.releasePointerCapture(pid); } catch (e) { /* noop */ }
        }
        pid = null;
        persist();
      };
      g.addEventListener("pointerdown", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();               // 别触发表头排序
        // 手工调列宽 → 从「铺满容器」切到 px 模式（此后允许横向滚动，见 _freeze 注释）
        _freeze(table);
        // 关键：抓住指针。拖到表外松手时，浏览器把 click 派给 down/up 的共同祖先（th 或 table），
        // 只判 event.target 是否为手柄会漏掉这种情况；指针捕获后后续指针事件与兼容鼠标事件都重定向到手柄。
        pid = ev.pointerId == null ? null : ev.pointerId;
        if (g.setPointerCapture && pid != null) {
          try { g.setPointerCapture(pid); } catch (e) { /* 环境不支持时靠下方 target 兜底 */ }
        }
        dragging = true;
        startX = ev.clientX;
        startW = th.getBoundingClientRect().width;
        document.body.classList.add("tk-resizing");
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
      });
      // 手柄上的任何交互都不是排序：指针捕获保证补发的 click 落在手柄，这里再兜一层。
      g.addEventListener("click", (ev) => { ev.preventDefault(); ev.stopPropagation(); });
      // 双击：按当前内容自适应（测量每个 td 文本宽度）
      g.addEventListener("dblclick", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        _freeze(table);
        const idx = Array.from(th.parentNode.cells).indexOf(th);
        const cells = Array.from(table.tBodies[0] ? table.tBodies[0].rows : []).slice(0, 80);
        let max = th.textContent.length * 8 + 32;
        cells.forEach((r) => {
          const c = r.cells[idx];
          if (!c) return;
          const t = c.textContent.trim();
          max = Math.max(max, Math.min(360, t.length * 7.6 + 26));
        });
        th.style.width = Math.round(max) + "px";
        _syncWidth(table);
        persist();
      });
    });
    return table;
  }

  /* ── 排序：表头点击 ───────────────────────────────────── */
  function sortable(table, handler, opts) {
    if (!table) return;
    opts = opts || {};
    const ths = _ths(table);
    ths.forEach((th, i) => {
      if (th.dataset.tkNoSort === "1" || opts.skip && opts.skip.indexOf(i) > -1) return;
      th.classList.add("tk-sortable");
      th.setAttribute("tabindex", th.getAttribute("tabindex") || "0");
      th.setAttribute("role", "columnheader");
      const trigger = (ev) => {
        // 列宽手柄不是排序控件：不管事件从哪里出发，只要命中 .tk-grip 就不排序。
        const t = ev && ev.target;
        if (t && t.closest && t.closest(".tk-grip")) return;
        if (ev) ev.preventDefault();
        handler(i, th);
      };
      th.addEventListener("click", trigger);
      th.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") trigger(ev);
      });
    });
  }

  function setSort(table, col, dir) {
    if (!table) return;
    _ths(table).forEach((th, i) => {
      th.classList.remove("tk-asc", "tk-desc");
      if (i === col) {
        th.classList.add(dir > 0 ? "tk-asc" : "tk-desc");
        th.setAttribute("aria-sort", dir > 0 ? "ascending" : "descending");
      } else {
        th.removeAttribute("aria-sort");
      }
    });
  }

  /* ── 分页条 ───────────────────────────────────────────── */
  function pageList(page, pages) {
    const out = [];
    const push = (v) => out.push(v);
    if (pages <= 7) {
      for (let i = 1; i <= pages; i++) push(i);
      return out;
    }
    push(1);
    const lo = Math.max(2, page - 1), hi = Math.min(pages - 1, page + 1);
    if (lo > 2) push("…");
    for (let i = lo; i <= hi; i++) push(i);
    if (hi < pages - 1) push("…");
    push(pages);
    return out;
  }

  function pager(host, o) {
    if (!host) return;
    const total = Math.max(0, num(o.total));
    const size = Math.max(1, num(o.size));
    const pages = Math.max(1, Math.ceil(total / size));
    const page = Math.min(Math.max(1, num(o.page)), pages);
    if (total <= 0) { host.innerHTML = ""; return; }
    const sizes = o.sizes || [25, 50, 100, 200];
    const btns = pageList(page, pages)
      .map((p) =>
        p === "…"
          ? `<span class="tk-pg-gap">…</span>`
          : `<button type="button" class="tk-pg-b${p === page ? " on" : ""}" data-pg="${p}"
               aria-current="${p === page ? "page" : "false"}">${p}</button>`)
      .join("");
    host.innerHTML = `<div class="tk-pager">
      <span class="tk-pg-info">共 <b>${total}</b> ${esc(o.unit || "条")} · 第 ${page} / ${pages} 页</span>
      <span class="tk-pg-btns">
        <button type="button" class="tk-pg-b" data-pg="${page - 1}" ${page <= 1 ? "disabled" : ""} aria-label="上一页">‹</button>
        ${btns}
        <button type="button" class="tk-pg-b" data-pg="${page + 1}" ${page >= pages ? "disabled" : ""} aria-label="下一页">›</button>
      </span>
      <span class="tk-pg-size">
        每页
        <select aria-label="每页条数">${sizes.map((s) => `<option value="${s}" ${s === size ? "selected" : ""}>${s}</option>`).join("")}</select>
        条
      </span>
    </div>`;
    host.querySelectorAll("[data-pg]").forEach((b) => {
      b.addEventListener("click", () => {
        const p = Number(b.getAttribute("data-pg"));
        if (p >= 1 && p <= pages && p !== page && o.onChange) o.onChange(p, size);
      });
    });
    const sel = host.querySelector("select");
    if (sel) sel.addEventListener("change", () => { if (o.onChange) o.onChange(1, Number(sel.value)); });
  }

  /* ── 空态 / 骨架 ──────────────────────────────────────── */
  function empty(colspan, o) {
    o = o || {};
    return `<tr class="tk-empty-row"><td colspan="${colspan}">
      <div class="tk-empty">
        <div class="tk-empty-ic" aria-hidden="true">${o.icon || "◍"}</div>
        <div class="tk-empty-t">${esc(o.title || "没有匹配的数据")}</div>
        <div class="tk-empty-d">${esc(o.desc || "")}</div>
        ${o.action ? `<div class="tk-empty-a">${o.action}</div>` : ""}
      </div></td></tr>`;
  }

  function skeleton(colspan, n) {
    let out = "";
    for (let i = 0; i < (n || 4); i++) {
      out += `<tr class="tk-sk-row" style="--i:${i}"><td colspan="${colspan}"><div class="tk-sk"></div></td></tr>`;
    }
    return out;
  }

  /* ── 行展开 ───────────────────────────────────────────── */
  function expandable(tbody, spec) {
    // spec: { rowOf(tr) -> data, html(data) -> string, colspan }
    // 注意：绑定标记必须避开 data-tk-exp —— 行内展开按钮就用这个属性，
    // 若把标记也写成 data-tk-exp，tbody 会自己命中选择器从而吞掉整表点击。
    if (!tbody || tbody.dataset.tkBound === "1") return;
    tbody.dataset.tkBound = "1";
    tbody.addEventListener("click", (ev) => {
      const btn = ev.target.closest && ev.target.closest("[data-tk-exp]");
      if (!btn) return;
      ev.stopPropagation();
      const tr = btn.closest("tr");
      if (!tr) return;
      const next = tr.nextElementSibling;
      if (next && next.classList.contains("tk-exp")) {
        next.remove();
        btn.setAttribute("aria-expanded", "false");
        tr.classList.remove("tk-open");
        return;
      }
      const data = spec.rowOf(tr);
      const row = document.createElement("tr");
      row.className = "tk-exp";
      row.innerHTML = `<td colspan="${spec.colspan}">${spec.html(data)}</td>`;
      tr.parentNode.insertBefore(row, tr.nextSibling);
      btn.setAttribute("aria-expanded", "true");
      tr.classList.add("tk-open");
      // 高度过渡：先 0 后实测
      if (!reduceMotion()) {
        row.style.height = "0";
        requestAnimationFrame(() => { row.style.height = row.scrollHeight + "px"; });
        setTimeout(() => { row.style.height = ""; }, 300);
      }
    });
  }

  /* ── 选中对比 ─────────────────────────────────────────── */
  const COMPARE_SPEC = {
    plays: { label: "播放", fmt: (v) => num(v).toLocaleString() },
    likes: { label: "点赞", fmt: (v) => num(v).toLocaleString() },
    comments: { label: "评论", fmt: (v) => num(v).toLocaleString() },
    shares: { label: "转发", fmt: (v) => num(v).toLocaleString() },
    collects: { label: "收藏", fmt: (v) => num(v).toLocaleString() },
    engagement: { label: "互动率", fmt: (v) => num(v).toFixed(2) + "%" },
  };

  function compare(host, rows, spec, labelOf) {
    if (!host) return;
    spec = spec || COMPARE_SPEC;
    rows = rows || [];
    if (rows.length < 2) { host.innerHTML = ""; host.classList.remove("on"); return; }
    const keys = Object.keys(spec).filter((k) => rows.every((r) => r[k] !== undefined && r[k] !== null));
    const label = (r, i) => (labelOf ? labelOf(r, i) : (r.account || r.title || "行 " + (i + 1)));
    const head = rows.map((r, i) =>
      `<th><span class="tk-cmp-i">${i + 1}</span>${esc(label(r, i))}</th>`).join("");
    const body = keys.map((k) => {
      const vals = rows.map((r) => num(r[k]));
      const best = Math.max.apply(null, vals), worst = Math.min.apply(null, vals);
      const spread = worst ? ((best - worst) / worst) * 100 : 0;
      return `<tr><th>${spec[k].label}</th>${vals
        .map((v) => {
          const cls = v === best && best !== worst ? "hi" : v === worst && best !== worst ? "lo" : "";
          return `<td class="${cls}">${spec[k].fmt(v)}</td>`;
        })
        .join("")}<td class="sp">${spread > 0 ? "↕ " + spread.toFixed(0) + "%" : "—"}</td></tr>`;
    }).join("");
    host.innerHTML = `<div class="tk-cmp">
      <div class="tk-cmp-h"><b>对比</b><span class="muted">已选 ${rows.length} 条 · 最高值标红、最低值标绿</span>
        <button type="button" class="btn ghost sm" data-tk-cmp-clear>清除选择</button></div>
      <div class="tk-cmp-wrap"><table class="tk-cmp-t"><thead><tr><th>指标</th>${head}<th class="sp">极差</th></tr></thead>
      <tbody>${body}</tbody></table></div>
    </div>`;
    host.classList.add("on");
    const clr = host.querySelector("[data-tk-cmp-clear]");
    if (clr) clr.addEventListener("click", () => { host.innerHTML = ""; host.classList.remove("on"); if (global.tkClearSel) global.tkClearSel(); });
  }

  /* ── 入场动画 ─────────────────────────────────────────── */
  function reveal(tbody, opts) {
    if (!tbody || reduceMotion()) return;
    opts = opts || {};
    const rows = Array.from(tbody.rows).filter((r) => !r.classList.contains("tk-exp"));
    if (rows.length > (opts.max || 200)) return;     // 大表不逐行动画，只做容器淡入
    rows.forEach((tr, i) => {
      tr.style.setProperty("--i", Math.min(i, 24));
      tr.classList.remove("tk-in");
    });
    // 强制重排后加类，保证动画重播
    void tbody.offsetHeight;
    rows.forEach((tr) => tr.classList.add("tk-in"));
  }

  /* ── 迷你图表（SVG） ──────────────────────────────────── */
  function svg(w, h, cls) {
    const s = document.createElementNS(NS, "svg");
    s.setAttribute("viewBox", `0 0 ${w} ${h}`);
    s.setAttribute("class", "tk-chart " + (cls || ""));
    s.setAttribute("role", "img");
    return s;
  }
  function mk(tag, attrs) {
    const el = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach((k) => el.setAttribute(k, attrs[k]));
    return el;
  }

  // 柱状：data = [{key, label, value, tone?}]
  function bars(el, data, o) {
    if (!el) return;
    o = o || {};
    const W = o.width || 520, H = o.height || 132, pad = { t: 12, r: 8, b: 26, l: 8 };
    el.innerHTML = "";
    const max = Math.max.apply(null, data.map((d) => num(d.value)).concat([1]));
    const innerW = W - pad.l - pad.r, innerH = H - pad.t - pad.b;
    const bw = Math.max(6, Math.min(56, (innerW / Math.max(data.length, 1)) * 0.62));
    const step = innerW / Math.max(data.length, 1);
    const s = svg(W, H, "tk-bars");
    data.forEach((d, i) => {
      const h = Math.max(2, (num(d.value) / max) * innerH);
      const x = pad.l + step * i + (step - bw) / 2;
      const y = pad.t + innerH - h;
      const r = mk("rect", { x, y, width: bw, height: h, rx: Math.min(4, bw / 3),
        class: "tk-bar" + (o.activeKey === d.key ? " on" : "") + (d.tone ? " " + d.tone : ""),
        "data-k": d.key, tabindex: "0", role: "button" });
      r.style.transformOrigin = `${x + bw / 2}px ${pad.t + innerH}px`;
      // 入场编排：30ms 递进、总时长 ≤ 0.38s+；静默刷新（o.animate === false）直接落位
      if (o.animate === false) {
        r.style.animation = "none";
        r.style.transform = "none";
      } else {
        r.style.animationDelay = (i * 30) + "ms";
      }
      const hit = (ev) => {
        showTip(`<b>${esc(d.label || d.key)}</b><span>${o.valueFmt ? o.valueFmt(d.value) : num(d.value).toLocaleString()}</span>`, ev.clientX, ev.clientY);
      };
      r.addEventListener("pointermove", hit);
      r.addEventListener("pointerleave", hideTip);
      r.addEventListener("click", () => { hideTip(); if (o.onPick) o.onPick(d.key, d); });
      r.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); if (o.onPick) o.onPick(d.key, d); } });
      s.appendChild(r);
      const t = mk("text", { x: pad.l + step * i + step / 2, y: H - 8, class: "tk-bar-lb", "text-anchor": "middle" });
      t.textContent = d.label || d.key;
      s.appendChild(t);
    });
    el.appendChild(s);
  }

  // 环形：data = [{key, label, value}]；带中心合计
  function donut(el, data, o) {
    if (!el) return;
    o = o || {};
    el.innerHTML = "";
    const D = o.size || 148, R = D / 2, r0 = R * 0.58, cx = R, cy = R;
    const total = data.reduce((a, d) => a + num(d.value), 0);
    const s = svg(D, D, "tk-donut");
    if (!total) {
      s.appendChild(mk("circle", { cx, cy, r: R - 6, class: "tk-dn-track" }));
      el.appendChild(s);
      return;
    }
    const palette = o.colors || ["#b45309", "#0f6b60", "#1f4e79", "#8a3f07", "#6b7280", "#a16207", "#0e7490", "#9d174d"];
    let acc = 0;
    data.forEach((d, i) => {
      const frac = num(d.value) / total;
      const a0 = acc * Math.PI * 2 - Math.PI / 2;
      acc += frac;
      const a1 = acc * Math.PI * 2 - Math.PI / 2;
      const large = frac > 0.5 ? 1 : 0;
      const x0 = cx + R * Math.cos(a0), y0 = cy + R * Math.sin(a0);
      const x1 = cx + R * Math.cos(a1), y1 = cy + R * Math.sin(a1);
      const xi1 = cx + r0 * Math.cos(a1), yi1 = cy + r0 * Math.sin(a1);
      const xi0 = cx + r0 * Math.cos(a0), yi0 = cy + r0 * Math.sin(a0);
      const path = mk("path", {
        d: `M${x0} ${y0} A${R} ${R} 0 ${large} 1 ${x1} ${y1} L${xi1} ${yi1} A${r0} ${r0} 0 ${large} 0 ${xi0} ${yi0} Z`,
        fill: palette[i % palette.length],
        class: "tk-dn-slice" + (o.activeKey === d.key ? " on" : o.activeKey ? " dim" : ""),
        "data-k": d.key, tabindex: "0", role: "button",
      });
      path.style.animationDelay = o.animate === false ? "0ms" : (i * 40) + "ms";
      if (o.animate === false) {
        path.style.animation = "none";
        path.style.opacity = o.activeKey && o.activeKey !== d.key ? ".3" : "1";
      }
      const hit = (ev) =>
        showTip(`<b>${esc(d.label || d.key)}</b><span>${o.valueFmt ? o.valueFmt(d.value) : num(d.value).toLocaleString()} · ${(frac * 100).toFixed(1)}%</span>`, ev.clientX, ev.clientY);
      path.addEventListener("pointermove", hit);
      path.addEventListener("pointerleave", hideTip);
      path.addEventListener("click", () => { hideTip(); if (o.onPick) o.onPick(d.key, d); });
      path.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); if (o.onPick) o.onPick(d.key, d); } });
      s.appendChild(path);
    });
    const c1 = mk("text", { x: cx, y: cy - 2, class: "tk-dn-v", "text-anchor": "middle" });
    c1.textContent = o.centerValue || num(total).toLocaleString();
    const c2 = mk("text", { x: cx, y: cy + 15, class: "tk-dn-c", "text-anchor": "middle" });
    c2.textContent = o.centerLabel || "合计";
    s.appendChild(c1);
    s.appendChild(c2);
    el.appendChild(s);
    if (o.legend !== false) {
      const lg = document.createElement("div");
      lg.className = "tk-dn-legend";
      lg.innerHTML = data.map((d, i) =>
        `<button type="button" class="tk-lg-i${o.activeKey === d.key ? " on" : ""}" data-k="${esc(d.key)}">
          <i style="background:${palette[i % palette.length]}"></i>${esc(d.label || d.key)}
          <b>${o.valueFmt ? o.valueFmt(d.value) : num(d.value).toLocaleString()}</b></button>`).join("");
      lg.querySelectorAll("[data-k]").forEach((b) =>
        b.addEventListener("click", () => { if (o.onPick) o.onPick(b.getAttribute("data-k"), null); }));
      el.appendChild(lg);
    }
  }

  // 折线 + 面积：data = [{key, label, value}]
  function line(el, data, o) {
    if (!el) return;
    o = o || {};
    el.innerHTML = "";
    const showAxis = o.showAxis !== false;
    const W = o.width || 520, H = o.height || 132, pad = { t: 16, r: 10, b: 22, l: showAxis ? 38 : 10 };
    const innerW = W - pad.l - pad.r, innerH = H - pad.t - pad.b;
    const max = Math.max.apply(null, data.map((d) => num(d.value)).concat([1]));
    const step = data.length > 1 ? innerW / (data.length - 1) : innerW;
    const pt = (d, i) => [pad.l + i * step, pad.t + innerH - (num(d.value) / max) * innerH];
    const short = (v) => (v >= 1e8 ? (v / 1e8).toFixed(1) + "亿"
      : v >= 1e4 ? (v / 1e4).toFixed(1) + "万" : String(Math.round(v)));
    const s = svg(W, H, "tk-line");
    // 横向参照线：只画 50% 一条虚线 + 底部基线；纵轴只标 0 与中值。
    // 顶部刻度故意不标 —— 峰值点旁已有数值，两处重复会显得"数字打架"。
    if (showAxis) {
      const my = pad.t + innerH * 0.5;
      s.appendChild(mk("line", { x1: pad.l, y1: my, x2: W - pad.r, y2: my, class: "tk-ln-grid" }));
      const t0 = mk("text", { x: pad.l - 6, y: pad.t + innerH + 3, class: "tk-ln-ax", "text-anchor": "end" });
      t0.textContent = "0";
      s.appendChild(t0);
      const tm = mk("text", { x: pad.l - 6, y: my + 3, class: "tk-ln-ax", "text-anchor": "end" });
      tm.textContent = short(max * 0.5);
      s.appendChild(tm);
    }
    const grid = mk("line", { x1: pad.l, y1: pad.t + innerH, x2: W - pad.r, y2: pad.t + innerH, class: "tk-ln-axis" });
    s.appendChild(grid);
    if (data.length) {
      const pts = data.map(pt);
      const dLine = pts.map((p, i) => (i ? "L" : "M") + p[0] + " " + p[1]).join(" ");
      const dArea = dLine + ` L${pts[pts.length - 1][0]} ${pad.t + innerH} L${pts[0][0]} ${pad.t + innerH} Z`;
      s.appendChild(mk("path", { d: dArea, class: "tk-ln-area" }));
      const path = mk("path", { d: dLine, class: "tk-ln-path" });
      s.appendChild(path);
      const len = path.getTotalLength ? path.getTotalLength() : 0;
      if (len && !reduceMotion() && o.animate !== false) {
        path.style.strokeDasharray = len;
        path.style.strokeDashoffset = len;
        path.style.transition = "stroke-dashoffset .5s cubic-bezier(.16,1,.3,1)";
        requestAnimationFrame(() => { path.style.strokeDashoffset = 0; });
      }
      pts.forEach((p, i) => {
        const c = mk("circle", { cx: p[0], cy: p[1], r: o.activeKey === data[i].key ? 4.5 : 3,
          class: "tk-ln-dot" + (o.activeKey === data[i].key ? " on" : ""), "data-k": data[i].key,
          tabindex: "0", role: "button" });
        const hit = (ev) => showTip(`<b>${esc(data[i].label || data[i].key)}</b><span>${o.valueFmt ? o.valueFmt(data[i].value) : num(data[i].value).toLocaleString()}</span>`, ev.clientX, ev.clientY);
        c.addEventListener("pointermove", hit);
        c.addEventListener("pointerleave", hideTip);
        c.addEventListener("click", () => { hideTip(); if (o.onPick) o.onPick(data[i].key, data[i]); });
        s.appendChild(c);
      });
      // 峰值标注：放在点右下方，避开顶部边界与纵轴刻度；右端越界时改左对齐
      if (o.showMax !== false) {
        let mi = 0;
        data.forEach((d, i) => { if (num(d.value) > num(data[mi].value)) mi = i; });
        if (num(data[mi].value) > 0) {
          const [mx, my2] = pt(data[mi], mi);
          const right = mx + 8 < W - pad.r - 28;
          const lb = mk("text", {
            x: right ? mx + 8 : mx - 8, y: my2 + 12,
            class: "tk-ln-peak", "text-anchor": right ? "start" : "end",
          });
          lb.textContent = short(num(data[mi].value));
          s.appendChild(lb);
          s.appendChild(mk("circle", { cx: mx, cy: my2, r: 4, class: "tk-ln-halo" }));
        }
      }
      // 稀疏 x 轴标签（最多 7 个）；首尾贴边对齐，避免半个日期被裁
      const every = Math.max(1, Math.ceil(data.length / 7));
      data.forEach((d, i) => {
        if (i % every) return;
        const x = pt(d, i)[0];
        const anchor = i === 0 ? "start" : (i >= data.length - 1 ? "end" : "middle");
        const t = mk("text", { x, y: H - 5, class: "tk-ln-lb", "text-anchor": anchor });
        t.textContent = d.label || d.key;
        s.appendChild(t);
      });
    }
    el.appendChild(s);
  }

  /* ── 分档清单（HTML 版）：档位 / 条数 / 占比 三列对齐 ──
     旧版用 4 根细柱：档位多时最矮的一根只有 2px，数值全靠 hover，等于没信息；
     这里改成"一行一档"的横向条，标签、数量、占比同排，扫读即得。 */
  function bands(el, data, o) {
    if (!el) return;
    o = o || {};
    const rows = data || [];
    const total = rows.reduce((s, d) => s + num(d.value), 0);
    const max = Math.max.apply(null, rows.map((d) => num(d.value)).concat([1]));
    el.innerHTML = `<div class="tk-bands">${rows.map((d) => {
      const v = num(d.value);
      const pct = total ? (v / total) * 100 : 0;
      const w = v ? Math.max(2, (v / max) * 100) : 0;
      const on = o.activeKey === d.key;
      return `<button type="button" class="tk-band${on ? " on" : ""}${v ? "" : " is-zero"}"
          data-k="${esc(d.key)}" aria-pressed="${on ? "true" : "false"}"
          title="${esc(d.label || d.key)}：${v.toLocaleString()} 条 · ${pct.toFixed(1)}%">
        <span class="tk-band-l">${esc(d.label || d.key)}</span>
        <span class="tk-band-track"><i style="width:${w}%"></i></span>
        <span class="tk-band-n">${v.toLocaleString()}<span class="u">条</span></span>
        <span class="tk-band-p">${pct.toFixed(1)}%</span>
      </button>`;
    }).join("")}</div>`;
    el.querySelectorAll(".tk-band").forEach((b) => {
      const k = b.getAttribute("data-k");
      const d = rows.find((x) => String(x.key) === k);
      const pick = () => { hideTip(); if (o.onPick) o.onPick(k, d); };
      b.addEventListener("click", pick);
      b.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); }
      });
      b.addEventListener("pointermove", (ev) => {
        if (!d) return;
        const pct = total ? ((num(d.value) / total) * 100).toFixed(1) : "0.0";
        showTip(`<b>${esc(d.label || d.key)}</b><span>${num(d.value).toLocaleString()} 条 · ${pct}%</span>`,
          ev.clientX, ev.clientY);
      });
      b.addEventListener("pointerleave", hideTip);
    });
  }

  /* ── 大数据量窗口化 ───────────────────────────────────── */
  // 只对「一次性渲染超过阈值」的表启用：把行分批 append，避免长任务卡顿。
  function virtualize(table, rows, renderRow, opts) {
    opts = opts || {};
    const tbody = table.tBodies[0];
    if (!tbody) return;
    const threshold = opts.threshold || 400;
    tbody.innerHTML = "";
    if (rows.length <= threshold) {
      tbody.innerHTML = rows.map(renderRow).join("");
      return;
    }
    const frag = document.createDocumentFragment();
    const chunks = [];
    for (let i = 0; i < rows.length; i += threshold) {
      const div = document.createElement("tbody");
      div.innerHTML = rows.slice(i, i + threshold).map(renderRow).join("");
      chunks.push(div);
    }
    rafBatch(chunks, (d) => {
      while (d.firstChild) frag.appendChild(d.firstChild);
      tbody.appendChild(frag);
    });
  }

  global.TK = {
    esc, num, debounce, resizable, resetWidths, sortable, setSort, pager, empty, skeleton,
    expandable, compare, reveal, bars, donut, line, bands, virtualize, rafBatch, store,
    reduceMotion, hideTip,
  };
})(window);
