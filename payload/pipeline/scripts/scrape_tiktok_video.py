"""
scrape_tiktok_video.py — Step2 页面抓取（v1 规格：静态抓内嵌 JSON，2s 限流）

输入: --account --audio-dir（从产物文件名取视频 ID）或 --ids
输出: scrape_{account}.json + stdout 末行 JSON 摘要
摘要 schema（对齐 gate_scrape）:
  {"errors": [{"id","error"}], "items": [{"id","publish_time_gmt8",...}]}
规则: itemStruct 空（下架/私有）→ 进 errors[]，不猜数据，其余继续
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
PROXY = os.environ.get('WB_PROXY', '')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
RATE_LIMIT_SEC = 5
GMT8 = dt.timezone(dt.timedelta(hours=8))
UNIVERSAL_RE = re.compile('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', re.S)

def fetch_page(url: str) -> str:
    """统一抓取层（L1 scrapling → curl_cffi 兜底）+ 退避重试
    （TikTok 间歇性 SSL 掐断/风控 → 重试）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tiktok_fetch import fetch_html
    last = None
    for attempt in range(3):
        try:
            return fetch_html(url)
        except Exception as e:
            last = e
            time.sleep(3 * (attempt + 1))
    raise last

def parse_item(html: str) -> dict | None:
    m = UNIVERSAL_RE.search(html)
    if not m:
        return None
    data = json.loads(m.group(1))
    detail = data.get('__DEFAULT_SCOPE__', {}).get('webapp.video-detail') or {}
    return (detail.get('itemInfo') or {}).get('itemStruct') or None

def to_gmt8(create_time) -> str | None:
    """Unix 秒 → GMT+8 字符串。
    ★ 2026-10-05 修正：原实现为 fromtimestamp(ts) + 8h，但本机时区已是 UTC+8，
    fromtimestamp 返回的已是 GMT+8，再 +8h 会让发布时间**整体偏晚 8 小时**
    （实测台账 kee 7692612863776656670 记 17:06，真实为 09:06）。
    改为显式以 UTC 为基准换算，任何本机时区下结果都正确。"""
    if not create_time:
        return None
    return dt.datetime.fromtimestamp(int(create_time), GMT8).strftime('%Y-%m-%d %H:%M:%S')

def collect_ids(a) -> list[str]:
    if a.ids:
        return list(dict.fromkeys((x.strip() for x in a.ids.split(',') if x.strip())))
    pat = re.compile(f'^{re.escape(a.account)}_(\\d+)\\.mp3$')
    ids = []
    for f in sorted(Path(a.audio_dir).glob(f'{a.account}_*.mp3')):
        m = pat.match(f.name)
        if m:
            ids.append(m.group(1))
    return ids

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', required=True)
    ap.add_argument('--audio-dir', default='')
    ap.add_argument('--ids', default='')
    ap.add_argument('--out-dir', required=True)
    ap.add_argument('--policy', choices=['strict', 'tolerant'], default='strict', help='strict: errors 非空 gate 即停；tolerant: 下架条标注后继续')
    ap.add_argument('--merge', action='store_true', help='与已有 scrape_{account}.json **合并**（增量抓取用）。不开则整体覆盖——用 --ids 只抓部分视频时**务必加 --merge**，否则会把未参与本次抓取的历史条目冲掉。')
    ap.add_argument('--allow-overwrite', action='store_true', help='逃生口：明确知道自己在丢弃历史时使用，配合 --ids 才放行整体覆盖。')
    a = ap.parse_args()
    if a.ids and (not a.merge) and (not a.allow_overwrite):
        print(f'[ERROR] 检测到 --ids 但未加 --merge：这会用本次结果**整体覆盖** scrape_{a.account}.json，丢失未参与本次抓取的历史条目。\n        正确用法：加 --merge（保留历史，同 ID 以本次为准）\n        确需覆盖：显式加 --allow-overwrite，自担风险', file=sys.stderr)
        return 2
    ids = collect_ids(a)
    if not ids:
        print('[ERROR] 未找到待抓取视频ID，未覆盖历史抓取文件', file=sys.stderr)
        return 2
    out = Path(a.out_dir)
    path = out / f'scrape_{a.account}.json'
    old = {}
    if a.merge and path.exists():
        try:
            old = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(old, dict):
                raise ValueError('历史抓取文件不是对象')
            for key in ('items', 'errors'):
                rows = old.get(key, [])
                if not isinstance(rows, list) or any((not isinstance(row, dict) or not row.get('id') for row in rows)):
                    raise ValueError(f'历史抓取字段 {key} 结构不符')
        except (ValueError, OSError) as e:
            print(f'[ERROR] 无法合并历史抓取文件，未覆盖 {path}: {e}', file=sys.stderr)
            return 1
    items, errors = ([], [])
    total_sc = len(ids)
    for i, vid in enumerate(ids, start=1):
        print(f'[进度] 视频抓取 {i}/{total_sc}：{vid}', flush=True)
        url = f'https://www.tiktok.com/@{a.account}/video/{vid}'
        try:
            item = parse_item(fetch_page(url))
            if not item:
                errors.append({'id': vid, 'error': 'itemStruct empty（下架/私有）'})
            else:
                stats = item.get('stats', {})
                items.append({'id': vid, 'url': url, 'desc': item.get('desc') or '', 'hashtags': [c.get('title') for c in item.get('challenges') or [] if c.get('title')], 'publish_time_gmt8': to_gmt8(item.get('createTime')), 'stats': {k: stats.get(k) for k in ('playCount', 'diggCount', 'commentCount', 'shareCount', 'collectCount')}, 'music': ((item.get('music') or {}).get('title') or '')[:100], 'author': (item.get('author') or {}).get('uniqueId', a.account)})
        except Exception as e:
            errors.append({'id': vid, 'error': str(e)[:150]})
        if i < len(ids):
            time.sleep(RATE_LIMIT_SEC)
    out.mkdir(parents=True, exist_ok=True)
    if a.merge and path.exists():
        merged = {str(i['id']): dict(i, id=str(i['id'])) for i in old.get('items') or [] if i.get('id')}
        ok_ids = {i['id'] for i in items}
        for it in items:
            merged[it['id']] = it
        errs = {str(e['id']): dict(e, id=str(e['id'])) for e in old.get('errors') or [] if e.get('id')}
        for i in ok_ids:
            errs.pop(i, None)
        for e in errors:
            errs[e['id']] = e
        items = list(merged.values())
        errors = list(errs.values())
        print(f'[merge] 本次抓 {len(ok_ids)} 条，合并后 items={len(items)} errors={len(errors)}', file=sys.stderr)
    result = {'account': a.account, 'policy': a.policy, 'scraped_at': dt.datetime.now().isoformat(timespec='seconds'), 'errors': errors, 'items': items}
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)
    print(json.dumps({'policy': a.policy, 'errors': errors, 'items': items}, ensure_ascii=False))
    print(f'[saved] {path}', file=sys.stderr)
    return 0
if __name__ == '__main__':
    sys.exit(main())
