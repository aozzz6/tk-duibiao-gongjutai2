"""hot_product_monitor.py — 全类目爆品监控：每日快照 + 1/3/7 天差分（2026-10-05）

三层漏斗架构的 L1 发现层 + L2 波动引擎（零成本路线）：
  L1 发现层：拉取榜单快照落盘（当前数据源=TikTok Creative Center，见
            hot_monitor_sources.json；FastMoss 充值后可追加源）
  L2 波动引擎：今日快照 vs 1/3/7 天前快照差分 → 候选爆品清单

数据通道（2026-10-05 实测）：
  - 老 creative_radar_api 全部 40101（废弃）；新版 CC 趋势页为 SSR 直出——
    GET /creative/creativeCenter/trends/hashtag?region=US&period=7 带 cookie，
    页面内嵌 React Query 缓存 queryKey=['popular','hashtags',{...}]，
    20 条/页，字段：hashtagID/hashtagName/industryIDs/popularityCurve(7点时序)/
    publishCnt/rankIndex/topCreators/vv
  - 纯 curl_cffi 即可获取（无需浏览器）；cookie 用浏览器登录后导出
  - keyword/video 标签页为客户端加载，SSR 无数据（待后续浏览器自动化扩展）

命令：
  probe    探测数据源可达性/登录态（不带 cookie 也可跑，用于巡检）
  snapshot 拉取当日快照（幂等：同 (date, source) 重跑覆盖）
  diff     差分引擎：算 1/3/7 天波动，输出候选清单

产物（均落 pipeline/output/hot_monitor/）：
  snapshots/{date}/{source}.json   解析后的榜单快照（当日重跑覆盖）
  daily_metrics.jsonl              指标行 (date,source,item_id) 键幂等重写
  candidates.json                  差分候选爆品清单

示例：
  python scripts/hot_product_monitor.py probe
  python scripts/hot_product_monitor.py probe --cookie-file cc_cookie.txt
  python scripts/hot_product_monitor.py snapshot --cookie-file cc_cookie.txt
  python scripts/hot_product_monitor.py diff --windows 1,3,7 --min-growth-pct 30
"""
from __future__ import annotations
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
BASE = 'https://ads.tiktok.com'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
PROXY = os.environ.get('WB_PROXY', '')
RATE_LIMIT_SEC = 3.0
PIPELINE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = PIPELINE_DIR / 'output' / 'hot_monitor'
SOURCES_FILE = PIPELINE_DIR / 'hot_monitor_sources.json'
ID_KEYS = ['product_id', 'hashtagID', 'hashtag_id', 'ad_id', 'keyword', 'id', 'item_id']
NAME_KEYS = ['product_name', 'hashtagName', 'hashtag_name', 'keyword', 'ad_name', 'name', 'title']

def load_sources() -> list[dict]:
    cfg = json.loads(SOURCES_FILE.read_text(encoding='utf-8'))
    return [s for s in cfg['sources'] if s.get('enabled', True)]

def load_cookie(path: str) -> str:
    """支持两种格式：单行 Cookie 请求头原文；Netscape cookie jar（tab 分隔）。"""
    if not path:
        return ''
    raw = Path(path).read_text(encoding='utf-8').strip()
    if not raw:
        return ''
    if '\t' in raw:
        pairs = []
        for line in raw.splitlines():
            parts = line.split('\t')
            if len(parts) == 7 and (not parts[0].startswith('#')):
                pairs.append(f'{parts[5]}={parts[6]}')
        return '; '.join(pairs)
    return raw.replace('\n', '; ')

def api_get(path: str, params: dict, cookie: str):
    """curl_cffi chrome124 指纹请求（cc_api 型源）。返回 (http_status, biz_code, payload)。"""
    from curl_cffi import requests as cffi
    headers = {'User-Agent': UA, 'Referer': 'https://ads.tiktok.com/creative/creativeCenter/trends', 'Accept': 'application/json, text/plain, */*'}
    if cookie:
        headers['Cookie'] = cookie
    r = cffi.get(BASE + path, params=params, impersonate='chrome124', proxies={'http': PROXY, 'https': PROXY}, timeout=30, headers=headers, allow_redirects=False)
    try:
        body = r.json()
        return (r.status_code, body.get('code'), body)
    except Exception:
        return (r.status_code, None, r.text[:500])

