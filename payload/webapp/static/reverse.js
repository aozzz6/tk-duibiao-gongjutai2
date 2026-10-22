/* TikTok 对标工作台 · 提示词反推（tk_reverse）前端
   独立文件：删掉 index.html 里对应的 <link>/<script> 与 nav 项即可完全回滚。
   依赖 app.js 暴露的全局工具：$ / $$ / api / toast / fmtTime。 */
(function () {
  "use strict";

  var MODEL_KEY = "tkrev_model";
  var ITEMS = [];
  var TIMER = null;

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  var STAGE = {
    pending: ["排队中", "rvb-idle"],
    preparing: ["生成素材中", "rvb-run"],
    analyzing: ["AI 反推中", "rvb-run"],
    ready: ["素材就绪", "rvb-ready"],
    done: ["已完成", "rvb-done"],
    failed: ["失败", "rvb-fail"],
    stopped: ["已停止", "rvb-idle"],
  };

  function badge(stage) {
    var s = STAGE[stage] || ["未知", "rvb-idle"];
    return '<span class="rv-badge ' + s[1] + '">' + esc(s[0]) + "</span>";
  }

  function fmtDur(s) {
    var n = Number(s) || 0;
    return n ? n.toFixed(2) + "s" : "-";
  }

  /* ── 模型下拉 ── */
  function loadModels() {
    var sel = $("#rv-model");
    if (!sel) return Promise.resolve();
    return api("/api/reverse/models")
      .then(function (d) {
        var cur = "";
        try { cur = localStorage.getItem(MODEL_KEY) || ""; } catch (e) { cur = ""; }
        var val = cur || d["default"] || "";
        sel.innerHTML = (d.items || [])
          .map(function (m) {
            return '<option value="' + esc(m.id) + '"' +
              (m.id === val ? " selected" : "") + ">" + esc(m.name) + "</option>";
          })
          .join("");
      })
      .catch(function () {
        sel.innerHTML = '<option value="">（请在软件设置中配置视觉模型）</option>';
      });
  }

  function curModel() {
    var sel = $("#rv-model");
    var v = sel ? sel.value : "";
    try { if (v) localStorage.setItem(MODEL_KEY, v); } catch (e) { /* 隐私模式忽略 */ }
    return v;
  }

  /* ── 列表 ── */
  function load(manual) {
    if (TIMER) { clearTimeout(TIMER); TIMER = null; }
    return api("/api/reverse/list")
      .then(function (d) {
        ITEMS = d.items || [];
        render();
        if (manual && $("#rv-updated")) $("#rv-updated").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
        schedule();
      })
      .catch(function (e) {
        var host = $("#rv-list");
        if (host) host.innerHTML = '<div class="panel muted">读取失败：' + esc(e.message) + "</div>";
      });
  }

  function anyBusy() {
    return ITEMS.some(function (it) {
      return it.busy || it.stage === "preparing" || it.stage === "analyzing";
    });
  }

  function schedule() {
    if (TIMER) { clearTimeout(TIMER); TIMER = null; }
    if (!anyBusy()) return;
    if (!$("#view-reverse") || !$("#view-reverse").classList.contains("active")) return;
    TIMER = setTimeout(function () { load(false); }, 3000);
  }

  function render() {
    var host = $("#rv-list");
    if (!host) return;
    // 轮询会整表重建：把已展开的精修表单内容按 video_id 暂存，重建后回填，
    // 否则用户刚点开的精修表单会被下一次 3s 轮询清空。
    var keep = {};
    Array.prototype.forEach.call(host.querySelectorAll(".rv-panel-host"), function (h) {
      var c = h.closest(".rv-card");
      var input=h.querySelector("textarea"); if(input) input.textContent=input.value;
      if (c && h.innerHTML.trim()) keep[c.getAttribute("data-vid")] = h.innerHTML;
    });
    if (!ITEMS.length) {
      host.innerHTML = '<div class="panel rv-empty">' +
        '<p class="muted">还没有任务。粘贴一条 TikTok 视频链接，点「① 生成素材」开始。</p>' +
        '<p class="muted">素材阶段只下原片、抽帧、导出首帧，不消耗 token；' +
        '点「② AI 反推」才会调用视觉模型读帧写提示词。</p></div>';
      return;
    }
    host.innerHTML = ITEMS.map(card).join("");
    Object.keys(keep).forEach(function (vid) {
      var h = host.querySelector('.rv-card[data-vid="' + vid + '"] .rv-panel-host');
      if (h) h.innerHTML = keep[vid];
    });
  }

  function card(it) {
    var meta = it.meta || {};
    var probe = it.probe || {};
    var stage = it.stage || "pending";
    var showBar = it.busy || stage === "preparing" || stage === "analyzing";
    var dur = probe.duration || meta.duration || 0;
    var res = (probe.width ? probe.width + "×" + probe.height : (meta.width ? meta.width + "×" + meta.height : "-"));
    var logs = (it.log || []).slice(-6);

    var btns = [];
    if (it.has_report && !it.busy) btns.push('<button class="btn ghost sm" data-act="refine" data-vid="' + esc(it.video_id) + '">本地精修</button>');
    if (it.frames) {
      btns.push('<button class="btn primary sm" data-act="ai" data-vid="' + esc(it.video_id) + '">' +
        (it.has_report ? "重跑 AI 反推" : "② AI 反推") + "</button>");
    }
    if (it.has_report) {
      btns.push('<button class="btn sm" data-act="report" data-vid="' + esc(it.video_id) + '" data-url="' +
        esc(it.report) + '">打开报告</button>');
    }
    if (it.frames) {
      btns.push('<button class="btn ghost sm" data-act="rebuild" data-vid="' + esc(it.video_id) + '">重建素材</button>');
    }
    btns.push('<button class="btn ghost sm" data-act="open" data-vid="' + esc(it.video_id) + '">打开目录</button>');
    btns.push('<button class="btn ghost sm danger" data-act="del" data-vid="' + esc(it.video_id) + '">删除</button>');

    return '' +
      '<section class="panel rv-card" data-vid="' + esc(it.video_id) + '">' +
        '<div class="rv-head">' +
          '<div class="rv-title">' +
            badge(stage) +
            '<b>@' + esc(meta.author || "（未知作者）") + "</b>" +
            '<span class="rv-id">' + esc(it.video_id) + "</span>" +
          "</div>" +
          '<div class="rv-acts">' + btns.join("") + "</div>" +
        "</div>" +
        '<div class="rv-kv">' +
          "<span>时长 <b>" + fmtDur(dur) + "</b></span>" +
          "<span>分辨率 <b>" + esc(res) + "</b></span>" +
          "<span>帧率 <b>" + esc((probe.fps || meta.fps || "-")) + " fps</b></span>" +
          "<span>抽帧 <b>" + (it.frames || 0) + "</b></span>" +
          "<span>送模型 <b>" + (it.ai_frames || 0) + "</b></span>" +
          "<span>镜头 <b>" + (it.shots || 0) + "</b></span>" +
          "<span>首帧 <b>" + (it.refs || 0) + "</b></span>" +
          "<span>播放/点赞 <b>" + (meta.view || 0) + "/" + (meta.like || 0) + "</b></span>" +
        "</div>" +
        (showBar
          ? '<div class="rv-prog"><div class="rv-prog-t"><span>' + esc(it.stage_label || "") +
            "</span><span>" + (it.progress || 0) + '%</span></div>' +
            '<div class="rv-bar"><i style="width:' + Math.max(2, it.progress || 0) + '%"></i></div></div>'
          : "") +
        (it.error ? '<div class="rv-err">' + esc(it.error) + "</div>" : "") +
        (meta.desc ? '<p class="rv-desc">' + esc(String(meta.desc).slice(0, 180)) + "</p>" : "") +
        (logs.length
          ? '<details class="rv-log"><summary>过程日志（' + (it.log || []).length + " 条）</summary><pre>" +
            esc(logs.map(function (l) { return "· " + l.msg; }).join("\n")) + "</pre></details>"
          : "") +
        '<div class="rv-panel-host"></div>' +
      "</section>";
  }

  /* ── 操作 ── */
  function create() {
    var inp = $("#rv-url");
    var url = inp ? inp.value.trim() : "";
    if (!url) { toast("先填 TikTok 视频链接"); return; }
    var btn = $("#rv-create");
    if (btn) { btn.disabled = true; btn.textContent = "解析中…"; }
    api("/api/reverse/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url }),
    })
      .then(function (r) {
        toast("已开始生成素材：" + r.video_id);
        if (inp) inp.value = "";
        return load(true);
      })
      .catch(function (e) { toast("创建失败：" + e.message, "error"); })
      .finally(function () {
        if (btn) { btn.disabled = false; btn.textContent = "① 生成素材"; }
      });
  }

  function post(url, body, okMsg) {
    return api(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    })
      .then(function (r) { if (okMsg) toast(okMsg); load(true); return r; })
      .catch(function (e) { toast("失败：" + e.message, "error"); });
  }

  function showRefine(vid, host) {
    host.innerHTML='<label>精修要求<textarea maxlength="4000" rows="4" style="width:100%" placeholder="例如：保留商品事实，缩短旁白，强化开头动作"></textarea></label><button class="btn primary sm" data-act="refine-submit" data-vid="'+esc(vid)+'">执行精修</button><p class="muted">调用本软件配置的视觉模型，保留分镜事实，修改提示词和旁白。</p>';
  }
  function bind() {
    var host = $("#rv-list");
    if (!host) return;
    host.addEventListener("click", function (ev) {
      var b = ev.target.closest("[data-act]");
      if (!b) return;
      var vid = b.getAttribute("data-vid");
      var act = b.getAttribute("data-act");
      var cardEl = b.closest(".rv-card");
      var ph = cardEl ? cardEl.querySelector(".rv-panel-host") : null;
      if (act === "copy") return;   // 由下方 [data-copy] 委托处理
      if (act === "ai") {
        var model = curModel();
        if (b.textContent.indexOf("重跑") >= 0 &&
            !confirm("重跑会覆盖现有报告与首帧，继续？")) return;
        b.disabled = true;
        post("/api/reverse/analyze/" + encodeURIComponent(vid), { model: model },
             "已开始 AI 反推（读帧写词约 1–4 分钟）");
        setTimeout(function () { b.disabled = false; }, 2000);
      } else if (act === "report") {
        var u = b.getAttribute("data-url");
        if (u) window.open(u, "_blank", "noopener");
      } else if (act === "rebuild") {
        if (!confirm("重建会清理抽帧、首帧与报告；本地素材保留原片，链接素材重新下载。继续？")) return;
        post("/api/reverse/rebuild/" + encodeURIComponent(vid), {}, "已开始重建素材");
      } else if (act === "open") {
        post("/api/reverse/open/" + encodeURIComponent(vid), {}, "");
      } else if (act === "refine") {
        if (ph) showRefine(vid, ph);
      } else if (act === "refine-submit") {
        var instruction = ph.querySelector("textarea").value.trim();
        if (!instruction) return toast("请填写精修要求", "error");
        post("/api/reverse/refine/" + encodeURIComponent(vid), { instruction: instruction, model: curModel() }, "已开始本地精修");
      } else if (act === "del") {
        if (!confirm("删除任务目录（含原片、帧图、报告）？此操作不可恢复。")) return;
        api("/api/reverse/" + encodeURIComponent(vid), { method: "DELETE" })
          .then(function () { toast("已删除"); load(true); })
          .catch(function (e) { toast("删除失败：" + e.message, "error"); });
      }
    });
  }

  function init() {
    var fileInput=document.getElementById('rv-local-file');
    if(fileInput) fileInput.addEventListener('change',async function(){
      var file=fileInput.files[0];if(!file)return;
      if(file.size>500*1024*1024){toast('本地视频上限500MB','error');fileInput.value='';return;}
      try{var r=await fetch('/api/reverse/import?filename='+encodeURIComponent(file.name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});var d=await r.json();if(!r.ok)throw new Error(d.detail||'导入失败');toast('本地视频已导入，正在生成素材');load(true);}catch(e){toast(e.message,'error');}finally{fileInput.value='';}
    });
    bind();          // 只在进入视图时拉数据（见 window.RV_load），首屏不额外发请求
  }

  window.RV_load = function () {
    var sel = $("#rv-model");
    if (!sel || !sel.options.length) loadModels();
    load(true);
  };
  window.reverseCreate = create;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
