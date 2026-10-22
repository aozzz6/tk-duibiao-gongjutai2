"""Remove retired paths from the delivered application, preserving current features."""
import ast
import json
import re
from pathlib import Path


class RemoveLegacy(ast.NodeTransformer):
    def visit_Constant(self, node):
        if isinstance(node.value, str):
            replacements = {
                'pending（Agent 写台账后执行）': '待本地台账同步与磁盘终验完成；最终状态见 run_summary.json',
                'Agent 待办: ①按 ledger_update_*.json 写台账(加行+刷动态列) ②写后磁盘终验(重开xlsx核对行数与抽样) ③新视频音频文案/翻译补录 ④将本报告与台账结果发送用户邮箱': '本地处理：采集后的台账同步与磁盘终验由软件执行，最终状态见运行回执；翻译与内容分析可在独立任务中执行。',
                '把 pending_trans.json 交给 Agent 翻译，译文回来后执行 post_update.py apply-trans <译文json> 回填台账': '在软件的独立任务中执行「中文翻译并回填」',
                '日更后 Agent 侧确定性步骤打包器': '日更后台账同步与校验工具',
                '台账同步 + 翻译交接 一键流': '台账同步与待译队列生成',
            }
            node.value = replacements.get(node.value, node.value)
        return node

    def visit_Expr(self,node):
        if isinstance(node.value,ast.Constant) and isinstance(node.value.value,str) and any(s in node.value.value.lower() for s in ('workbuddy','agent','未接适配')):
            return None
        return self.generic_visit(node)
    def visit_Dict(self, node):
        self.generic_visit(node)
        pairs = [(k,v) for k,v in zip(node.keys,node.values) if not isinstance(k,ast.Constant) or k.value not in {'workbuddy_home','workbuddy_exists'}]
        node.keys, node.values = [p[0] for p in pairs], [p[1] for p in pairs]
        return node


