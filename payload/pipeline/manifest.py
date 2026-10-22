"""
manifest.py — 增量索引层（v3 方案 §1，2026-10-03）

每账号一份 manifest_{account}.json（存于该账号产物目录）：
  {"account": str,
   "last_success_date": "YYYY-MM-DD" | null,
   "videos": {video_id: {"first_seen": "YYYY-MM-DD",
                          "stages_done": ["download","scrape","transcript","products","gmv"],
                          "gmv_status": "below_threshold|pending|done"}}}

职责：
- 差集计算：窗口枚举 ID 与 manifest 已知 ID 的差集 = 新增集（去重唯一事实主键是 video_id）
- 窗口计算：last_success_date - recall_days 滚动召回（宁多勿漏，时间窗口只做召回不做判重）
- 从磁盘产物推导阶段（derive）：音频文件 / scrape items / 转写文件 / 商品 JSON / gmv 缓存
  —— 磁盘产物是唯一事实来源，manifest 是其索引；merge 只增不减（幂等重跑不回退进度）

纯 stdlib，可独立单测。
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import datetime as dt
import json
import re
from pathlib import Path
STAGE_ORDER = ['download', 'scrape', 'transcript', 'products', 'gmv']
AUDIO_EXTS = ('.mp3', '.m4a', '.wav')
GMV_RANK = {'below_threshold': 0, 'pending': 1, 'done': 2}

def manifest_path(out_dir, account: str) -> Path:
    return Path(out_dir) / f'manifest_{account}.json'

def load(out_dir, account: str) -> dict:
    p = manifest_path(out_dir, account)
    if p.exists():
        m = json.loads(p.read_text(encoding='utf-8'))
        m.setdefault('videos', {})
        m.setdefault('last_success_date', None)
        return m
    return {'account': account, 'last_success_date': None, 'videos': {}}

def save(out_dir, account: str, m: dict) -> Path:
    p = manifest_path(out_dir, account)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding='utf-8')
    return p

def new_ids(manifest: dict, enumerated: list[str]) -> list[str]:
    """窗口枚举 ID 中不在 manifest 的 = 新增集。"""
    known = set(manifest.get('videos', {}))
    return [i for i in enumerated if i not in known]

def compute_window(manifest: dict, today: dt.date, recall_days: int=2) -> tuple[str, str]:
    """滚动窗口：from = last_success_date - recall_days（无记录则 today - recall_days）。"""
    last = manifest.get('last_success_date')
    base = dt.date.fromisoformat(last) if last else today
    frm = base - dt.timedelta(days=recall_days)
    if frm > today:
        frm = today
    return (frm.isoformat(), today.isoformat())

def audio_ids(audio_dir, account: str) -> list[str]:
    pat = re.compile(f'^{re.escape(account)}_(\\d+)$')
    out = []
    d = Path(audio_dir)
    if not d.exists():
        return out
    for f in sorted(d.iterdir()):
        if f.suffix.lower() in AUDIO_EXTS and f.stat().st_size > 0:
            m = pat.match(f.stem)
            if m:
                out.append(m.group(1))
    return out

def scrape_ids(out_dir, account: str) -> tuple[list[str], list[str]]:
    """返回 (成功抓取 items 的 ID, errors 的 ID)。文件不存在返回 ([], [])。"""
    p = Path(out_dir) / f'scrape_{account}.json'
    if not p.exists():
        return ([], [])
    data = json.loads(p.read_text(encoding='utf-8'))
    items = [i['id'] for i in data.get('items', []) if i.get('id')]
    errors = [e['id'] for e in data.get('errors', []) if e.get('id')]
    return (items, errors)

def transcript_ids(audio_dir, account: str) -> list[str]:
    d = Path(audio_dir) / 'transcripts'
    if not d.exists():
        return []
    pat = re.compile(f'^{re.escape(account)}_(\\d+)_transcript\\.txt$')
    out = []
    for f in sorted(d.iterdir()):
        m = pat.match(f.name)
        if m and f.stat().st_size > 0:
            out.append(m.group(1))
    return out

def product_ids(out_dir, account: str, products_path=None) -> list[str]:
    """已做过商品提取的视频 ID。值是 list（含空列表=确认无挂车）即算完成；
    None（提取时网络失败）不算 → 次日自动重试，不会永久卡住也不会重复刷。

    products_path: 显式商品文件路径（2026-10-09 起商品文件在
    {台账目录}/{account}/{account}_products.json，与 out_dir 不一致）。
    缺省退回旧路径 out_dir/{account}_products.json（兼容）。"""
    p = Path(products_path) if products_path else Path(out_dir) / f'{account}_products.json'
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding='utf-8'))
    return [k for k, v in data.items() if isinstance(v, list)]

def _load_json(p: Path, default):
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default

def gmv_status_for(out_dir, video_id: str, account: str | None=None) -> str:
    """2026-10-10: 文件按账号隔离（{account}_fastmoss_cache.json / pending_gmv_{account}.json），
    account 给定时优先读账号文件（不存在视为空），缺省退回旧共享名（兼容）。"""
    d = Path(out_dir)
    if account:
        cache = _load_json(d / f'{account}_fastmoss_cache.json', {})
        pending = _load_json(d / f'pending_gmv_{account}.json', {})
    else:
        cache = _load_json(d / 'fastmoss_cache.json', {})
        pending = _load_json(d / 'pending_gmv.json', {})
    if video_id in cache:
        return 'done'
    pend_ids = {i.get('id') for i in pending.get('items', [])}
    if video_id in pend_ids:
        return 'pending'
    return 'below_threshold'

def derive(out_dir, account: str, audio_dir, today: str | None=None, products_path=None) -> dict:
    """从磁盘产物构建 manifest 快照（bootstrap 与日更 update 共用）。"""
    today = today or dt.date.today().isoformat()
    a_ids = set(audio_ids(audio_dir, account))
    s_items, s_errs = scrape_ids(out_dir, account)
    s_set = set(s_items)
    tr_ids = set(transcript_ids(audio_dir, account))
    pr_ids = set(product_ids(out_dir, account, products_path))
    videos = {}
    for vid in sorted(a_ids | s_set | set(s_errs)):
        stages = []
        if vid in a_ids:
            stages.append('download')
        if vid in s_set:
            stages.append('scrape')
        if vid in tr_ids:
            stages.append('transcript')
        if vid in pr_ids:
            stages.append('products')
        videos[vid] = {'first_seen': today, 'stages_done': stages, 'gmv_status': gmv_status_for(out_dir, vid, account=account)}
    return {'account': account, 'last_success_date': None, 'videos': videos}

def merge(dst: dict, src: dict) -> dict:
    """src 并入 dst：stages 并集、first_seen 取更早、gmv_status 取更高级。只增不减。"""
    for vid, info in src.get('videos', {}).items():
        cur = dst['videos'].get(vid)
        if cur is None:
            dst['videos'][vid] = {'first_seen': info['first_seen'], 'stages_done': list(info['stages_done']), 'gmv_status': info['gmv_status']}
            continue
        merged = sorted(set(cur['stages_done']) | set(info['stages_done']), key=STAGE_ORDER.index)
        cur['stages_done'] = merged
        cur['first_seen'] = min(cur['first_seen'], info['first_seen'])
        if GMV_RANK[info['gmv_status']] > GMV_RANK[cur['gmv_status']]:
            cur['gmv_status'] = info['gmv_status']
    return dst

def update_after_run(out_dir, account: str, audio_dir, success: bool=False, today: str | None=None, products_path=None) -> dict:
    """日更收尾：derive 当前产物状态 → merge 进 manifest → success 时推进 last_success_date。"""
    today = today or dt.date.today().isoformat()
    m = load(out_dir, account)
    derived = derive(out_dir, account, audio_dir, today, products_path=products_path)
    merge(m, derived)
    if success:
        m['last_success_date'] = today
    save(out_dir, account, m)
    return m
if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='manifest 工具（查看/从产物重建）')
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--account', required=True)
    ap.add_argument('--audio-dir', required=True)
    ap.add_argument('--rebuild', action='store_true', help='从产物重建（不推进 last_success_date）')
    a = ap.parse_args()
    if a.rebuild:
        m = update_after_run(a.out_dir, a.account, a.audio_dir, success=False)
    else:
        m = load(a.out_dir, a.account)
    print(json.dumps({'account': m['account'], 'last_success_date': m['last_success_date'], 'total': len(m['videos']), 'stages_hist': {s: sum((1 for v in m['videos'].values() if s in v['stages_done'])) for s in STAGE_ORDER}}, ensure_ascii=False, indent=2))