def ssr_get(url: str, params: dict, cookie: str):
    """curl_cffi 抓 CC SSR 页面（cc_ssr 型源）。返回 (http_status, html)。"""
    from curl_cffi import requests as cffi
    headers = {'User-Agent': UA, 'Referer': 'https://ads.tiktok.com/creative/creativeCenter/trends', 'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'}
    if cookie:
        headers['Cookie'] = cookie
    r = cffi.get(url, params=params, impersonate='chrome124', proxies={'http': PROXY, 'https': PROXY}, timeout=60, headers=headers, allow_redirects=True)
    return (r.status_code, r.text)

def _balanced_json(raw: str) -> int:
    """返回从 raw 开头（必须是 '{'）起第一个配平的 JSON 对象结束位置。"""
    depth, instr, esc = (0, False, False)
    for i, ch in enumerate(raw):
        if instr:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                instr = False
        elif ch == '"':
            instr = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return i + 1
    return -1

def extract_ssr_items(html: str) -> tuple[list[dict], list]:
    """从 SSR 页面提取内嵌 React Query 缓存中的榜单数据。

    返回 (items, queries概览)。定位所有 dehydratedState 块，取
    queryKey[0]=='popular' 且 data 形如 {pages:[{data:[...]}]} 的查询。
    """
    items: list[dict] = []
    queries: list = []
    for m in re.finditer('\\{"dehydratedState"', html):
        end = _balanced_json(html[m.start():])
        if end < 0:
            continue
        try:
            j = json.loads(html[m.start():m.start() + end])
        except json.JSONDecodeError:
            continue
        for q in j['dehydratedState'].get('queries', []):
            key = q.get('queryKey')
            data = q.get('state', {}).get('data')
            queries.append(key if key else '?')
            if isinstance(key, list) and key and (key[0] == 'popular') and isinstance(data, dict) and ('pages' in data):
                for page in data['pages']:
                    rows = page.get('data') or []
                    if isinstance(rows, list):
                        items.extend(rows)
    return (items, queries)

def extract_api_items(payload) -> list[dict]:
    """cc_api 响应中定位榜单数组：data.list / data.items / data.rank_info。"""
    if not isinstance(payload, dict):
        return []
    data = payload.get('data')
    if not isinstance(data, dict):
        return []
    for key in ('list', 'items', 'rank_info'):
        v = data.get(key)
        if isinstance(v, list) and v:
            return v
    return []

def extract_id(item: dict) -> str:
    for k in ID_KEYS:
        v = item.get(k)
        if v:
            return str(v)
    return ''

def extract_name(item: dict) -> str:
    for k in NAME_KEYS:
        v = item.get(k)
        if v:
            return str(v)
    return ''

def extract_metrics(item: dict) -> dict:
    """收集数值字段作为指标（增长率差分用）+ popularityCurve 派生指标。

    CC SSR 的数值常为字符串（"890542254"），统一强转 int/float。
    """
    out = {}
    for k, v in item.items():
        if k in ID_KEYS:
            continue
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str):
            try:
                out[k] = float(v.replace(',', ''))
            except ValueError:
                pass
    curve = item.get('popularityCurve')
    if isinstance(curve, list) and len(curve) >= 2:
        vals = [p.get('value') for p in curve if isinstance(p, dict) and isinstance(p.get('value'), (int, float))]
        if len(vals) >= 2:
            out['curve_last'] = vals[-1]
            out['curve_prev'] = vals[-2]
    return out