def clean_payload(payload):
    for file in payload.rglob('*.py'):
        tree = RemoveLegacy().visit(ast.parse(file.read_text(encoding='utf-8')))
        # Removing an obsolete docstring must not leave an otherwise empty function.
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and not node.body:
                node.body=[ast.Pass()]
        if file.name == 'config.py':
            tree.body[:] = [n for n in tree.body if not (isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id in {'WORKBUDDY_HOME','CURATED_SKILLS','HANDOFF_DIR'} for t in n.targets))]
            for node in tree.body:
                if isinstance(node,ast.For) and isinstance(node.iter,ast.Tuple):
                    node.iter.elts[:] = [n for n in node.iter.elts if not isinstance(n,ast.Name) or n.id != 'HANDOFF_DIR']
        ast.fix_missing_locations(tree)
        file.write_text(ast.unparse(tree)+'\n',encoding='utf-8')
        compile(file.read_text(encoding='utf-8'),str(file),'exec')
    static = payload/'webapp'/'static'
    html = (static/'index.html').read_text(encoding='utf-8')
    html = re.sub(r'<a\b[^>]*data-view="handoff".*?</a>', '', html, flags=re.S)
    html = re.sub(r'<section id="view-handoff".*?</section>', '', html, flags=re.S)
    html = html.replace('需要人工精修时点「交接包」生成 Agent 指令。','需要调整提示词时点「本地精修」，直接在软件里填写要求并执行。')
    html = html.replace('<label>AI 模型 <select id="rv-model"></select></label>', '<label>视觉模型 <select id="rv-model"></select></label><label class="btn ghost">导入本地视频<input id="rv-local-file" type="file" accept="video/mp4,video/webm,video/quicktime,.mkv" hidden /></label>')
    html = html.replace('class="nav-item hide" data-view="hot"', 'class="nav-item" data-view="hot"')
    html = html.replace('<!-- ── 转交 Agent ── -->', '')
    (static/'index.html').write_text(html,encoding='utf-8')
    js=(static/'app.js').read_text(encoding='utf-8')
    js=re.sub(r'/\* ─────────── 转交 Agent.*?(?=/\* ═+ 账号管理)', '', js, flags=re.S)
    js=re.sub(r'/\* 自动化 → 生成 Agent 转交指令.*?(?=/\* ─────────── 一键运行)', '', js, flags=re.S)
    js=re.sub(r'^.*(?:\$\("#hd-|renderSteps\("#hd-|if \((?:name|v) === "handoff"\)).*\n', '', js, flags=re.M)
    js=re.sub(r'const ho = \(f\.agent_steps.*?\n\s*: "";', 'const ho = "";', js, flags=re.S)
    js=js.replace('需转交 Agent','在独立任务中执行')
    js=js.replace('待译队列已刷新，请交给 Agent 翻译回填', '已开始中文翻译并回填，可在独立任务查看进度')
    js=js.replace('该流程含 Agent 步骤，在独立任务中执行 执行', '请在独立任务中执行').replace('>转交</span>', '>独立任务</span>')
    js=re.sub(r'\blet skillsCache\s*=\s*\[\];','',js)
    js=js.replace('  loadSkills();','')
    (static/'app.js').write_text(js,encoding='utf-8')
    reverse=(static/'reverse.js').read_text(encoding='utf-8')
    reverse=reverse.replace('（读不到 models.json）','（请在软件设置中配置视觉模型）')
    reverse=re.sub(r'^.*btns\.push\(.*data-act="handoff".*\n', '',reverse,flags=re.M)
    reverse=reverse.replace('  function handoff(vid, host) {','  function retiredRemoved(vid, host) {')
    reverse=re.sub(r'  function retiredRemoved\(vid, host\).*?(?=  function bind\(\))','',reverse,flags=re.S)
    reverse=re.sub(r'^    // 交接包复制.*?^    \}\);\n','',reverse,flags=re.M|re.S)
    reverse=reverse.replace('} else if (act === "handoff") {\n        if (ph) handoff(vid, ph);','} else if (act === "refine") {\n        if (ph) showRefine(vid, ph);\n      } else if (act === "refine-submit") {\n        var instruction = ph.querySelector("textarea").value.trim();\n        if (!instruction) return toast("请填写精修要求", "error");\n        post("/api/reverse/refine/" + encodeURIComponent(vid), { instruction: instruction, model: curModel() }, "已开始本地精修");')
    reverse=reverse.replace('    var keep = {};','    var keep = {};')
    reverse=reverse.replace('      if (c && h.innerHTML.trim()) keep[c.getAttribute("data-vid")] = h.innerHTML;', '      var input=h.querySelector("textarea"); if(input) input.textContent=input.value;\n      if (c && h.innerHTML.trim()) keep[c.getAttribute("data-vid")] = h.innerHTML;')
    # Add a refinement entry only when an existing report can be edited.
    marker='      btns.push('
    # The render function always creates a panel host; adding a hidden-on-empty button is safe.
    reverse=reverse.replace('    var btns = [];','    var btns = [];\n    if (it.has_report && !it.busy) btns.push(\'<button class="btn ghost sm" data-act="refine" data-vid="\' + esc(it.video_id) + \'">本地精修</button>\');')
    form='''  function showRefine(vid, host) {
    host.innerHTML='<label>精修要求<textarea maxlength="4000" rows="4" style="width:100%" placeholder="例如：保留商品事实，缩短旁白，强化开头动作"></textarea></label><button class="btn primary sm" data-act="refine-submit" data-vid="'+esc(vid)+'">执行精修</button><p class="muted">调用本软件配置的视觉模型，保留分镜事实，修改提示词和旁白。</p>';
  }
'''
    reverse=reverse.replace('  function bind() {',form+'  function bind() {')
    reverse=reverse.replace('    bind();          //', '''    var fileInput=document.getElementById('rv-local-file');
    if(fileInput) fileInput.addEventListener('change',async function(){
      var file=fileInput.files[0];if(!file)return;
      if(file.size>500*1024*1024){toast('本地视频上限500MB','error');fileInput.value='';return;}
      try{var r=await fetch('/api/reverse/import?filename='+encodeURIComponent(file.name),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});var d=await r.json();if(!r.ok)throw new Error(d.detail||'导入失败');toast('本地视频已导入，正在生成素材');load(true);}catch(e){toast(e.message,'error');}finally{fileInput.value='';}
    });
    bind();          //''')
    # Polling keeps local refinement inputs; no chat handoff or clipboard path remains.
    reverse=reverse.replace('交接包','精修表单').replace('Agent','本地模型')
    reverse=reverse.replace('重建素材会删掉抽帧、首帧与现有报告，重新下载解析。继续？','重建会清理抽帧、首帧与报告；本地素材保留原片，链接素材重新下载。继续？')
    (static/'reverse.js').write_text(reverse,encoding='utf-8')
    # Save an auditable cleanup record, outside the runtime manifest.
    (payload.parent/'清理清单.json').write_text(json.dumps({'excluded':['已下线 EchoTik 全模块','转交 Agent 页面、技能扫描、指令复制、反推交接接口','历史 bak/attic/rollback/截图/日志/测试数据','原电脑账号、Cookie、API凭据和业务产物','安装脚本与开发期补丁'], 'preserved':['最新提示词反推','最新版进程树与日更回执修复','账号、分组、采集、转写、台账、报告、定时','本地旧监控（使用本机配置）']},ensure_ascii=False,indent=2),encoding='utf-8')
