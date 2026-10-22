import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

def load_json(path: Path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--scrape', required=True, help='scrape_{account}.json 路径')
    ap.add_argument('--min-plays', type=int, default=6000, help='播放量阈值，仅 ≥ 此值才查 FastMoss（默认 6000）')
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--account', default=None, help='账号名：给定时缓存/待查文件按账号隔离（{account}_fastmoss_cache.json / pending_gmv_{account}.json）。缺省退回旧共享文件名（兼容历史调用）。')
    a = ap.parse_args()
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if a.account:
        cache_path = out_dir / f'{a.account}_fastmoss_cache.json'
        pending_path = out_dir / f'pending_gmv_{a.account}.json'
    else:
        cache_path = out_dir / 'fastmoss_cache.json'
        pending_path = out_dir / 'pending_gmv.json'
    cache = load_json(cache_path, {})
    scrape = load_json(Path(a.scrape), {})
    items = scrape.get('items', [])
    results, pending = ([], [])
    for it in items:
        plays = (it.get('stats') or {}).get('playCount') or 0
        vid = it['id']
        if plays <= a.min_plays:
            continue
        hit = cache.get(vid)
        if hit:
            results.append({'id': vid, 'plays': plays, **hit})
        else:
            pending.append({'id': vid, 'plays': plays, 'url': f"https://www.tiktok.com/@{it.get('author', '')}/video/{vid}"})
    if pending:
        pending_path.write_text(json.dumps({'generated_at': dt.datetime.now().isoformat(timespec='seconds'), 'note': '待 FastMoss 查询（需登录态 MCP）；回写 fastmoss_cache.json 后重跑本脚本合并', 'items': pending}, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {'min_plays': a.min_plays, 'results': results, 'pending_ids': [p['id'] for p in pending]}
    print(json.dumps(summary, ensure_ascii=False))
    print(f'[saved] {pending_path}' if pending else '[info] 无待查项', file=sys.stderr)
    return 0
if __name__ == '__main__':
    sys.exit(main())
