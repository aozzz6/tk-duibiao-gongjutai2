from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import config
import groups as account_groups
import pipeline_ops
import runner
import scheduler
import store
import benchmark
import tk_reverse
app = FastAPI(title='TikTok 对标工作台', version='1.0.0')

@app.exception_handler(account_groups.GroupStorageError)
async def group_storage_error(request, exc):
    return JSONResponse({'detail': str(exc)}, status_code=503)
STATIC_DIR = config.WEBAPP_DIR / 'static'
_overview_cache: dict = {'ts': 0.0, 'data': None}
_ledger_cache: dict = {'ts': 0.0, 'data': None}
_accounts_cache: dict = {'ts': 0.0, 'data': None}

@app.get('/api/health')
def health() -> dict:
    return {'ok': True, 'ts': time.time(), 'diagnostics': config.diagnostics()}

@app.get('/api/config')
def get_config() -> dict:
    cfg = config.load_accounts()
    all_cfg = config.load_accounts(include_inactive=True)
    return {'accounts': [a.get('account') for a in cfg.get('accounts', [])], 'accounts_detail': all_cfg.get('accounts', []), 'accounts_inactive': [a.get('account') for a in all_cfg.get('accounts', []) if not a.get('active', True)], 'steps': config.PIPELINE_STEPS, 'step_labels': config.STEP_LABELS, 'local_steps': config.LOCAL_STEPS, 'agent_steps': sorted(config.AGENT_STEPS), 'daily_steps': config.DAILY_STEPS, 'ledger': str(config.ledger_path()) if config.ledger_path() else None, 'diagnostics': config.diagnostics()}

def _count_files(d: Path, pattern: str) -> int:
    try:
        return sum((1 for _ in d.glob(pattern)))
    except Exception:
        return 0

def _first_existing(cands: list) -> Path | None:
    for c in cands:
        if not c:
            continue
        p = Path(c)
        if p.exists():
            return p
    return None

def _resolve_dirs(acc: dict) -> tuple[Path, Path]:
    """产物目录解析：优先 pipeline/output（实际产物位），再退回 accounts.json 声明。"""
    name = acc.get('account', '?')
    cfg_out = Path(acc['out_dir']) if acc.get('out_dir') else None
    cfg_audio = Path(acc['audio_dir']) if acc.get('audio_dir') else None
    default_audio = config.OUTPUT_DIR / f'{name}_audio'
    audio_dir = _first_existing([default_audio, cfg_audio, cfg_out]) or default_audio
    out_dir = _first_existing([config.OUTPUT_DIR, cfg_out, audio_dir]) or config.OUTPUT_DIR
    return (audio_dir, out_dir)

def _account_view(acc: dict, state: dict, ledger_rows: dict | None=None, ledger_ids: dict | None=None) -> dict:
    name = acc.get('account', '?')
    is_image = acc.get('media_type') == 'image'
    audio_dir, out_dir = _resolve_dirs(acc)
    man_path = _first_existing([audio_dir / f'manifest_{name}.json', out_dir / f'manifest_{name}.json'])
    man = config.read_json(man_path, None) or {} if man_path else {}
    videos = man.get('videos', {}) or {}
    scrape = config.read_json(out_dir / f'scrape_{name}.json', None) or config.read_json(config.OUTPUT_DIR / f'scrape_{name}.json', None) or {}
    items = scrape.get('items', []) or []
    errors = scrape.get('errors', []) or []
    aux = _aux_dir()
    prod = _first_existing([aux / name / f'{name}_products.json', out_dir / f'{name}_products.json', audio_dir / f'{name}_products.json'])
    products = config.read_json(prod, {}) if prod else {}
    if isinstance(products, dict):
        entries = products.get('products', products)
        prod_count = sum((isinstance(v, list) for v in entries.values())) if isinstance(entries, dict) else len(entries) if isinstance(entries, list) else 0
    elif isinstance(products, list):
        prod_count = len(products)
    else:
        prod_count = 0
    audio_n = _count_files(audio_dir, '*.mp3')
    trans_n = _count_files(audio_dir / 'transcripts', '*_transcript.txt')
    scraped_n = len(items)
    failed_n = len(errors)
    ledger_n = (ledger_rows or {}).get(name)
    ledger_set = (ledger_ids or {}).get(name)
    if is_image and ledger_set is None:
        ledger_set = set()
    trans_all = (ledger_ids or {}).get('__translation__') or {}
    trans_set = trans_all.get(name)
    scrape_ids = {i.get('id') for i in items if i.get('id')}
    covered = len(scrape_ids & ledger_set) if ledger_set is not None else None
    translation_covered = len(scrape_ids & trans_set) if trans_set is not None else None
    gaps: list[dict] = []
    if not is_image and audio_n and (scraped_n + failed_n != audio_n):
        d = scraped_n + failed_n - audio_n
        gaps.append({'level': 'warn', 'label': '采集', 'msg': f'抓取 {scraped_n} + 失败 {failed_n} 与音频 {audio_n} 相差 {d:+d} 条'})
    if not is_image and audio_n and (trans_n < audio_n):
        gaps.append({'level': 'warn', 'label': '转写', 'msg': f'转写缺 {audio_n - trans_n} 条'})
    if not is_image and scraped_n and (prod_count < scraped_n):
        ratio = prod_count / scraped_n
        acc_region = acc.get('region') or ''
        if acc_region == 'eu':
            pass
        elif prod is None:
            gaps.append({'level': 'info', 'label': '商品', 'msg': '商品未提取（可手动补跑 extract_products）'})
        elif ratio == 0:
            gaps.append({'level': 'info', 'label': '商品', 'msg': '无商品（账号未挂小黄车，纯内容/bio带货）'})
        else:
            gaps.append({'level': 'warn', 'label': '商品', 'msg': f'商品 {prod_count}/{scraped_n}（部分视频未挂车）'})
    if covered is not None and scraped_n and (covered != scraped_n):
        missing = scraped_n - covered
        gaps.append({'level': 'warn', 'label': '入账', 'msg': f'抓取 {scraped_n} 条中有 {missing} 条未入账（台账共 {ledger_n} 行）'})
    if not is_image and scraped_n and (translation_covered is not None) and (translation_covered < scraped_n):
        tmiss = scraped_n - translation_covered
        gaps.append({'level': 'info', 'label': '翻译', 'msg': f'翻译 {translation_covered}/{scraped_n}，缺 {tmiss} 条（待 15 分钟巡检回填 N 列）'})

    def _rate(have, want):
        return round(100 * have / want) if want else None
    rates = {'scrape': _rate(scraped_n, audio_n) if not is_image else None, 'transcribe': _rate(trans_n, audio_n) if not is_image else None, 'ledger': _rate(covered, scraped_n) if covered is not None else None, 'products': _rate(prod_count, scraped_n) if not is_image else None, 'translation': _rate(translation_covered, scraped_n) if translation_covered is not None and (not is_image) else None}
    vals = [v for v in rates.values() if v is not None]
    landing = round(sum(vals) / len(vals)) if vals else 0
    severity = 'ok'
    if any((g['level'] == 'error' for g in gaps)):
        severity = 'error'
    elif any((g['level'] in ('warn', 'error') for g in gaps)):
        severity = 'warn'
    steps = {}
    for s in config.PIPELINE_STEPS:
        rec = state.get(f'{name}|{s}')
        steps[s] = {'label': config.STEP_LABELS.get(s, s), 'ok': bool(rec.get('ok')) if rec else None, 'cost_s': rec.get('cost_s') if rec else None, 'detail': (rec.get('detail') or '')[:300] if rec else '', 'agent_side': s in config.AGENT_STEPS}
    return {'account': name, 'media_type': 'image' if is_image else 'video', 'lang': acc.get('lang', ''), 'out_dir': str(out_dir), 'audio_dir': str(audio_dir), 'active': bool(audio_n or trans_n or scraped_n or prod_count), 'has_data': bool(audio_n or trans_n or scraped_n or prod_count), 'enabled': bool(acc.get('active', True)) and (not acc.get('ledger_skip', False)), 'running': False, 'audio_count': audio_n, 'transcript_count': trans_n, 'scrape_items': scraped_n, 'scrape_errors': failed_n, 'products_count': prod_count, 'ledger_rows': ledger_n, 'ledger_covered': covered, 'translation_covered': translation_covered, 'manifest_exists': man_path is not None, 'manifest_videos': len(videos) if man_path else None, 'last_success_date': man.get('last_success_date') if man_path else None, 'rates': rates, 'landing': landing, 'severity': severity, 'gaps': gaps, 'steps': steps, 'progress': landing}

def _running_account_set() -> set[str]:
    """当前真正在跑的账号集合（多账号任务 account 是逗号串）。

    ★ 2026-10-20 验收 P2-2：运行态查询不能走 list(limit=N) —— 那是「先取最新 N 条
      再过滤 running」，一条早起的长任务会被后续成批 done 记录挤出窗口，导致卡片
      误判「未在运行」。
    """
    out: set[str] = set()
    for r in runner.MANAGER.running():
        out.update((x.strip() for x in (r.get('account') or '').split(',') if x.strip()))
    return out

