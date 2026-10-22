"""
retry_scrape_failed.py — s2 抓取失败条目的慢速补抓（抗风控）

背景（2026-10-05）：scrape_tiktok_video.py 以固定 2s 间隔连抓 30+ 条时，TikTok 会从中途
开始返回"无内嵌数据的风控页"，被误判为 itemStruct empty（下架/私有），导致新视频数据
整批缺失。实测放慢到 7~15s 间隔后同一批条目可成功抓取。

本脚本读 scrape_{account}.json 的 errors[]，以长间隔重试 rounds 轮，成功条目并入 items[]
并回写同一文件（幂等：已在 items 中的 id 不重复加入）。

用法:
  python retry_scrape_failed.py --account vivi_ashop --out-dir "D:/1/对标账号/vivi_ashop"       [--interval 8] [--rounds 1]
输出: 末行 JSON {"account","fixed","remaining"}
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import json
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape_tiktok_video as sc

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', required=True)
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--interval', type=float, default=8.0)
    ap.add_argument('--rounds', type=int, default=1)
    a = ap.parse_args()
    f = Path(a.out_dir) / f'scrape_{a.account}.json'
    if not f.exists():
        print(json.dumps({'account': a.account, 'fixed': 0, 'remaining': 0, 'note': 'scrape 文件不存在'}, ensure_ascii=False))
        return 0
    d = json.loads(f.read_text(encoding='utf-8'))
    have = {i.get('id') for i in d.get('items', [])}
    fixed_total = 0
    for _ in range(max(1, a.rounds)):
        failed = [e['id'] for e in d.get('errors', []) if e.get('id')]
        if not failed:
            break
        still = []
        for vid in failed:
            if vid in have:
                continue
            url = f'https://www.tiktok.com/@{a.account}/video/{vid}'
            item = None
            try:
                item = sc.parse_item(sc.fetch_page(url))
            except Exception:
                item = None
            if item:
                stats = item.get('stats', {})
                d['items'].append({'id': vid, 'url': url, 'desc': item.get('desc') or '', 'hashtags': [c.get('title') for c in item.get('challenges') or [] if c.get('title')], 'publish_time_gmt8': sc.to_gmt8(item.get('createTime')), 'stats': {k: stats.get(k) for k in ('playCount', 'diggCount', 'commentCount', 'shareCount', 'collectCount')}, 'music': ((item.get('music') or {}).get('title') or '')[:100], 'author': (item.get('author') or {}).get('uniqueId', a.account)})
                have.add(vid)
                fixed_total += 1
            else:
                still.append({'id': vid, 'error': 'itemStruct empty（下架/私有）'})
            time.sleep(a.interval)
        d['errors'] = still
        f.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'account': a.account, 'fixed': fixed_total, 'remaining': len(d.get('errors', []))}, ensure_ascii=False))
    return 0
if __name__ == '__main__':
    sys.exit(main())
