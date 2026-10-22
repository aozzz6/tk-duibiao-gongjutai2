"""extract_products.py — 从 TikTok 视频页提取小黄车商品挂车数据

原理: 视频页 SSR 的 itemStruct.anchors 内含电商锚点（type=33 商品），
extra 字段是双重转义 JSON，内含 product_id / title / seller_id / skus。
输出链接格式: https://shop.tiktok.com/view/product/{product_id}

输入: --scrape scrape_{account}.json（取视频 ID）或 --ids 逗号分隔
      --account 账号名
输出: {out}/{account}_products.json: {video_id: [{"product_id","title","anchor_type"}]}
门禁对齐: 抓取失败重试 3 次 + 2s 限流（与 scrape_tiktok_video.py 同策略）
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
from tiktok_fetch import fetch_html

def extract_products_for_video(account: str, vid: str) -> list:
    """抓单个视频页，解析全部商品锚点。返回 [{product_id,title,anchor_type}]"""
    html = fetch_html(f'https://www.tiktok.com/@{account}/video/{vid}')
    import re
    m = re.search('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError('页面无内嵌数据（可能被风控/地区重定向，检查最终 URL）')
    data = json.loads(m.group(1))
    item = ((data.get('__DEFAULT_SCOPE__', {}).get('webapp.video-detail') or {}).get('itemInfo') or {}).get('itemStruct') or {}
    if not item:
        raise RuntimeError('itemStruct empty（下架/私有或风控），无法确认商品信息')
    products = []
    for a in item.get('anchors') or []:
        cur = a.get('extra')
        for _ in range(4):
            if isinstance(cur, str):
                try:
                    cur = json.loads(cur)
                except Exception:
                    break
            else:
                break
        lst = cur if isinstance(cur, list) else [cur] if isinstance(cur, dict) else []
        for e in lst:
            if not isinstance(e, dict):
                continue
            inner = e.get('extra')
            for _ in range(3):
                if isinstance(inner, str):
                    try:
                        inner = json.loads(inner)
                    except Exception:
                        break
                else:
                    break
            pid = str((inner or {}).get('product_id') or e.get('id') or '')
            title = (inner or {}).get('title') or e.get('keyword') or ''
            if pid.isdigit():
                products.append({'product_id': pid, 'title': title[:200], 'anchor_type': a.get('type')})
    return products

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', required=True)
    ap.add_argument('--scrape', help='scrape_{account}.json（取 items 的视频 ID）')
    ap.add_argument('--ids', help='逗号分隔视频 ID（与 --scrape 二选一）')
    ap.add_argument('--out', required=True, help='输出 JSON 路径')
    a = ap.parse_args()
    if a.ids:
        vids = [v.strip() for v in a.ids.split(',') if v.strip()]
    elif a.scrape:
        d = json.load(open(a.scrape, encoding='utf-8'))
        vids = [i['id'] for i in d.get('items', [])]
    else:
        ap.error('--scrape 或 --ids 必选其一')
    out: dict = {}
    failed = []
    for i, vid in enumerate(vids):
        ok = False
        for attempt in range(3):
            try:
                out[vid] = extract_products_for_video(a.account, vid)
                print(f'[{i + 1}/{len(vids)}] {vid} -> {len(out[vid])} 商品', file=sys.stderr)
                ok = True
                break
            except Exception as e:
                print(f'[{i + 1}/{len(vids)}] {vid} attempt{attempt + 1} err: {str(e)[:80]}', file=sys.stderr)
                time.sleep(6)
        if not ok:
            out[vid] = None
            failed.append(vid)
        time.sleep(2)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    n_empty = sum((1 for v in out.values() if v == []))
    print(json.dumps({'total': len(vids), 'with_products': sum((1 for v in out.values() if v)), 'no_products': n_empty, 'failed': failed}, ensure_ascii=False))
    return 0
if __name__ == '__main__':
    sys.exit(main())