@app.get('/api/overview')
def overview(refresh: bool=False) -> dict:
    if not refresh and _overview_cache['data'] and (time.time() - _overview_cache['ts'] < 5):
        return _overview_cache['data']
    cfg = config.load_accounts(include_inactive=True)
    state = config.read_json(config.STATE_FILE, {}) or {}
    gate = config.read_json(config.GATE_REPORT, {})
    ledger_rows = _ledger_row_counts(refresh)
    ledger_ids = _ledger_id_sets(refresh)
    accounts = [_account_view(a, state, ledger_rows, ledger_ids) for a in cfg.get('accounts', [])]
    running_set = _running_account_set()
    for _a in accounts:
        _a['running'] = _a['account'] in running_set
    sev_order = {'error': 0, 'warn': 1, 'ok': 2}
    accounts.sort(key=lambda a: (0 if a['active'] else 1, sev_order.get(a['severity'], 3), -a['audio_count']))
    runs = store.load_all(limit=20)
    data = {'ts': time.time(), 'accounts': [a for a in accounts if a['active']], 'idle_accounts': [a for a in accounts if not a['active']], 'gate': gate or {}, 'runs': runs[::-1], 'active_runs': runner.MANAGER.list(limit=10), 'schedule': scheduler.load(), 'pending_translation': _read_pending_translation(), 'diagnostics': config.diagnostics()}
    _overview_cache.update(ts=time.time(), data=data)
    return data

def _build_pipeline_cmd(p: dict) -> list[str]:
    account = p.get('account')
    if not account:
        raise HTTPException(400, '缺少 account')
    try:
        if not isinstance(account, str):
            raise ValueError('账号名必须是字符串')
        account = config.validate_account_name(account)
        from datetime import date
        start = date.fromisoformat(p.get('date_from', ''))
        end = date.fromisoformat(p.get('date_to', ''))
        if start > end:
            raise ValueError('开始日期不能晚于结束日期')
    except (ValueError, TypeError) as e:
        raise HTTPException(400, f'账号或日期无效: {e}')
    if p.get('backend') and p['backend'] not in ('auto', 'faster', 'openai'):
        raise HTTPException(400, '不支持的转写后端')
    ledger = config.ledger_path()
    xlsx = p.get('xlsx') or (str(ledger) if ledger else '')
    if not xlsx:
        raise HTTPException(400, '未找到台账 xlsx，请显式指定')
    cmd = [config.python_exe(), str(config.PIPELINE_DIR / 'pipeline.py'), '--account', account, '--from', p.get('date_from', ''), '--to', p.get('date_to', ''), '--xlsx', xlsx]
    steps = p.get('steps') or []
    if 'steps' in p and (not isinstance(p['steps'], list) or not p['steps']) or not isinstance(steps, list) or any((s not in config.PIPELINE_STEPS for s in steps)):
        raise HTTPException(400, '无效的流水线步骤')
    if steps and 's5b_products' not in steps and any((s in steps for s in ('s2_scrape', 's5_transcribe'))):
        steps = steps + ['s5b_products']
    if steps:
        cmd += ['--steps', *steps]
    if p.get('no_resume'):
        cmd.append('--no-resume')
    if p.get('allow_scrape_errors'):
        cmd.append('--allow-scrape-errors')
    lang = p.get('lang')
    if lang:
        if not isinstance(lang, str) or lang not in config.SUPPORTED_LANGS:
            raise HTTPException(400, f'不支持的转写语言: {lang}')
    else:
        account_cfg = config.load_accounts_raw().get('accounts', [])
        account_row = next((a for a in account_cfg if isinstance(a, dict) and a.get('account') == account), {})
        lang = config.guess_lang(account_row.get('region'), account_row.get('lang'))
    cmd += ['--lang', lang]
    if p.get('backend'):
        cmd += ['--backend', p['backend']]
    return cmd

def _multi_title(p: dict, accounts: list[str]) -> str:
    """多账户运行标题：N 个账号 + 日期区间。"""
    return f"多账户 · {len(accounts)} 个 · {p.get('date_from')}~{p.get('date_to')} · " + ('、'.join(accounts[:6]) + ('…' if len(accounts) > 6 else ''))

def _build_multi_pipeline_cmd(p: dict, accounts: list[str]) -> list[str]:
    """多账户批跑：对每个账号复用单账号校验+命令构造（含 s5b 自动补、lang 推断），
    包在一个 python 驱动里顺序串行执行 → 单任务/单日志流/单进度面板。

    顺序而非并发：转写占 GPU/模型，并发会互相抢资源且日志交织难读；
    串行让每个账号完整跑完再下一个，某账号失败即整链停（与单账号 fail-fast 同语义）。
    """
    per_cmds = [_build_pipeline_cmd({**p, 'account': acc}) for acc in accounts]
    driver = 'import subprocess, sys\ncmds = ' + repr(per_cmds) + '\naccounts = ' + repr(accounts) + "\nfor i, c in enumerate(cmds, 1):\n    print(f'[multi] ({i}/{len(cmds)}) 账号: ' + accounts[i-1], flush=True)\n    r = subprocess.run(c)\n    if r.returncode != 0:\n        print(f'[multi] 第 {i}/{len(cmds)} 个账号失败 rc={r.returncode}，停止后续', flush=True)\n        sys.exit(r.returncode)\nprint('[multi] 全部账号完成', flush=True)\n"
    return [config.python_exe(), '-c', driver]
AGENT_PENDING_NOTE = '台账同步与回填由本软件完成；翻译、分析可在「独立任务」执行，模型在本软件设置中配置。'
_LEDGER_SYNC_LOCK = threading.Lock()
_LEDGER_SYNC_ACTIVE = False
_LEDGER_SYNC_QUEUE: list[dict] = []

def _image_only_run(account: str) -> bool:
    names = account.split(',') if account and account != 'all' else []
    types = {a.get('account'): a.get('media_type') for a in config.load_accounts_raw().get('accounts', [])}
    return bool(names) and all((types.get(name) == 'image' for name in names))

def _start_ledger_sync(title: str, account: str, translate_backfill: bool) -> None:
    """在锁内登记并启动一次台账同步，防止完成钩子并发漏掉任务。"""
    global _LEDGER_SYNC_ACTIVE
    flow = config.SCRIPTS_DIR / 'post_update_flow.py'
    if not flow.exists():
        return
    tag = ' [trans-backfill]' if translate_backfill else ''
    _LEDGER_SYNC_ACTIVE = True
    cmd = [config.python_exe(), str(flow)]
    if account and account != 'all':
        cmd += ['--accounts', *account.split(',')]
    runner.MANAGER.start(kind='ledger', title=f'台账同步 · 自动跟随「{title}」{tag}', cmd=cmd, account=account)

def _auto_ledger_sync(run) -> None:
    global _LEDGER_SYNC_ACTIVE
    if run.kind not in {'pipeline', 'daily'} or run.status != 'done':
        return
    if '台账同步' in run.title:
        return
    translate_backfill = _TRANS_BACKFILL_RUNS.pop(run.id, False)
    with _LEDGER_SYNC_LOCK:
        ledger_running = any((r.get('kind') == 'ledger' for r in runner.MANAGER.running()))
        if _LEDGER_SYNC_ACTIVE or _trans_running or ledger_running:
            _LEDGER_SYNC_QUEUE.append({'title': run.title, 'account': run.account, 'translate_backfill': translate_backfill})
            print(f'[ledger] 已有台账同步运行，排队等待：{run.title}', flush=True)
            return
        if translate_backfill:
            print(f'[trans-backfill] 本 run 勾选翻译回填，台账同步完成后衔接翻译', flush=True)
        _start_ledger_sync(run.title, run.account, translate_backfill)
runner.MANAGER.add_finish_hook(_auto_ledger_sync)
PENDING_TRANS_FILE = Path(__file__).resolve().parent / 'data' / 'pending_translation.json'
_TRANS_BACKFILL_RUNS: dict[str, bool] = {}

def _refresh_pending_translation() -> int:
    """直扫台账真值：「音频(M) 有值而 翻译(N) 为空」的行 = 待译。

    非空则写入 data/pending_translation.json 供 15 分钟巡检 automation 消费；
    空则删除队列文件。返回待译条数。

    ★ 列定位按表头名（兼容「音频/备注」与「翻译/备注中文翻译」），不硬编码索引：
      14 列规范 M 音频=第 13 列(0-based 12)、N 翻译=第 14 列(0-based 13)。
    """
    import json as _json
    import openpyxl as _openpyxl
    items = []
    ledger = config.ledger_path()
    if ledger and Path(ledger).exists():
        wb = _openpyxl.load_workbook(ledger, read_only=True, data_only=True)
        for sn in wb.sheetnames:
            if not sn.startswith('对标记录-'):
                continue
            acc = sn.replace('对标记录-', '')
            ws = wb[sn]
            hdr = [str(c.value or '').strip() for c in next(ws.iter_rows(min_row=1, max_row=1), [])]
            mi = next((i for i, h in enumerate(hdr) if h in ('音频', '备注')), 12)
            ni = next((i for i, h in enumerate(hdr) if h in ('翻译', '备注中文翻译')), 13)
            li = next((i for i, h in enumerate(hdr) if '视频链接' in h), 5)
            for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
                row = list(row)
                pad = [None] * (max(mi, ni, li) + 1 - len(row))
                row = row + pad
                audio = str(row[mi] or '').strip()
                zh = str(row[ni] or '').strip()
                if audio and (not zh):
                    s = str(row[li] or '')
                    m = re.search('ID:\\s*\\(?\\s*(\\d{15,25})', s) or re.search('/video/(\\d{15,25})', s)
                    items.append({'account': acc, 'row': i, 'vid': m.group(1) if m else '', 'text': audio[:2000]})
        wb.close()
    PENDING_TRANS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if items:
        PENDING_TRANS_FILE.write_text(_json.dumps({'ts': __import__('time').time(), 'total': len(items), 'items': items}, ensure_ascii=False, indent=1), encoding='utf-8')
    elif PENDING_TRANS_FILE.exists():
        PENDING_TRANS_FILE.unlink()
    return len(items)