def cmd_probe(a) -> int:
    cookie = load_cookie(a.cookie_file)
    print(json.dumps({'cookie_loaded': bool(cookie), 'sources': len(load_sources())}, ensure_ascii=False))
    for s in load_sources():
        stype = s.get('type', 'cc_api')
        if stype == 'cc_ssr':
            status, html = ssr_get(s['url'], s.get('params', {}), cookie)
            items, queries = extract_ssr_items(html)
            verdict = 'ok' if items else 'need_login' if not cookie else 'empty'
            print(json.dumps({'source': s['name'], 'type': stype, 'http': status, 'items': len(items), 'verdict': verdict, 'queries': queries[:6]}, ensure_ascii=False))
        else:
            status, code, payload = api_get(s['path'], s.get('params', {}), cookie)
            items = extract_api_items(payload)
            verdict = 'ok' if status == 200 and code == 0 and items else 'need_login' if code == 40101 else 'endpoint_dead' if status == 404 else 'error'
            print(json.dumps({'source': s['name'], 'type': stype, 'http': status, 'biz_code': code, 'items': len(items), 'verdict': verdict, 'msg': (payload or {}).get('msg') if isinstance(payload, dict) else ''}, ensure_ascii=False))
        time.sleep(RATE_LIMIT_SEC)
    return 0

def snapshot_source(s: dict, cookie: str, date: str) -> dict:
    stype = s.get('type', 'cc_api')
    if stype == 'cc_ssr':
        status, html = ssr_get(s['url'], s.get('params', {}), cookie)
        items, queries = extract_ssr_items(html)
        payload = {'url': s['url'], 'params': s.get('params', {}), 'queries_seen': queries[:10], 'items': items}
        biz_code = 0 if items else None
    else:
        status, biz_code, payload = api_get(s['path'], s.get('params', {}), cookie)
        items = extract_api_items(payload)
    raw_path = OUT_DIR / 'snapshots' / date / f"{s['name']}.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps({'fetched_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'source': s['name'], 'http_status': status, 'biz_code': biz_code, 'item_count': len(items), 'payload': payload}, ensure_ascii=False, indent=1), encoding='utf-8')
    rows = []
    for rank, item in enumerate(items, 1):
        iid = extract_id(item)
        if not iid:
            continue
        rows.append({'date': date, 'source': s['name'], 'item_id': iid, 'name': extract_name(item), 'rank': rank, 'metrics': extract_metrics(item)})
    return {'source': s['name'], 'http': status, 'biz_code': biz_code, 'items': len(items), 'rows': rows}

def rewrite_metrics(metrics_file: Path, date: str, source: str, rows: list[dict]) -> int:
    """同日同源幂等：剔除旧行 → 追加新行 → 重写。返回文件总行数。"""
    kept = []
    if metrics_file.exists():
        for line in metrics_file.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get('date') == date and row.get('source') == source:
                continue
            kept.append(row)
    kept.extend(rows)
    metrics_file.parent.mkdir(parents=True, exist_ok=True)
    with metrics_file.open('w', encoding='utf-8') as f:
        for row in kept:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    return len(kept)

def cmd_snapshot(a) -> int:
    cookie = load_cookie(a.cookie_file)
    if not cookie:
        print(json.dumps({'error': 'CC 数据通道要求登录，请先 --cookie-file 提供登录 Cookie'}, ensure_ascii=False))
        return 2
    date = a.date or dt.date.today().isoformat()
    metrics_file = OUT_DIR / 'daily_metrics.jsonl'
    summary = {'date': date, 'sources': []}
    ok = 0
    for s in load_sources():
        r = snapshot_source(s, cookie, date)
        if r['rows']:
            rewrite_metrics(metrics_file, date, s['name'], r['rows'])
            ok += 1
        summary['sources'].append({k: r[k] for k in ('source', 'http', 'biz_code', 'items')})
        time.sleep(RATE_LIMIT_SEC)
    summary['ok_sources'] = ok
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if ok else 2

def load_history(metrics_file: Path) -> dict:
    """{(source, item_id): {date: row}}"""
    hist: dict[tuple, dict] = {}
    if not metrics_file.exists():
        return hist
    for line in metrics_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        hist.setdefault((row['source'], row['item_id']), {})[row['date']] = row
    return hist

