from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
PIPE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPE_DIR))
from gate import run_gates
CFG = {'python': sys.executable, 'audio_download': str(PIPE_DIR / 'scripts' / 'tk_audio_download.py'), 'scrape': str(PIPE_DIR / 'scripts' / 'scrape_tiktok_video.py'), 'transcribe': str(PIPE_DIR / 'scripts' / 'batch_transcribe_fw.py'), 'output_root': os.path.join(os.environ.__getitem__('WB_PIPELINE_DIR'), 'output'), 'proxy': os.environ.get('WB_PROXY', '')}
STATE_FILE = PIPE_DIR / 'pipeline_state.json'
RUN_LOG = PIPE_DIR / 'pipeline_run.log'

def log(step: str, msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {step} | {msg}"
    print(line, flush=True)
    with RUN_LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    return {}

def _account_media_type(account: str) -> str:
    """从 accounts.json 读账号 media_type（video/image）。缺省 = video（视频账号）。"""
    f = PIPE_DIR / 'accounts.json'
    if f.exists():
        d = json.loads(f.read_text(encoding='utf-8'))
        row = next((x for x in d.get('accounts', []) if x.get('account') == account), {})
        return row.get('media_type', '')
    return ''

def _is_image(a) -> bool:
    return getattr(a, 'media_type', '') == 'image'

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')

def _require_cfg(key: str, step: str) -> str:
    path = CFG[key]
    if not path:
        raise RuntimeError(f"{step} 未配置: CFG['{key}'] 为空（v1 脚本在本机缺失，需先部署）")
    if not Path(path).exists():
        raise RuntimeError(f'{step} 脚本不存在: {path}')
    return path

def _subprocess_step(step: str, cmd: list[str], env: dict | None=None) -> tuple[bool, str]:
    """跑外部脚本，解析 stdout 末尾的 JSON 产物行（约定脚本最后一行输出 JSON 摘要）。
    env: 追加到 os.environ 的覆盖项（如 s5 的 HF_HUB_OFFLINE，模型已缓存时
    禁联网检查——实测代理 502 会让转写整步炸掉）。"""
    import os
    log(step, '开始: ' + ' '.join(cmd[:3]) + ' ...')
    full_env = {**os.environ, **env} if env else None
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', env=full_env)
    if proc.returncode != 0:
        return (False, f'exit={proc.returncode} stderr={proc.stderr[-300:]}')
    for line in reversed(proc.stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith('{'):
            return (True, line)
    return (False, '脚本成功但未输出 JSON 摘要行（约定最后一条 stdout 为 JSON）')

def step0_check(a) -> tuple[bool, str]:
    import shutil
    missing = []
    if not Path(CFG['python']).exists():
        missing.append(f"venv python: {CFG['python']}")
    if not _is_image(a) and (not shutil.which('ffmpeg')):
        missing.append('ffmpeg（faster-whisper 解码依赖）')
    if missing:
        return (False, '前置缺失: ' + '; '.join(missing))
    return (True, 'venv 就绪（图文无需 ffmpeg）' if _is_image(a) else 'venv + ffmpeg 就绪')

def _audio_dir(a) -> Path:
    """账号音频目录：--audio-dir 显式覆盖，否则 out_root/{account}_audio。"""
    if getattr(a, 'audio_dir', None):
        return Path(a.audio_dir)
    return Path(CFG['output_root']) / f'{a.account}_audio'

def _out_dir() -> Path:
    """产物输出目录：--out-dir 显式覆盖，否则 output_root。"""
    if CFG.get('out_dir'):
        return Path(CFG['out_dir'])
    return Path(CFG['output_root'])

def step1_download(a) -> tuple[bool, str]:
    if _is_image(a):
        return (True, json.dumps({'matched': 0, 'downloaded': 0, 'skipped': 0, 'failed': []}) + ' | 图文账号跳过音频下载')
    script = _require_cfg('audio_download', 'Step1 音频下载')
    out_dir = _audio_dir(a)
    out_dir.mkdir(parents=True, exist_ok=True)
    return _subprocess_step('s1', [CFG['python'], script, '--account', a.account, '--from', a.date_from, '--to', a.date_to, '--out', str(out_dir)])

def _scraped_ids(path: Path) -> set:
    """已抓取过的视频 ID 集合（读 scrape_{account}.json）。"""
    try:
        d = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return set()
    return {i['id'] for i in d.get('items') or [] if i.get('id')}

def _audio_ids(audio_dir: Path, account: str) -> list:
    """音频目录里的视频 ID（与 scrape 脚本 collect_ids 规则一致：{account}_{id}.mp3）。"""
    pat = re.compile(f'^{re.escape(account)}_(\\d+)\\.mp3$')
    ids = []
    for f in sorted(audio_dir.glob(f'{account}_*.mp3')):
        m = pat.match(f.name)
        if m:
            ids.append(m.group(1))
    return ids

def _scrape_image(a, script: str, out_dir: Path) -> tuple[bool, str]:
    """图文账号 s2：完整枚举并按日期过滤；增量求差集，全量刷新已有作品，始终 merge 保留历史。

    图文帖走 /video/{id} 抓取解析（itemStruct 含 desc/stats/hashtags/createTime，探针已验证），
    与视频共用 scrape_{acc}.json 产物格式与 gate_scrape。不依赖音频目录（s1 已跳过）。
    """
    import subprocess as _sp
    enum = PIPE_DIR / 'scripts' / 'enumerate_image.py'
    proc = _sp.run([CFG['python'], str(enum), '--account', a.account, '--limit', '0', '--from', a.date_from, '--to', a.date_to], capture_output=True, text=True, encoding='utf-8', errors='replace')
    if proc.returncode != 0:
        return (False, f"图文枚举失败 exit={proc.returncode}: {(proc.stderr or proc.stdout or '')[-300:]}")
    ids: list[str] = []
    result = None
    for line in reversed((proc.stdout or '').strip().splitlines()):
        if line.startswith('{'):
            try:
                result = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                ids = []
            break
    if not isinstance(result, dict) or result.get('error') or (not isinstance(result.get('ids'), list)) or any((not isinstance(i, str) or not re.fullmatch('\\d{15,25}', i) for i in result['ids'])):
        return (False, '图文枚举失败：摘要结构无效或包含错误，历史产物未改动')
    ids = list(dict.fromkeys(result['ids']))
    have = {str(i) for i in _scraped_ids(out_dir / f'scrape_{a.account}.json')}
    new_ids = [i for i in ids if i not in have] if getattr(a, 'scrape_incremental', False) else ids
    policy = 'tolerant' if getattr(a, 'allow_scrape_errors', False) else 'strict'
    if not new_ids:
        p = out_dir / f'scrape_{a.account}.json'
        if not p.exists():
            payload = {'account': a.account, 'policy': policy, 'errors': [], 'items': [], 'media_type': 'image', 'enumeration_complete': True, 'window_empty': True}
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            tmp.replace(p)
            return (True, '图文日期范围内无作品：' + json.dumps(payload, ensure_ascii=False))
        try:
            r = json.loads(p.read_text(encoding='utf-8'))
        except Exception as e:
            return (False, f'图文增量：无新增视频，但已有产物不可读：{e}')
        payload = {'policy': policy, 'errors': r.get('errors', []), 'items': r.get('items', [])}
        return (True, '图文模式：%d 条近期作品均已有抓取记录，复用产物过门禁' % len(ids) + json.dumps(payload, ensure_ascii=False))
    log('s2', f'图文模式：日期范围内 {len(ids)} 条 / 已有 {len(have)} / 抓取 {len(new_ids)} 条（--merge）')
    cmd = [CFG['python'], script, '--account', a.account, '--ids', ','.join(new_ids), '--merge', '--policy', policy, '--out-dir', str(out_dir)]
    return _subprocess_step('s2', cmd)

def step2_scrape(a) -> tuple[bool, str]:
    """静态抓取视频详情（curl_cffi chrome124 指纹，2s 限流）。

    视频 ID 从 Step1 音频产物文件名推导。两种模式：
      · **默认（全量）**：抓音频目录里的全部视频 → 刷新所有历史条目的动态数据
        （播放/点赞/收藏）。代价 O(累计视频数)：9 账号实测约 2 小时。
      · **`--scrape-incremental`（增量）**：只抓「音频目录有、但 scrape json 里还没有」
        的视频，并配 `--merge` 与历史合并。日更只关心「最近一天的新内容」时用它，
        实测 1~2 分钟。
    """
    script = _require_cfg('scrape', 'Step2 页面抓取')
    out_dir = _out_dir()
    if _is_image(a):
        return _scrape_image(a, script, out_dir)
    audio_dir = _audio_dir(a)
    if not audio_dir.exists():
        return (False, f'音频目录不存在（Step1 应先行）: {audio_dir}')
    policy = 'tolerant' if getattr(a, 'allow_scrape_errors', False) else 'strict'
    cmd = [CFG['python'], script, '--account', a.account, '--audio-dir', str(audio_dir), '--out-dir', str(out_dir), '--policy', policy]
    if getattr(a, 'scrape_incremental', False):
        all_ids = _audio_ids(audio_dir, a.account)
        have = _scraped_ids(out_dir / f'scrape_{a.account}.json')
        new_ids = [i for i in all_ids if i not in have]
        if not new_ids:
            p = out_dir / f'scrape_{a.account}.json'
            if not p.exists():
                return (False, f'增量模式：无新增视频，且缺 {p.name}——请先做一次全量抓取（不加 --scrape-incremental）')
            try:
                r = json.loads(p.read_text(encoding='utf-8'))
            except Exception as e:
                return (False, f'增量模式：无新增视频，但已有产物不可读：{e}')
            payload = {'policy': r.get('policy', 'tolerant'), 'errors': r.get('errors', []), 'items': r.get('items', [])}
            return (True, '增量模式：%d 条音频均已有抓取记录，跳过抓取（复用已有产物过门禁）' % len(all_ids) + json.dumps(payload, ensure_ascii=False))
        cmd += ['--ids', ','.join(new_ids), '--merge']
        log('s2', f'增量模式：音频 {len(all_ids)} 条 / 已有 {len(have)} 条 / 本次只抓新增 {len(new_ids)} 条（--merge 保留历史）')
    return _subprocess_step('s2', cmd)

def step3_4_sheet(a) -> tuple[bool, str]:
    from scripts.standalone_tasks import pipeline_step
    return pipeline_step('build', a)

def step5_transcribe(a) -> tuple[bool, str]:
    if _is_image(a):
        return (True, json.dumps({'total': 0, 'done': [], 'suspect_hallucination': []}) + ' | 图文账号跳过转写')
    script = _require_cfg('transcribe', 'Step5 转写')
    audio_dir = _audio_dir(a)
    lang = getattr(a, 'lang', '')
    if not lang:
        accounts_file = PIPE_DIR / 'accounts.json'
        accounts = json.loads(accounts_file.read_text(encoding='utf-8')) if accounts_file.exists() else {}
        account = next((x for x in accounts.get('accounts', []) if x.get('account') == a.account), {})
        lang = account.get('lang') or 'en'
    cmd = [CFG['python'], script, '--audio-dir', str(audio_dir), '--model', 'medium', '--compute-type', 'int8', '--lang', lang, '--backend', getattr(a, 'backend', 'faster')]
    if lang in ('zh', ''):
        cmd.append('--simplified')
    return _subprocess_step('s5', cmd, env={'HF_HUB_OFFLINE': '1'})

def step6_translate(a) -> tuple[bool, str]:
    from scripts.standalone_tasks import pipeline_step
    return pipeline_step('translate', a)

def step5b_products(a) -> tuple[bool, str]:
    """商品挂车提取（手动一键运行版）：对 s2 抓到的视频跑 extract_products.py，
    产物落 {对标账号目录}/{account}/{account}_products.json（与 daily_update 同源路径）。

    仅当 s2_scrape 已在本次 steps 中跑过、或已有 scrape 产物时执行；
    eu 区账号跳过（按地区规则不抓商品，账号 region 在 accounts.json）。"""
    account = a.account
    if _is_image(a):
        return (True, '图文账号跳过商品提取（无小黄车）')
    import json as _json
    cfg = _json.loads((PIPE_DIR / 'accounts.json').read_text(encoding='utf-8')) if (PIPE_DIR / 'accounts.json').exists() else {}
    row = next((x for x in cfg.get('accounts', []) if x.get('account') == account), {})
    if row.get('region') == 'eu':
        return (True, 'eu 区账号跳过商品提取（地区规则）')
    out_dir = _out_dir()
    scrape_p = out_dir / f'scrape_{account}.json'
    if not scrape_p.exists():
        return (False, f'缺抓取产物 {scrape_p.name}（先跑 s2_scrape）')
    xlsx = Path(a.xlsx) if getattr(a, 'xlsx', None) else None
    aux = xlsx.parent if xlsx and xlsx.exists() else Path(os.environ.__getitem__('WB_LEDGER_DIR'))
    aux_dir = aux / account
    aux_dir.mkdir(parents=True, exist_ok=True)
    out_file = aux_dir / f'{account}_products.json'
    script = PIPE_DIR / 'scripts' / 'extract_products.py'
    cmd = [CFG['python'], str(script), '--account', account, '--scrape', str(scrape_p), '--out', str(out_file)]
    return _subprocess_step('s5b', cmd)

def step7_backfill(a) -> tuple[bool, str]:
    from scripts.standalone_tasks import pipeline_step
    return pipeline_step('backfill', a)

def step9_report(a) -> tuple[bool, str]:
    from scripts.standalone_tasks import pipeline_step
    return pipeline_step('report', a)
STEPS = {'s0_check': step0_check, 's1_download': step1_download, 's2_scrape': step2_scrape, 's3_4_sheet': step3_4_sheet, 's5_transcribe': step5_transcribe, 's5b_products': step5b_products, 's6_translate': step6_translate, 's7_backfill': step7_backfill, 's9_report': step9_report}
ORDER = ['s0_check', 's1_download', 's2_scrape', 's3_4_sheet', 's5_transcribe', 's5b_products', 's6_translate', 's7_backfill', 's9_report']
GATE_MAP = {'s1_download': 'download', 's2_scrape': 'scrape', 's5_transcribe': 'transcribe'}

def _exec_step(name: str, fn, args) -> tuple[bool, str, float]:
    """只跑步骤本体，不碰 state/门禁（供串行与并行两条路径复用）。"""
    t0 = time.time()
    try:
        ok, detail = fn(args)
    except Exception as e:
        ok, detail = (False, f'EXCEPTION: {e}')
    return (ok, detail, round(time.time() - t0, 1))

def _bookkeep(name: str, ok: bool, detail: str, cost: float, state: dict) -> bool:
    """写 state + 跑门禁 + 记日志（★必须在主线程串行调用：gate_report.json 是共享文件）。"""
    gate_note = ''
    if ok and name in GATE_MAP:
        try:
            i, j = (detail.index('{'), detail.rindex('}') + 1)
            artifact = json.loads(detail[i:j])
            report = PIPE_DIR / 'gate_report.json'
            ok = run_gates([(GATE_MAP[name], artifact)], report)
            gate_note = ' | 门禁:' + ('PASS' if ok else 'FAIL(见 gate_report.json)')
        except (ValueError, IndexError):
            ok, gate_note = (False, ' | 门禁:产物非 JSON，无法校验')
    state[f'{_current_account}|{name}'] = {'ok': ok, 'detail': detail[:500], 'cost_s': cost}
    save_state(state)
    log(name, f'ok={ok} cost={cost}s{gate_note} | {detail[:160]}')
    return ok
_current_account = ''

def run_step(name: str, fn, args, state: dict) -> bool:
    skey = f'{args.account}|{name}'
    rec = state.get(skey, {})
    image_scrape = _is_image(args) and name == 's2_scrape'
    window = [args.date_from, args.date_to] if image_scrape else None
    if rec.get('ok') and (not image_scrape or rec.get('date_range') == window):
        log(name, f"已完成，跳过（断点续跑）cost={rec.get('cost_s')}s")
        return True
    ok, detail, cost = _exec_step(name, fn, args)
    result = _bookkeep(name, ok, detail, cost, state)
    if image_scrape:
        state[skey]['date_range'] = window
        save_state(state)
    return result

def run_step_pair(pair: list[str], args, state: dict) -> bool:
    """★2026-10-14：把互不依赖的两步并行执行（目前只有 s2_scrape ∥ s5_transcribe）。

    依据：s2 只依赖 s1 的音频目录（推 video_id），s5 只依赖 s1 的音频文件；
    二者之间**无数据依赖**，且资源互补（s2 = 网络等待为主，s5 = GPU 计算为主），
    并行后墙钟 ≈ max(t_s2, t_s5) 而不是 t_s2 + t_s5。
    实测（thesymmetrymarket 6 条）：s2 约 45s、s5 约 39s → 省下约 39s。

    门禁仍串行执行：gate_report.json 是共享文件，两个步骤同时写会互相覆盖。
    """
    todo = [n for n in pair if not state.get(f'{args.account}|{n}', {}).get('ok')]
    for n in pair:
        if n not in todo:
            log(n, f"已完成，跳过（断点续跑）cost={state[f'{args.account}|{n}'].get('cost_s')}s")
    if not todo:
        return True
    results: dict[str, tuple[bool, str, float]] = {}
    if len(todo) == 1:
        n = todo[0]
        results[n] = _exec_step(n, STEPS[n], args)
    else:
        log('PARALLEL', '并行执行: ' + ' ∥ '.join(todo))
        with ThreadPoolExecutor(max_workers=len(todo)) as pool:
            futs = {pool.submit(_exec_step, n, STEPS[n], args): n for n in todo}
            for f in as_completed(futs):
                results[futs[f]] = f.result()
    ok_all = True
    for n in pair:
        if n in results:
            ok_all &= _bookkeep(n, *results[n], state)
    return ok_all

def main() -> int:
    p = argparse.ArgumentParser(description='TikTok 对标账号归档流水线（v2 骨架）')
    p.add_argument('--account', required=True)
    p.add_argument('--from', dest='date_from', required=True)
    p.add_argument('--to', dest='date_to', required=True)
    p.add_argument('--xlsx', required=True, help='目标台账（每次显式指定=v1 人工闸口#1）')
    p.add_argument('--steps', nargs='*', default=None, help='子集执行，默认全链')
    p.add_argument('--no-resume', action='store_true')
    p.add_argument('--no-parallel', action='store_true', help='禁用并行：s2_scrape ∥ s5_transcribe 退回全串行（排障用）')
    p.add_argument('--allow-scrape-errors', action='store_true', help='容忍下架/私有条（v1 §3 语义，人工闸口裁决）')
    p.add_argument('--scrape-incremental', action='store_true', help='s2 增量模式：只抓「音频目录里有、但 scrape_{account}.json 里还没有」的视频，并自动与历史合并。默认关闭（全量重抓=刷新所有历史条目动态数据）。日更场景建议开启——实测 9 账号全量约 2 小时，增量通常 1~2 分钟。')
    p.add_argument('--out-root', default=None, help="产物根目录（默认 CFG['output_root']）")
    p.add_argument('--lang', default='', help='转写语言（英文账号必须 en：auto/zh 会产生伪中文，实测教训）')
    p.add_argument('--backend', default='auto', choices=['auto', 'faster', 'openai'], help='转写后端（★2026-10-14 起默认 auto）：auto=有 CUDA 设备走 faster-whisper+GPU（实测 ~10x 实时），否则退回 openai-whisper/PyTorch CPU；faster=faster-whisper/ctranslate2；openai=openai-whisper/PyTorch')
    p.add_argument('--audio-dir', default=None, help='音频目录显式覆盖（默认 out-root/{account}_audio）')
    p.add_argument('--out-dir', default=None, help='产物输出目录显式覆盖（默认 out-root）')
    a = p.parse_args()
    a.media_type = _account_media_type(a.account)
    if a.out_root:
        CFG['output_root'] = a.out_root
    if a.out_dir:
        CFG['out_dir'] = a.out_dir
    log('START', f'account={a.account} range={a.date_from}~{a.date_to} xlsx={a.xlsx}')
    global _current_account
    _current_account = a.account
    state = {} if a.no_resume else load_state()
    order = a.steps or ORDER
    PARALLEL_PAIRS = [['s2_scrape', 's5_transcribe']]
    remaining = list(order)

    def _take_pair():
        """剩余步骤**开头**若正好是一整个可并行组合（且未全部完成）→ 返回其成员名。

        ★ 必须严格相邻且从 remaining[0] 起算：否则会把 s2∥s5 提前到 s1 之前执行
        （2026-10-14 实测踩到：s2 因音频目录还没有产物直接 FAIL）。s1 是它们共同的前置。
        """
        if a.no_parallel or _is_image(a):
            return None
        for pair in PARALLEL_PAIRS:
            members = [n for n in remaining if n in pair]
            if len(members) != len(pair):
                continue
            head = remaining.index(members[0])
            if head != 0:
                continue
            if [remaining[head + k] for k in range(len(pair))] != members:
                continue
            done = [n for n in pair if state.get(f'{a.account}|{n}', {}).get('ok')]
            if len(done) < len(pair):
                return members
        return None
    while remaining:
        hit = _take_pair()
        if hit:
            log('PARALLEL', '并行组: ' + ' ∥ '.join(hit))
            if not run_step_pair(hit, a, state):
                log('HALT', f"{' / '.join(hit)} 门禁未过，链路停止（详见 gate_report.json）")
                return 1
            remaining = [n for n in remaining if n not in hit]
            continue
        name = remaining.pop(0)
        if not run_step(name, STEPS[name], a, state):
            log('HALT', f'{name} 门禁未过，链路停止（详见 gate_report.json）')
            return 1
    log('DONE', '全链完成')
    return 0
if __name__ == '__main__':
    sys.exit(main())