def _read_pending_translation() -> dict:
    try:
        import json as _json
        d = _json.loads(PENDING_TRANS_FILE.read_text(encoding='utf-8'))
        return {'total': d.get('total', 0), 'ts': d.get('ts')} if d.get('total') else {'total': 0}
    except Exception:
        return {'total': 0}
TRANS_ENGINE_NOTE = '台账同步与回填由本软件完成；翻译、分析可在「独立任务」执行，模型在本软件设置中配置。'

@app.get('/api/translation/pending')
def translation_pending() -> dict:
    """待译队列：{total, ts}，供前端按钮角标显示。"""
    return _read_pending_translation()

def _on_ledger_sync_done(run) -> None:
    """台账同步（write+导出待译）完成后 → 刷新待译队列 → 本地 LLM 自动翻译回填。

    ★ 2026-10-16 重构：翻译不再依赖周期巡检 automation（MINUTELY 巡检实际不触发，
    导致翻译长期堆积）。改为「台账同步完成即触发本地 LLM 翻译」（run 钩子），
    真自动、不等调度。翻译模块 pipeline/scripts/auto_translate.py（读 models.json）。
    周期巡检保留为兜底。
    """
    global _LEDGER_SYNC_ACTIVE, _trans_running
    if run.kind != 'ledger':
        return
    n = 0
    if run.status == 'done':
        try:
            n = _refresh_pending_translation()
            if n:
                print(f'[pending] 待译队列已更新：{n} 条', flush=True)
        except Exception as e:
            print(f'[pending] 待译队列刷新失败: {e}', flush=True)
    with _LEDGER_SYNC_LOCK:
        _LEDGER_SYNC_ACTIVE = False
        if _LEDGER_SYNC_QUEUE:
            queued = _LEDGER_SYNC_QUEUE.pop(0)
            _start_ledger_sync(queued['title'], queued['account'], queued['translate_backfill'])
            return
        if n and config.AUTO_TRANSLATE_ENABLED and (not _image_only_run(run.account)):
            _trans_running = True
            threading.Thread(target=_run_auto_translate, kwargs={'claimed': True}, daemon=True).start()
    try:
        if '[trans-backfill]' in (run.title or '') and (not _image_only_run(run.account)):
            print(f'[trans-backfill] 已衔接翻译回填：{n} 条待译', flush=True)
    except Exception as e:
        print(f'[auto-translate] 启动失败: {e}', flush=True)
_trans_running = False

def _run_auto_translate(claimed: bool=False) -> None:
    """执行 auto_translate.py：扫台账待译 → LLM 翻译 → 写 N 列。串行防并发。"""
    global _trans_running
    if not claimed:
        with _LEDGER_SYNC_LOCK:
            if _LEDGER_SYNC_ACTIVE or _trans_running:
                print('[auto-translate] 已有台账写入或翻译在跑，跳过本次触发', flush=True)
                return
            _trans_running = True
    try:
        script = config.SCRIPTS_DIR / 'auto_translate.py'
        if not script.exists():
            print(f'[auto-translate] 缺少脚本 {script}', flush=True)
            return
        print('[auto-translate] 触发本地 LLM 翻译 …', flush=True)
        cmd = [config.python_exe(), str(script)]
        if config.AUTO_TRANSLATE_MODEL:
            cmd += ['--model', config.AUTO_TRANSLATE_MODEL]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        tail = '\n'.join([l for l in (proc.stdout or '').strip().splitlines() if l.strip()][-2:])
        if proc.returncode == 0:
            print(f'[auto-translate] 完成: {tail}', flush=True)
            try:
                _refresh_pending_translation()
            except Exception:
                pass
        else:
            print(f"[auto-translate] 失败 rc={proc.returncode}: {tail or (proc.stderr or '').strip()[-200:]}", flush=True)
    except Exception as e:
        print(f'[auto-translate] 异常: {type(e).__name__}: {e}', flush=True)
    finally:
        with _LEDGER_SYNC_LOCK:
            _trans_running = False
            if _LEDGER_SYNC_QUEUE and (not _LEDGER_SYNC_ACTIVE):
                queued = _LEDGER_SYNC_QUEUE.pop(0)
                _start_ledger_sync(queued['title'], queued['account'], queued['translate_backfill'])
runner.MANAGER.add_finish_hook(_on_ledger_sync_done)

@app.post('/api/run')
def start_run(payload: dict) -> dict:
    kind = payload.get('kind', 'pipeline')
    if kind == 'pipeline':
        try:
            accounts = scheduler.validate_accounts(payload['accounts'] if 'accounts' in payload else [payload.get('account')])
            if not accounts:
                raise ValueError('至少选择一个账号')
        except ValueError as e:
            raise HTTPException(400, str(e))
        payload = {**payload, 'account': accounts[0]}
        if len(accounts) >= 2:
            cmd = _build_multi_pipeline_cmd(payload, accounts)
            run = runner.MANAGER.start(kind, _multi_title(payload, accounts), cmd, account=','.join(accounts))
        else:
            cmd = _build_pipeline_cmd(payload)
            title = f"流水线 · {payload.get('account')} · {payload.get('date_from')}~{payload.get('date_to')}"
            run = runner.MANAGER.start(kind, title, cmd, account=payload.get('account', ''))
        if payload.get('translate_backfill'):
            _TRANS_BACKFILL_RUNS[run.id] = True
        data = run.snapshot()
        data['agent_pending_note'] = AGENT_PENDING_NOTE
        return data
    if kind == 'daily':
        try:
            accounts = scheduler.validate_accounts(payload.get('accounts'))
            if 'accounts' in payload and (not accounts):
                raise ValueError('至少选择一个账号')
            return scheduler.trigger(accounts, reason='工作台')
        except ValueError as e:
            raise HTTPException(400, str(e))
    if kind == 'script':
        script = payload.get('script')
        if not script:
            raise HTTPException(400, '缺少 script')
        sp = config.SCRIPTS_DIR / script if not Path(script).is_absolute() else Path(script)
        if not sp.exists():
            raise HTTPException(404, f'脚本不存在: {sp}')
        args = payload.get('args') or []
        cmd = [config.python_exe(), str(sp), *[str(a) for a in args]]
        if payload.get('cookie') and config.COOKIE_FILE.exists():
            cmd += ['--cookie-file', str(config.COOKIE_FILE)]
        return runner.MANAGER.start('script', f'脚本 · {sp.name}', cmd).snapshot()
    raise HTTPException(400, f'未知 kind: {kind}')

@app.get('/api/runs')
def list_runs(limit: int=50) -> dict:
    active = {r['id']: r for r in runner.MANAGER.all()}
    hist = store.load_all(limit=limit)[::-1]
    merged = []
    for h in hist:
        row = dict(h)
        if h['id'] in active:
            row.update(active.pop(h['id']))
        merged.append(row)
    merged = list(active.values()) + merged
    return {'runs': merged[:limit]}

@app.get('/api/runs/{run_id}')
def get_run(run_id: str, since: int=0) -> dict:
    r = runner.MANAGER.get(run_id)
    if not r:
        raise HTTPException(404, '运行不存在')
    snap = r.snapshot()
    snap['lines'] = runner.MANAGER.lines(run_id, since)
    return snap