def nearest_date(available: list[str], target: str, tolerance_days: int=2):
    """找 ≤ target 的最近快照日（容忍缺跑 gap ≤ tolerance 天），返回 (date, gap) 或 None。"""
    t = dt.date.fromisoformat(target)
    cands = []
    for d in available:
        dd = dt.date.fromisoformat(d)
        gap = (t - dd).days
        if 0 <= gap <= tolerance_days:
            cands.append((gap, d))
    if not cands:
        return None
    gap, best = min(cands)
    return (best, gap)

def cmd_diff(a) -> int:
    metrics_file = OUT_DIR / 'daily_metrics.jsonl'
    hist = load_history(metrics_file)
    if not hist:
        print(json.dumps({'error': '无快照历史，请先 snapshot（需积累 ≥1 天）'}, ensure_ascii=False))
        return 2
    all_dates = sorted({d for m in hist.values() for d in m})
    today = a.date or all_dates[-1]
    windows = [int(x) for x in a.windows.split(',')]
    min_growth = a.min_growth_pct / 100.0
    candidates = []
    for (source, item_id), by_date in hist.items():
        if today not in by_date:
            continue
        cur = by_date[today]
        for w in windows:
            base_day = (dt.date.fromisoformat(today) - dt.timedelta(days=w)).isoformat()
            hit = nearest_date(list(by_date), base_day)
            if not hit:
                continue
            base_date, _gap = hit
            base = by_date[base_date]
            actual_days = (dt.date.fromisoformat(today) - dt.date.fromisoformat(base_date)).days
            growths = {}
            for k, new_v in cur['metrics'].items():
                old_v = base['metrics'].get(k)
                if isinstance(old_v, (int, float)) and old_v > 0:
                    growths[k] = (new_v - old_v) / old_v
            if not growths:
                continue
            best_k = max(growths, key=growths.get)
            best_g = growths[best_k]
            rank_delta = (base.get('rank') or 0) - (cur.get('rank') or 0)
            if best_g >= min_growth or rank_delta >= a.min_rank_jump:
                candidates.append({'source': source, 'item_id': item_id, 'name': cur.get('name', ''), 'window_days': actual_days, 'base_date': base_date, 'rank_now': cur.get('rank'), 'rank_delta': rank_delta, 'top_metric': best_k, 'growth_pct': round(best_g * 100, 1), 'old': base['metrics'].get(best_k), 'new': cur['metrics'].get(best_k)})
    candidates.sort(key=lambda c: -c['growth_pct'])
    candidates = candidates[:a.top]
    out = OUT_DIR / 'candidates.json'
    out.write_text(json.dumps({'generated_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'today': today, 'history_dates': all_dates, 'threshold': {'min_growth_pct': a.min_growth_pct, 'min_rank_jump': a.min_rank_jump}, 'count': len(candidates), 'candidates': candidates}, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({'today': today, 'history_dates': len(all_dates), 'candidates': len(candidates), 'out': str(out)}, ensure_ascii=False))
    return 0

def main() -> int:
    ap = argparse.ArgumentParser(description='全类目爆品监控：快照 + 1/3/7 天差分')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('probe', help='探测数据源可达性/登录态')
    p.add_argument('--cookie-file', default='')
    p = sub.add_parser('snapshot', help='拉取当日快照（幂等）')
    p.add_argument('--cookie-file', required=True)
    p.add_argument('--date', default=None)
    p = sub.add_parser('diff', help='差分引擎，输出候选爆品清单')
    p.add_argument('--windows', default='1,3,7')
    p.add_argument('--min-growth-pct', type=float, default=30.0)
    p.add_argument('--min-rank-jump', type=int, default=5)
    p.add_argument('--top', type=int, default=20)
    p.add_argument('--date', default=None)
    a = ap.parse_args()
    return {'probe': cmd_probe, 'snapshot': cmd_snapshot, 'diff': cmd_diff}[a.cmd](a)
if __name__ == '__main__':
    sys.exit(main())