@app.get('/api/runs/{run_id}/stream')
def stream_run(run_id: str, since: int=0):
    r = runner.MANAGER.get(run_id)
    if not r:
        raise HTTPException(404, '运行不存在')

    def gen():
        q = runner.MANAGER.subscribe(run_id)
        try:
            for item in runner.MANAGER.lines(run_id, since):
                yield _sse({'line': item})
            yield _sse({'status': r.status, 'exit_code': r.exit_code, 'done': r.status != 'running'})
            if r.status != 'running':
                return
            while True:
                try:
                    item = q.get(timeout=20)
                except queue.Empty:
                    yield ': keepalive\n\n'
                    continue
                if item.get('done'):
                    yield _sse({'status': item['status'], 'exit_code': item['exit_code'], 'done': True})
                    break
                yield _sse({'line': item})
        finally:
            runner.MANAGER.unsubscribe(run_id, q)
    return StreamingResponse(gen(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

def _sse(obj: dict) -> str:
    return f'data: {json.dumps(obj, ensure_ascii=False)}\n\n'

@app.post('/api/runs/{run_id}/stop')
def stop_run(run_id: str) -> dict:
    return {'stopped': runner.MANAGER.stop(run_id)}

def _aux_dir() -> Path:
    lp = config.ledger_path()
    return lp.parent if lp else Path(os.environ.__getitem__('WB_LEDGER_DIR'))

@app.get('/api/reports')
def list_reports() -> dict:
    days = []
    if config.REPORTS_DIR.exists():
        for d in sorted(config.REPORTS_DIR.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            files = sorted((p.name for p in d.iterdir() if p.is_file()))
            days.append({'date': d.name, 'files': files, 'path': str(d)})
    aux = []
    ad = _aux_dir()
    if ad.exists():
        for p in sorted(ad.glob('*.html'), reverse=True):
            aux.append({'name': p.name, 'path': str(p), 'size_kb': round(p.stat().st_size / 1024, 1)})
    return {'reports_root': str(config.REPORTS_DIR), 'days': days, 'benchmark_reports': aux, 'aux_dir': str(ad)}

def _safe_path(raw: str, roots: list[Path]) -> Path:
    p = Path(raw).resolve()
    for root in roots:
        try:
            p.relative_to(root.resolve())
            return p
        except ValueError:
            continue
    raise HTTPException(403, '路径越权')

@app.get('/api/reports/view')
def view_report(path: str=Query(...)) -> FileResponse:
    roots = [config.REPORTS_DIR, config.OUTPUT_DIR, _aux_dir(), config.PIPELINE_DIR]
    p = _safe_path(path, roots)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, '文件不存在')
    return FileResponse(p)

@app.get('/api/reports/json')
def report_json(path: str=Query(...)) -> JSONResponse:
    roots = [config.REPORTS_DIR, config.OUTPUT_DIR, _aux_dir(), config.PIPELINE_DIR]
    p = _safe_path(path, roots)
    data = config.read_json(p, None)
    if data is None:
        raise HTTPException(404, '无法解析 JSON')
    return JSONResponse(data)

def _cell_str(v) -> str:
    if v is None:
        return ''
    if hasattr(v, 'strftime'):
        if getattr(v, 'hour', 0) == 0 and getattr(v, 'minute', 0) == 0 and (getattr(v, 'second', 0) == 0):
            return v.strftime('%Y-%m-%d')
        return v.strftime('%Y-%m-%d %H:%M')
    return str(v)

def _norm_header(s) -> str:
    """表头归一化：去首尾空格 + 全角空格 + 换行（G 列「视频出单量」实带前导空格）。"""
    return str(s or '').replace('\u3000', '').replace('\n', '').strip()

def _is_note(title: str) -> bool:
    return config.NOTE_SHEET_KEY in title

def _account_of(title: str) -> str:
    if title.startswith(config.SHEET_PREFIX):
        return title[len(config.SHEET_PREFIX):]
    return title.split('-', 1)[-1] if '-' in title else title

def _find_col(header: list, key: str) -> int:
    """按别名定位列索引：先 strip 后精确匹配，再退化为包含匹配。"""
    aliases = config.COL_ALIASES.get(key, [key])
    normed = [_norm_header(h) for h in header]
    for a in aliases:
        for i, h in enumerate(normed):
            if h == a:
                return i
    for a in aliases:
        for i, h in enumerate(normed):
            if a and a in h:
                return i
    return -1

def _hidden_cols(ws) -> set:
    """Excel 里被隐藏的列索引（0-based）。"""
    from openpyxl.utils import column_index_from_string
    out = set()
    try:
        for letter, dim in ws.column_dimensions.items():
            if getattr(dim, 'hidden', False):
                try:
                    out.add(column_index_from_string(letter) - 1)
                except Exception:
                    pass
    except Exception:
        pass
    return out

def _merged_fills(ws) -> dict:
    """合并区域 → 非左上角单元格的填充值（把合并区摊平，行判空才准）。"""
    fills = {}
    try:
        for rng in ws.merged_cells.ranges:
            min_col, min_row, max_col, max_row = rng.bounds
            tl = ws.cell(row=min_row, column=min_col).value
            for r in range(min_row, max_row + 1):
                for c in range(min_col, max_col + 1):
                    if (r, c) != (min_row, min_col):
                        fills[r, c] = tl
    except Exception:
        pass
    return fills

def _read_grid(ws, limit: int | None=None) -> dict:
    """读取 sheet：合并单元格摊平；返回全列网格（含隐藏列，由调用方裁剪）。"""
    fills = _merged_fills(ws)
    header: list = []
    rows: list[list] = []
    for r_i, row in enumerate(ws.iter_rows(), start=1):
        vals = []
        for cell in row:
            v = cell.value
            if v is None and (cell.row, cell.column) in fills:
                v = fills[cell.row, cell.column]
            vals.append(v)
        if r_i == 1:
            header = vals
            continue
        if all((v is None or str(v).strip() == '' for v in vals)):
            continue
        rows.append(vals)
        if limit and len(rows) >= limit:
            break
    return {'header': header, 'rows': rows, 'hidden': _hidden_cols(ws)}

def _column_checks(header: list, media_type: str='video') -> list[dict]:
    """列位自检：对照 pipeline 的真实读取方式，把隐患变成看得见的告警。"""
    from openpyxl.utils import get_column_letter
    checks: list[dict] = []
    link_idx = _find_col(header, '视频链接/ID')
    if link_idx == -1:
        checks.append({'level': 'error', 'msg': '未识别到「视频链接/ID」列；daily_update 会退回按第 6 列读取，新增判定可能失效'})
    elif link_idx != 5:
        checks.append({'level': 'info', 'msg': f'「视频链接/ID」位于第 {link_idx + 1} 列（{get_column_letter(link_idx + 1)}），现行规范为第 6 列（F）。daily_update 按表头定位，不受影响'})
    normed = [_norm_header(h) for h in header]
    while normed and (not normed[-1]):
        normed.pop()
    exp = [_norm_header(c) for c in (config.IMAGE_EXPECTED_COLS if media_type == 'image' else config.EXPECTED_COLS)]
    if len(normed) != len(exp):
        checks.append({'level': 'warn', 'msg': f'列数 {len(normed)} ≠ 规范 {len(exp)} 列'})
    diffs = [f'第{i + 1}列 期望「{e}」实际「{a}」' for i, (e, a) in enumerate(zip(exp, normed)) if e != a]
    if diffs:
        checks.append({'level': 'warn', 'msg': '列序与规范不符：' + '；'.join(diffs[:6]) + ('…' if len(diffs) > 6 else '')})
    spaced = [f'第{i + 1}列 {h!r}' for i, h in enumerate(header) if isinstance(h, str) and h != h.strip()]
    if spaced:
        checks.append({'level': 'info', 'msg': '表头含首尾空格（读取已自动 strip）：' + '、'.join(spaced[:4])})
    return checks

def _sheet_stats(ws) -> dict:
    """单 sheet 统计：表头、有效数据行数、记录日期范围（合并/隐藏列已处理）。"""
    grid = _read_grid(ws)
    header = grid['header']
    rows = grid['rows']
    date_idx = _find_col(header, '记录日期')
    dmin = dmax = None
    if date_idx >= 0:
        for r in rows:
            if date_idx >= len(r):
                continue
            s = _cell_str(r[date_idx]).strip()
            if not s:
                continue
            if dmin is None or s < dmin:
                dmin = s
            if dmax is None or s > dmax:
                dmax = s
    hidden = grid['hidden']
    media_type = 'image' if getattr(ws, 'title', '').startswith('图文') else 'video'
    account_rows = {}
    if media_type == 'image':
        for r in rows:
            name = str(r[1] or '').lstrip('@').strip() if len(r) > 1 else ''
            if name:
                account_rows[name] = account_rows.get(name, 0) + 1
    return {'media_type': media_type, 'account_rows': account_rows, 'header': [_norm_header(h) for h in header], 'rows': len(rows), 'cols': len(header), 'visible_cols': len([i for i in range(len(header)) if i not in hidden]), 'hidden_cols': sorted(hidden), 'date_min': dmin, 'date_max': dmax, 'checks': [] if _is_note(getattr(ws, 'title', '')) else _column_checks(header, media_type)}

def _ledger_file(file: str | None=None) -> Path:
    if file is not None:
        if not isinstance(file, str) or not file.strip():
            raise HTTPException(400, '台账文件名无效')
        lp = _safe_path(str(config.LEDGER_DIR / file), [config.LEDGER_DIR])
        if lp.suffix.lower() != '.xlsx':
            raise HTTPException(400, '仅支持 xlsx 台账')
    else:
        lp = config.ledger_path()
    if not lp or not lp.is_file():
        raise HTTPException(404, '台账文件不存在')
    return lp

def _open_ledger(file: str | None=None):
    """打开指定（或主）台账。返回 (workbook, path)。普通模式：可读隐藏列/合并区域。"""
    import openpyxl
    lp = _ledger_file(file)
    return (openpyxl.load_workbook(lp), lp)

def _ledger_overview_data(refresh: bool=False) -> dict:
    """台账目录总览数据：目录内全部 xlsx → 各 sheet 统计（60 秒缓存）。"""
    if not refresh and _ledger_cache['data'] and (time.time() - _ledger_cache['ts'] < 60):
        return _ledger_cache['data']
    try:
        import openpyxl
    except Exception as e:
        raise HTTPException(500, f'openpyxl 不可用: {e}')
    files = config.list_ledgers()
    out_files, sheets_all = ([], [])
    total_rows = 0
    for fp in files:
        info = {'name': fp.name, 'path': str(fp), 'size_kb': round(fp.stat().st_size / 1024, 1), 'mtime': fp.stat().st_mtime, 'sheets': [], 'error': None}
        try:
            wb = openpyxl.load_workbook(fp)
        except Exception as e:
            info['error'] = str(e)
            out_files.append(info)
            continue
        for ws in wb.worksheets:
            st = _sheet_stats(ws)
            title = ws.title
            entry = {'file': fp.name, 'file_path': str(fp), 'sheet': title, 'account': _account_of(title), 'is_note': _is_note(title), **st}
            if st['media_type'] == 'image' and len(st['account_rows']) == 1:
                entry['account'] = next(iter(st['account_rows']))
            info['sheets'].append(entry)
            sheets_all.append(entry)
            if not entry['is_note']:
                total_rows += st['rows']
        wb.close()
        out_files.append(info)
    data = {'dir': str(config.LEDGER_DIR), 'dir_exists': config.LEDGER_DIR.exists(), 'files': out_files, 'sheets': sheets_all, 'summary': {'files': len(files), 'sheets': sum((len(f['sheets']) for f in out_files)), 'rows': total_rows}, 'ts': time.time()}
    _ledger_cache.update(ts=time.time(), data=data)
    return data
_ledger_ids_cache: dict = {'ts': 0.0, 'data': None}

def _ledger_id_sets(refresh: bool=False) -> dict:
    """{account: {video_id, ...}} —— 台账「视频链接/ID」列中已入账的视频 ID 集合。

    入账判定必须用「覆盖」而非「行数相等」：
      · 台账累积历史视频（行数 > 抓取条数）→ 按行数比会**误报**缺口；
      · 反之历史行数恰好接近抓取条数（gylwwcxb: 18 行 vs 16 条）→ 按行数比会**漏报**
        真正未入账的新视频。
    """
    if not refresh and _ledger_ids_cache['data'] and (time.time() - _ledger_ids_cache['ts'] < 60):
        return _ledger_ids_cache['data']
    out: dict = {}
    trans: dict = {}
    try:
        import openpyxl
        paths = [config.ledger_path(), config.image_ledger_path()]
        for lp in dict.fromkeys((p for p in paths if p is not None)):
            wb = openpyxl.load_workbook(lp, read_only=True, data_only=True)
            try:
                for ws in wb.worksheets:
                    if _is_note(ws.title):
                        continue
                    image = ws.title.startswith('图文')
                    acc = _account_of(ws.title)
                    header = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), ())
                    idx = next((i for i, h in enumerate(header) if h and '视频链接' in str(h)), 5)
                    tid = next((i for i, h in enumerate(header) if h and str(h).strip() in ('翻译', '备注中文翻译')), 13)
                    if not image:
                        out.setdefault(acc, set())
                        trans.setdefault(acc, set())
                    for row in ws.iter_rows(min_row=2, max_col=max(idx, tid) + 1, values_only=True):
                        owner = str(row[1] or '').lstrip('@').strip() if image else acc
                        if not owner or idx >= len(row) or (not row[idx]):
                            continue
                        m = re.search('(\\d{15,25})', str(row[idx]))
                        if m:
                            out.setdefault(owner, set()).add(m.group(1))
                            if not image and tid < len(row) and str(row[tid] or '').strip():
                                trans.setdefault(owner, set()).add(m.group(1))
            finally:
                wb.close()
    except Exception:
        return out
    _ledger_ids_cache.update(ts=time.time(), data={**out, '__translation__': trans})
    return {**out, '__translation__': trans}

def _ledger_row_counts(refresh: bool=False) -> dict:
    """{account: 台账数据行数}，供流水线卡片做「入账」段对账。

    refresh 需透传：台账总览有 60s 缓存，若不透传，新建 sheet 后
    `/api/overview?refresh=true` 仍会读到旧缓存（台账列显示 —），误导用户。
    """
    out: dict = {}
    try:
        data = _ledger_overview_data(refresh)
    except Exception:
        return out
    for s in data.get('sheets', []):
        if s.get('is_note'):
            continue
        if s.get('media_type') == 'image':
            for name, rows in s.get('account_rows', {}).items():
                out[name] = out.get(name, 0) + rows
        else:
            out.setdefault(s['account'], s['rows'])
    return out

@app.get('/api/ledger/overview')
def ledger_overview(refresh: bool=False) -> dict:
    """台账目录总览：目录内全部 xlsx → 各 sheet 统计。"""
    return _ledger_overview_data(refresh)

@app.get('/api/ledger')
def ledger_info() -> dict:
    lp = config.ledger_path()
    if not lp or not lp.exists():
        return {'exists': False, 'dir': str(config.LEDGER_DIR), 'path': None, 'files': [], 'sheets': []}
    try:
        import openpyxl
        wb = openpyxl.load_workbook(lp, read_only=True, data_only=True)
        sheets = [{'name': ws.title, 'file': lp.name, 'rows': ws.max_row, 'cols': ws.max_column} for ws in wb.worksheets]
        wb.close()
        return {'exists': True, 'dir': str(config.LEDGER_DIR), 'path': str(lp), 'size_kb': round(lp.stat().st_size / 1024, 1), 'files': [p.name for p in config.list_ledgers()], 'sheets': sheets}
    except Exception as e:
        return {'exists': True, 'dir': str(config.LEDGER_DIR), 'path': str(lp), 'error': str(e), 'files': [], 'sheets': []}

@app.get('/api/ledger/resolve')
def ledger_resolve(account: str=Query(...)) -> dict:
    """按账号名在台账目录内定位 sheet。

    判定顺序（2026-10-20 修正 P0-2）：
      ① 精确 sheet 名 `对标记录-{account}` / `{account}`（视频主簿一账号一表）
      ② 共用/图文表里逐行匹配 `@account`（表里真的存在这个账号的行）
      ③ 才按 media_type 兜底 —— 但兜底表也必须"真的含该账号行"，否则返回 sheet=None，
         让前端走已写好的「未找到 + 候选列表」分支。
    旧实现先按 media_type 取共用图文表、且不校验行，导致 15 个图文账号全部落到
    372 行的别人表上（标题对、内容全无该账号）。
    """
    files = config.list_ledgers()
    if not files:
        raise HTTPException(404, '台账目录内无 xlsx')
    try:
        import openpyxl
    except Exception as e:
        raise HTTPException(500, str(e))
    all_names: list[str] = []
    exact_hit: tuple | None = None
    row_hit: tuple | None = None
    for fp in files:
        try:
            wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
            names = [n for n in wb.sheetnames if '说明' not in n]
            if exact_hit is None:
                ex = [n for n in names if n == account or n == config.SHEET_PREFIX + account]
                if ex:
                    exact_hit = (ex[0], fp, names)
            if row_hit is None:
                for n in [x for x in names if x.startswith('图文')]:
                    if any((str(r[0] or '').lstrip('@').strip() == account for r in wb[n].iter_rows(min_row=2, min_col=2, max_col=2, values_only=True))):
                        row_hit = (n, fp, names)
                        break
            wb.close()
        except Exception:
            continue
        all_names += names
    hit = exact_hit or row_hit
    if hit:
        sheet, fp, names = hit
        return {'account': account, 'sheet': sheet, 'file': fp.name, 'file_path': str(fp), 'candidates': names}
    return {'account': account, 'sheet': None, 'file': None, 'file_path': None, 'candidates': sorted(set(all_names))}

@app.get('/api/ledger/sheet')
def ledger_sheet(name: str=Query(...), file: str | None=None, limit: int=5000, offset: int=0) -> dict:
    try:
        wb, lp = _open_ledger(file)
    except HTTPException:
        raise
    try:
        if name not in wb.sheetnames:
            wb.close()
            raise HTTPException(404, f'sheet 不存在: {name}')
        ws = wb[name]
        grid = _read_grid(ws, limit=20000)
        hidden = grid['hidden']
        keep = [i for i in range(len(grid['header'])) if i not in hidden]
        header = [_cell_str(grid['header'][i]) for i in keep]
        all_rows = grid['rows']
        total = len(all_rows)
        page = all_rows[offset:offset + limit]
        rows = [[_cell_str(r[i]) if i < len(r) else '' for i in keep] for r in page]
        checks = [] if _is_note(name) else _column_checks(grid['header'], 'image' if name.startswith('图文') else 'video')
        wb.close()
        return {'sheet': name, 'file': lp.name, 'header': header, 'rows': rows, 'offset': offset, 'limit': limit, 'total': total, 'hidden_cols': sorted(hidden), 'all_cols': [_cell_str(h) for h in grid['header']], 'checks': checks}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.get('/api/benchmark')
def benchmark_view(account: str | None=None, accounts: str | None=None, refresh: bool=False) -> dict:
    """对标分析数据：账号→视频→爆款规律→可借鉴点（聚合 scrape+products+台账）。

    账号维度、视频排行、爆款规律、可借鉴点。数据源为本地真实产物，
    出单/GMV 如实标注「待接入」，不编造。

    accounts：逗号分隔的账号子集（组内对标入口用），与 account 二选一；
    传 accounts 时按子集聚合 KPI/patterns。
    """
    acc_list = list(dict.fromkeys((a.strip() for a in accounts.split(',') if a.strip()))) if accounts is not None else None
    return benchmark.build(account=account, refresh=refresh, accounts=acc_list)
_EXCEL_PS = "\n$ErrorActionPreference = 'Stop'\n$path  = $env:LEDGER_PATH\n$sheet = $env:LEDGER_SHEET\n$excel = $null\ntry { $excel = [Runtime.InteropServices.Marshal]::GetActiveObject('Excel.Application') } catch { }\nif ($null -eq $excel) { $excel = New-Object -ComObject Excel.Application }\n$excel.Visible = $true\ntry { $excel.DisplayAlerts = $false } catch { }\n$wb = $null\nforeach ($b in $excel.Workbooks) { if ($b.FullName -ieq $path) { $wb = $b; break } }\nif ($null -eq $wb) { $wb = $excel.Workbooks.Open($path) }\nif ($sheet -ne '') { try { $wb.Worksheets.Item($sheet).Activate() } catch { } }\ntry { $wb.Activate() } catch { }\nWrite-Output 'OK'\n"

def _open_in_excel(path: Path, sheet: str='') -> dict:
    """唤起本机 Excel 打开台账并尽量激活指定 sheet；失败则退化为直接打开文件。"""
    env = {**os.environ, 'LEDGER_PATH': str(path), 'LEDGER_SHEET': sheet or ''}
    err = ''
    try:
        proc = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', _EXCEL_PS], env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if proc.returncode == 0 and 'OK' in (proc.stdout or ''):
            return {'ok': True, 'method': 'excel-com', 'file': path.name, 'sheet': sheet}
        err = ((proc.stderr or '') + (proc.stdout or ''))[-400:]
    except Exception as e:
        err = str(e)
    try:
        os.startfile(str(path))
        return {'ok': True, 'method': 'startfile', 'file': path.name, 'sheet': '', 'note': '未能定位到指定 sheet，已直接打开整个文件。' + err[:160]}
    except Exception as e2:
        return {'ok': False, 'error': f'{err[:200]} | {e2}'}

@app.post('/api/ledger/open')
def ledger_open(payload: dict, request: Request) -> dict:
    """用本机 Excel 打开台账（尽量定位到指定 sheet）。

    这是「在本机拉起一个进程」的动作，与账号写接口同级别的权限边界：
    只允许回环来源，避免服务被绑到 0.0.0.0 后被远端借刀开文件。
    """
    _guard_local_write(request)
    file = payload.get('file')
    sheet = payload.get('sheet') or ''
    lp = _ledger_file(file)
    return _open_in_excel(lp, sheet)

@app.get('/api/schedule')
def get_schedule() -> dict:
    return scheduler.load()

@app.post('/api/schedule')
def set_schedule(payload: dict) -> dict:
    try:
        return scheduler.save(payload)
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post('/api/schedule/trigger')
def trigger_schedule(payload: dict | None=None) -> dict:
    try:
        return scheduler.trigger((payload or {}).get('accounts'), reason='手动触发')
    except ValueError as e:
        raise HTTPException(400, str(e))
_pipeline_status_cache: dict = {'ts': 0.0, 'data': None}

@app.get('/api/pipeline/status')
def pipeline_status(force: bool=Query(False)) -> dict:
    """待接管队列：失败账号 + 原因 + 重试次数。15s 缓存，force=1 跳过。"""
    now = time.time()
    if not force and _pipeline_status_cache['data'] and (now - _pipeline_status_cache['ts'] < 15):
        return _pipeline_status_cache['data']
    data = pipeline_ops.status(now)
    _pipeline_status_cache.update({'ts': now, 'data': data})
    return data

@app.post('/api/pipeline/retry')
def pipeline_retry(payload: dict | None=None) -> dict:
    """重跑失败账号（缺省 = 队列内全部）。

    ★ 2026-10-21 独立验收 P1-D：本端点必须走 `scripts/daily_batch.py`（唯一回执契约，
      子集补跑按账号并入当日整轮）。旧实现走 `daily_update.py`，一次重跑就会把未跑账号
      与 ledger_verify 失败一起覆写掉 —— 见 pipeline_ops.start_retry 的说明。
    """
    payload = payload or {}
    accounts = payload.get('accounts')
    if accounts is None:
        accounts = [x['account'] for x in pipeline_ops.status().get('takeover', [])]
    if not accounts:
        raise HTTPException(400, '没有待接管账号')
    try:
        run = pipeline_ops.start_retry(accounts, retry=int(payload.get('retry', 2)), timeout=int(payload.get('timeout', 5400)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'ok': True, 'run': run, 'accounts': accounts}
HOT_DIR = config.OUTPUT_DIR / 'hot_monitor'

@app.get('/api/groups')
def groups_list() -> dict:
    """分组清单（含成员与计数）。内置「默认分组」不可删改；成员不在 members 里的账号视为默认组。"""
    return account_groups.list_groups()

@app.post('/api/groups')
def groups_create(payload: dict) -> dict:
    """新建分组；可选 category_id 指定所属大分类（缺省 = 未分类）。"""
    payload = payload or {}
    try:
        g = account_groups.create_group(payload.get('name', ''), payload.get('category_id'))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {'ok': True, 'group': g}

@app.patch('/api/groups/{gid}')
def groups_update(gid: str, payload: dict) -> dict:
    """改分组：{"name": ...} 改名，{"category_id": ...} 换大分类，可同时传。"""
    payload = payload or {}
    if 'name' not in payload and 'category_id' not in payload:
        raise HTTPException(400, '需要 name 或 category_id')
    try:
        g = account_groups.update_group(gid, name=payload.get('name'), category_id=payload.get('category_id'))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {'ok': True, 'group': g}
groups_rename = groups_update

@app.patch('/api/groups/{gid}/category')
def groups_set_category(gid: str, payload: dict) -> dict:
    """把分组整体挪到另一个大分类（组内账号随组迁移）。"""
    cid = (payload or {}).get('category_id')
    if not cid:
        raise HTTPException(400, '缺少 category_id')
    try:
        g = account_groups.set_group_category(gid, cid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {'ok': True, 'group': g}

@app.get('/api/categories')
def categories_list() -> dict:
    """大分类清单（含分组数 / 账号数）。层级：大分类 ← 分组 ← 账号。"""
    d = account_groups.list_groups()
    return {'ok': True, 'categories': d['categories']}

@app.post('/api/categories')
def categories_create(payload: dict) -> dict:
    payload = payload or {}
    try:
        c = account_groups.create_category(payload.get('name', ''), payload.get('desc', ''))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {'ok': True, 'category': c}

@app.patch('/api/categories/{cid}')
def categories_update(cid: str, payload: dict) -> dict:
    payload = payload or {}
    if 'name' not in payload and 'desc' not in payload:
        raise HTTPException(400, '需要 name 或 desc')
    try:
        c = account_groups.update_category(cid, name=payload.get('name'), desc=payload.get('desc'))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {'ok': True, 'category': c}

@app.delete('/api/categories/{cid}')
def categories_delete(cid: str) -> dict:
    """删除大分类：内置不可删；其下还有分组时拒绝（需先把分组挪走）。"""
    try:
        r = account_groups.delete_category(cid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {'ok': True, **r}

@app.delete('/api/groups/{gid}')
def groups_delete(gid: str) -> dict:
    """删除分组：成员自动回「默认分组」，账号本身不会被删除。"""
    try:
        r = account_groups.delete_group(gid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {'ok': True, **r}

@app.post('/api/groups/{gid}/members')
def groups_add_member(gid: str, payload: dict) -> dict:
    try:
        r = account_groups.add_member(gid, (payload or {}).get('account', ''))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {'ok': True, **r}

@app.post('/api/groups/{gid}/transfer')
def groups_transfer(gid: str, payload: dict) -> dict:
    """批量迁移账号到目标分组（2026-10-19）。

    入参：{"accounts": ["a", "b"]}；逐条返回 moved/same/not_found，不因单条失败中断。
    典型用法：台账卡片勾选多个账号 → 选目标分组 → 一次迁移。
    """
    accounts = (payload or {}).get('accounts')
    if not isinstance(accounts, list) or not accounts:
        raise HTTPException(400, 'accounts 必须是非空数组')
    try:
        r = account_groups.transfer_members(gid, accounts)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {'ok': True, **r}

@app.delete('/api/groups/{gid}/members/{account}')
def groups_remove_member(gid: str, account: str) -> dict:
    """移出分组 = 回到默认分组（账号保留）。"""
    try:
        r = account_groups.remove_member(gid, account)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {'ok': True, **r}

def _group_metrics_for(gid: str) -> dict:
    """组内账号指标聚合 + 健康度（方案 B）。数据源：本地 scrape_{acc}.json，无数据如实标注。

    指标（每账号）：视频数 / 总播放 / 均播放 / 爆款数(播放≥1万) / 7 天更新数 / 挂车率 / 健康标签。
    组级：账号数、空组、低价值账号数（爆款=0 且 ≥7 天没更新）、未更新账号（>7 天无新视频）。
    """
    import datetime as _dt
    g = next((x for x in account_groups.list_groups().get('groups', []) if x.get('id') == gid), None)
    if g is None:
        raise HTTPException(404, '分组不存在')
    members = [m['account'] for m in g.get('members') or []]
    now = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8))).replace(tzinfo=None)
    cutoff = now - _dt.timedelta(days=7)
    accounts = []
    for acc in members:
        p = config.OUTPUT_DIR / f'scrape_{acc}.json'
        data = config.read_json(p, None) or {}
        raw_items = data.get('items', []) if isinstance(data, dict) else []
        items, seen = ([], set())
        for it in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(it, dict):
                continue
            vid = str(it.get('id') or it.get('video_id') or '')
            if vid and vid in seen:
                continue
            if vid:
                seen.add(vid)
            items.append(it)
        stats_list = [it.get('stats') if isinstance(it.get('stats'), dict) else {} for it in items]
        plays = [benchmark._num(s.get('playCount')) for s in stats_list]
        total_plays = sum(plays)
        n = len(items)
        viral = sum((1 for x in plays if x >= 10000))
        pubs = []
        for it in items:
            try:
                d = _dt.datetime.strptime(it.get('publish_time_gmt8'), '%Y-%m-%d %H:%M:%S')
                if d <= now:
                    pubs.append(d)
            except (ValueError, TypeError):
                continue
        recent = sum((cutoff <= d <= now for d in pubs))
        last = max(pubs) if pubs else None
        last_pub = last.strftime('%Y-%m-%d %H:%M:%S') if last else ''
        stale_days = (now - last).days if last else None
        products = benchmark._load_products_map(acc)
        prod_n = sum((bool(products.get(str(it.get('id') or it.get('video_id') or ''))) for it in items))
        coverage = round(prod_n / n * 100, 1) if n else 0
        tags = []
        if n == 0:
            tags.append('无数据')
        if viral == 0:
            tags.append('无爆款')
        if stale_days is not None and stale_days > 7:
            tags.append(f'{stale_days}天未更新')
        elif stale_days == 0:
            tags.append('今日更新')
        accounts.append({'account': acc, 'videos': n, 'total_plays': total_plays, 'avg_plays': round(total_plays / n) if n else 0, 'viral': viral, 'recent7d': recent, 'last_pub': last_pub, 'stale_days': stale_days, 'product_coverage': coverage, 'tags': tags})
    low_value = [a['account'] for a in accounts if a['videos'] > 0 and a['viral'] == 0 and ((a['stale_days'] or 0) > 7)]
    stale = [a['account'] for a in accounts if (a['stale_days'] or 0) > 7]
    return {'group': {'id': g['id'], 'name': g.get('name'), 'count': len(members)}, 'accounts': accounts, 'low_value': low_value, 'stale': stale, 'empty': len(members) == 0}

@app.get('/api/groups/{gid}/metrics')
def groups_metrics(gid: str) -> dict:
    """组内账号指标对比 + 健康度提醒（读本地 scrape/products，不写任何数据）。"""
    return _group_metrics_for(gid)

@app.get('/api/hot/latest')
def hot_latest() -> dict:
    out: dict = {'dir': str(HOT_DIR)}
    cand = HOT_DIR / 'candidates.json'
    out['candidates'] = config.read_json(cand, None)
    snaps_root = HOT_DIR / 'snapshots'
    snaps = []
    if snaps_root.exists():
        for d in sorted(snaps_root.iterdir(), reverse=True)[:10]:
            if d.is_dir():
                snaps.append({'date': d.name, 'sources': [p.stem for p in d.glob('*.json')]})
    out['snapshots'] = snaps
    metrics = HOT_DIR / 'daily_metrics.jsonl'
    if metrics.exists():
        lines = metrics.read_text(encoding='utf-8', errors='replace').splitlines()
        out['metrics_rows'] = len([l for l in lines if l.strip()])
        tail = []
        for l in lines[-200:]:
            try:
                tail.append(json.loads(l))
            except json.JSONDecodeError:
                pass
        out['metrics_tail'] = tail
    out['cookie_exists'] = config.COOKIE_FILE.exists()
    return out

@app.post('/api/hot/{action}')
def hot_action(action: str, payload: dict | None=None) -> dict:
    if action not in {'probe', 'snapshot', 'diff'}:
        raise HTTPException(400, '动作仅支持 probe/snapshot/diff')
    script = config.SCRIPTS_DIR / 'hot_product_monitor.py'
    if not script.exists():
        raise HTTPException(404, '缺少 hot_product_monitor.py')
    cmd = [config.python_exe(), str(script), action]
    if action in {'probe', 'snapshot'}:
        if not config.COOKIE_FILE.exists():
            raise HTTPException(400, '缺少 cc_cookie.txt（爆品监控需登录 Cookie）')
        cmd += ['--cookie-file', str(config.COOKIE_FILE)]
    if action == 'diff':
        if (payload or {}).get('windows'):
            cmd += ['--windows', str(payload['windows'])]
    return runner.MANAGER.start('hot', f'爆品监控 · {action}', cmd).snapshot()

def _automation_run_cmd(flow: dict) -> list[str] | None:
    kind = flow.get('kind')
    if kind == 'daily':
        try:
            return scheduler.build_daily_cmd(flow.get('accounts'))
        except ValueError as e:
            raise HTTPException(400, str(e))
    if kind == 'script':
        script = flow.get('script')
        if not script:
            return None
        sp = Path(script)
        if not sp.is_absolute():
            sp = (config.SCRIPTS_DIR / script).resolve()
        if not sp.exists():
            raise HTTPException(404, f'脚本不存在: {sp}')
        args = [str(a) for a in flow.get('args') or []]
        if flow.get('cookie') and config.COOKIE_FILE.exists():
            args += ['--cookie-file', str(config.COOKIE_FILE)]
        return [config.python_exe(), str(sp), *args]
    return None

def _bust_accounts_cache() -> None:
    """写操作后立即失效缓存，避免页面读到旧状态。"""
    _accounts_cache.update(ts=0.0, data=None)
    _overview_cache.update(ts=0.0, data=None)
ACCOUNT_FIELD_SCHEMA = {'account': {'type': 'string', 'readonly': True, 'required': True, 'pattern': '^[a-z0-9_.]+$', 'max_len_with_prefix': 31, 'desc': '主键，同时是 Excel sheet 名后缀「对标记录-{account}」；创建后不可修改'}, 'region': {'type': 'string', 'enum': ['us', 'uk', 'eu', 'de', 'fr', 'es', 'it', 'cn', 'cn-hk', 'cn-tw', 'jp', 'kr', ''], 'default': '', 'desc': '账号面向地区；决定转写语言（做哪个地区就用该地区语言）。us/uk→en，de→de，fr→fr，es→es，it→it，cn/cn-hk/cn-tw→zh。未填 = 默认 en；新账号注册务必按实际面向地区补填。'}, 'lang': {'type': 'string', 'enum': ['en', 'de', 'es', 'zh', 'fr', 'it', 'jp', 'kr'], 'default': 'en', 'desc': '转写语言；未显式指定时按 region 自动推断（做哪个地区用该地区语言）。中文口播账号须 zh（auto/en 会产生伪转写）。'}, 'nickname': {'type': 'string', 'default': '', 'desc': '展示名，可空'}, 'tags': {'type': 'array<string>', 'default': [], 'desc': '分类标签；入参也接受逗号分隔的字符串，服务端会拆分'}, 'note': {'type': 'string', 'default': '', 'desc': '备注，可空'}, 'out_dir': {'type': 'string', 'default': 'pipeline/output', 'desc': '抓取产物目录；缺省随主流水线约定'}, 'audio_dir': {'type': 'string', 'default': 'pipeline/output/{account}_audio', 'desc': '音频目录；缺省 output/{account}_audio'}, 'active': {'type': 'boolean', 'default': True, 'desc': '是否参与流水线；false 时被 load_accounts / daily_update 过滤'}}
_LOCAL_WRITE_HOSTS = {'127.0.0.1', '::1', 'localhost', 'testclient'}
_ACCOUNT_WRITE_LOCK = threading.Lock()

def _guard_local_write(request: Request) -> None:
    host = (getattr(getattr(request, 'client', None), 'host', '') or '').lower()
    if host not in _LOCAL_WRITE_HOSTS:
        raise HTTPException(403, '账号写操作仅限本机访问（127.0.0.1）')

@app.post('/api/accounts')
def create_account(payload: dict, request: Request) -> dict:
    """新增账号。account 必填，其余可选（缺省按主流水线约定生成）。

    media_type 可选（video / image）：image 会额外打上「图文账号」标签，
    流水线据此跳过音频与转写，改走图文枚举。
    """
    _guard_local_write(request)
    payload = payload or {}
    name = payload.get('account')
    mt = str(payload.get('media_type') or 'video').strip().lower()
    if mt not in ('video', 'image'):
        raise HTTPException(400, 'media_type 只能是 video / image')
    with _ACCOUNT_WRITE_LOCK:
        try:
            row = config.add_account(name, payload)
            if mt == 'image':
                config.set_media_type([row.get('account')], 'image')
                _ledger_cache.update(ts=0.0, data=None)
                row = next((a for a in config.load_accounts_raw().get('accounts', []) if isinstance(a, dict) and a.get('account') == row.get('account')), row)
                row = dict(row)
                row['media_type'] = 'image'
        except ValueError as e:
            raise HTTPException(400, str(e))
        except OSError as e:
            raise HTTPException(500, f'写 accounts.json 失败: {e}')
    _bust_accounts_cache()
    return {'ok': True, 'account': row}

@app.get('/api/accounts')
def list_accounts(q: str='', status: str='', media_type: str='', group: str='') -> dict:
    """账号清单（只读）：配置字段 + 产物统计 + 健康等级 + 所属分组。

    统计直接复用 /api/overview 的 5s 缓存，不额外扫盘；筛选在后端做是为了
    让 counts 始终返回「全量」口径（筛选结果只影响 accounts 列表）。
    """
    cfg = config.load_accounts(include_inactive=True)
    rows = [a for a in cfg.get('accounts', []) if isinstance(a, dict)]
    ov = overview(refresh=False)
    stats = {}
    for a in (ov.get('accounts') or []) + (ov.get('idle_accounts') or []):
        if a.get('account'):
            stats[a['account']] = a
    gmap = {}
    try:
        for g in account_groups.list_groups().get('groups', []):
            for m in g.get('members', []):
                gmap[m.get('account')] = {'group_id': g.get('id'), 'group_name': g.get('name')}
    except Exception:
        gmap = {}
    items = []
    for a in rows:
        name = a.get('account')
        st = stats.get(name, {})
        grp = gmap.get(name, {})
        items.append({'account': name, 'nickname': a.get('nickname') or '', 'region': a.get('region') or '', 'lang': a.get('lang') or '', 'tags': a.get('tags') or [], 'note': a.get('note') or '', 'active': bool(a.get('active', True)), 'media_type': 'image' if a.get('media_type') == 'image' else 'video', 'out_dir': a.get('out_dir') or '', 'audio_dir': a.get('audio_dir') or '', 'audio_count': st.get('audio_count', 0), 'transcript_count': st.get('transcript_count', 0), 'scrape_items': st.get('scrape_items', 0), 'scrape_errors': st.get('scrape_errors', 0), 'products_count': st.get('products_count', 0), 'ledger_rows': st.get('ledger_rows', 0), 'last_success_date': st.get('last_success_date'), 'severity': st.get('severity', 'ok'), 'landing': st.get('landing', 0), 'has_artifacts': bool(st.get('audio_count') or st.get('scrape_items') or st.get('ledger_rows')), 'group_id': grp.get('group_id') or '', 'group_name': grp.get('group_name') or '未分组'})
    counts = {'all': len(items), 'active': sum((1 for x in items if x['active'])), 'inactive': sum((1 for x in items if not x['active'])), 'video': sum((1 for x in items if x['media_type'] == 'video')), 'image': sum((1 for x in items if x['media_type'] == 'image')), 'artifacts': sum((1 for x in items if x['has_artifacts']))}
    kw = (q or '').strip().lower()
    st_filter = (status or '').strip().lower()
    mt_filter = (media_type or '').strip().lower()
    g_filter = (group or '').strip()
    out = []
    for x in items:
        if kw:
            hay = ' '.join([x['account'] or '', x['nickname'] or '', ' '.join(x['tags'] or []), x['group_name'] or '']).lower()
            if kw not in hay:
                continue
        if st_filter in ('active', 'inactive') and x['active'] != (st_filter == 'active'):
            continue
        if mt_filter in ('video', 'image') and x['media_type'] != mt_filter:
            continue
        if g_filter and x['group_id'] != g_filter:
            continue
        out.append(x)
    sev = {'error': 0, 'warn': 1, 'ok': 2}
    out.sort(key=lambda x: (0 if x['active'] else 1, sev.get(x['severity'], 3), (x['account'] or '').lower()))
    return {'total': counts['all'], 'shown': len(out), 'counts': counts, 'accounts': out}

@app.post('/api/accounts/media-type')
def set_accounts_media_type(payload: dict, request: Request) -> dict:
    """批量设置账号类型：{"accounts": [...], "media_type": "image"|"video"}（2026-10-19）。

    用于把选中的视频账号导入为图文账号（或反向）。只改 accounts.json 的类型与标签，
    不迁移、不删除任何台账 sheet 与产物。
    """
    payload = payload or {}
    _guard_local_write(request)
    accounts = payload.get('accounts')
    if not isinstance(accounts, list) or not accounts:
        raise HTTPException(400, 'accounts 必须是非空数组')
    with _ACCOUNT_WRITE_LOCK:
        try:
            r = config.set_media_type(accounts, payload.get('media_type') or 'image')
        except ValueError as e:
            raise HTTPException(400, str(e))
    _bust_accounts_cache()
    _ledger_cache.update(ts=0.0, data=None)
    return {'ok': True, **r}

@app.get('/api/accounts/{name}')
def account_detail(name: str) -> dict:
    """账号详情：视图统计 + 配置 + 可编辑字段 schema。"""
    cfg = config.load_accounts_raw()
    row = next((a for a in cfg.get('accounts', []) if a.get('account') == name), None)
    if row is None:
        raise HTTPException(404, f'账号不存在：{name}')
    state = config.read_json(config.STATE_FILE, {}) or {}
    try:
        view = _account_view(row, state, _ledger_row_counts(False), _ledger_id_sets(False))
    except Exception:
        view = {'account': name}
    out = dict(view)
    out.update(row)
    out['editable_fields'] = list(config.EDITABLE_FIELDS)
    out['field_schema'] = ACCOUNT_FIELD_SCHEMA
    return out

@app.patch('/api/accounts/{name}')
def account_update(name: str, payload: dict, request: Request) -> dict:
    """改账号可编辑字段（主键 account 只读）；media_type 可一并改。

    media_type 走 config.set_media_type（会补/移「图文账号」标签并失效台账缓存），
    与批量接口保持同一套语义。
    """
    _guard_local_write(request)
    payload = dict(payload or {})
    mt = payload.pop('media_type', None)
    mt = str(mt).strip().lower() if mt is not None else None
    if mt is not None and mt not in ('video', 'image'):
        raise HTTPException(400, 'media_type 只能是 video / image')
    with _ACCOUNT_WRITE_LOCK:
        try:
            row = config.update_account(name, payload)
            if mt is not None:
                config.set_media_type([name], mt)
                _ledger_cache.update(ts=0.0, data=None)
                row = next((a for a in config.load_accounts_raw().get('accounts', []) if isinstance(a, dict) and a.get('account') == name), row)
        except ValueError as e:
            raise HTTPException(404 if '不存在' in str(e) else 400, str(e))
    _bust_accounts_cache()
    row = dict(row)
    row['media_type'] = mt or ('image' if row.get('media_type') == 'image' else 'video')
    return {'ok': True, 'account': row}

@app.delete('/api/accounts/{name}')
def account_delete(name: str, request: Request) -> dict:
    """删除账号（仅限无任何产物的空壳；有产物须改用「停用」）。"""
    _guard_local_write(request)
    with _ACCOUNT_WRITE_LOCK:
        try:
            config.remove_account(name)
        except ValueError as e:
            raise HTTPException(404 if '不存在' in str(e) else 400, str(e))
    _bust_accounts_cache()
    group_cleaned = False
    try:
        group_cleaned = account_groups.forget_account(name)
    except Exception as e:
        print(f'[groups] 清理 {name} 的分组记录失败：{e}', flush=True)
    return {'ok': True, 'deleted': name, 'group_cleaned': group_cleaned}

@app.post('/api/reverse/create')
def reverse_create(payload: dict) -> dict:
    try:
        return tk_reverse.create((payload or {}).get('url') or '')
    except tk_reverse.ReverseError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get('/api/reverse/list')
def reverse_list() -> dict:
    return {'items': tk_reverse.list_all()}

@app.get('/api/reverse/status/{vid}')
def reverse_status(vid: str) -> dict:
    try:
        return tk_reverse.status(vid)
    except tk_reverse.ReverseError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get('/api/reverse/models')
def reverse_models() -> dict:
    models = tk_reverse.load_vision_models()
    items = [{'id': m.get('id'), 'name': m.get('name') or m.get('id')} for m in models]
    try:
        default = tk_reverse.pick_model('').get('id') or ''
    except Exception:
        default = items[0]['id'] if items else ''
    return {'items': items, 'default': default}

@app.post('/api/reverse/analyze/{vid}')
def reverse_analyze(vid: str, payload: dict | None=None) -> dict:
    try:
        return tk_reverse.analyze(vid, (payload or {}).get('model') or '')
    except tk_reverse.ReverseError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post('/api/reverse/rebuild/{vid}')
def reverse_rebuild(vid: str) -> dict:
    try:
        return tk_reverse.rebuild(vid)
    except tk_reverse.ReverseError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete('/api/reverse/{vid}')
def reverse_delete(vid: str) -> dict:
    try:
        return tk_reverse.remove(vid)
    except tk_reverse.ReverseError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post('/api/reverse/open/{vid}')
def reverse_open(vid: str) -> dict:
    try:
        d = tk_reverse.task_dir(vid)
    except tk_reverse.ReverseError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not d.exists():
        raise HTTPException(status_code=404, detail='任务目录不存在')
    try:
        os.startfile(str(d))
        return {'ok': True, 'dir': str(d)}
    except Exception as e:
        return {'ok': False, 'dir': str(d), 'error': str(e)}
app.mount('/tk_reverse', StaticFiles(directory=str(tk_reverse.REVERSE_DIR)), name='tk_reverse')

@app.middleware('http')
async def _no_store_frontend(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path == '/' or path.startswith('/static'):
        resp.headers['Cache-Control'] = 'no-store, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
    return resp

@app.get('/')
def index() -> FileResponse:
    idx = STATIC_DIR / 'index.html'
    if not idx.exists():
        return JSONResponse({'error': '前端未构建'}, status_code=500)
    return FileResponse(idx)
app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')
if __name__ == '__main__':
    import uvicorn
    _swept = store.sweep_stale_running()
    if _swept:
        print(f'[startup] 清扫僵尸运行记录 {_swept} 条 -> interrupted', flush=True)
    scheduler.start_background()
    uvicorn.run(app, host='127.0.0.1', port=8787, log_level='info')
