"""
snapshot_metrics.py — v3 §3.2 时序快照：daily_metrics.jsonl（2026-10-03）

职责：对当日 scrape_{account}.json 的全部视频（= C1 全量 manifest 视频，
因为 s2 以音频目录为清单）输出一行快照追加到 daily_metrics.jsonl：
  {"date","account","video_id","age_days","plays","digg","comment",
   "share","collect","publish_time","gmv","gmv_status"}

趋势分析用法（一行 pandas）：
  播放增量 = 今日 plays - 昨日同 video_id 的 plays
  生命周期 = age_days vs plays 曲线（age_days = 快照日 - 发布日）

幂等：同 (date, account) 重跑 → 覆盖当日该账号行（读全文件 → 过滤 → 追加 → 重写）。
raw 层保持严格 append-only；本层以 (date, account) 为键去重，重跑安全。

输入: --scrape scrape_{account}.json  --account  --metrics-file daily_metrics.jsonl
      [--gmv-cache fastmoss_cache.json]  输出路径即数据源，缺省时 gmv=null
stdout 末行 JSON 摘要: {"account","date","records","gmv_done","gmv_pending"}
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import datetime as dt
import json
import sys
from pathlib import Path

def load_json(p: Path, default):
    return json.loads(p.read_text(encoding='utf-8')) if p.exists() else default

def snapshot_records(scrape: dict, account: str, today: str, gmv_cache: dict, pending_ids: set) -> list[dict]:
    recs = []
    for it in scrape.get('items', []):
        vid = it.get('id')
        if not vid:
            continue
        stats = it.get('stats') or {}
        pub = (it.get('publish_time_gmt8') or '')[:10]
        try:
            age = (dt.date.fromisoformat(today) - dt.date.fromisoformat(pub)).days
        except ValueError:
            age = None
        hit = gmv_cache.get(vid)
        if hit is not None:
            gmv, gmv_status = (hit.get('gmv'), 'done')
        elif vid in pending_ids:
            gmv, gmv_status = (None, 'pending')
        else:
            gmv, gmv_status = (None, 'below_threshold')
        recs.append({'date': today, 'account': account, 'video_id': vid, 'age_days': age, 'plays': stats.get('playCount'), 'digg': stats.get('diggCount'), 'comment': stats.get('commentCount'), 'share': stats.get('shareCount'), 'collect': stats.get('collectCount'), 'publish_time': it.get('publish_time_gmt8'), 'gmv': gmv, 'gmv_status': gmv_status})
    return sorted(recs, key=lambda r: r['video_id'])

def rewrite_metrics(metrics_file: Path, today: str, account: str, recs: list[dict]) -> int:
    """同日同账号幂等：剔除旧行 → 追加新行 → 重写。返回文件总行数。"""
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
            if row.get('date') == today and row.get('account') == account:
                continue
            kept.append(row)
    kept.extend(recs)
    metrics_file.parent.mkdir(parents=True, exist_ok=True)
    with metrics_file.open('w', encoding='utf-8') as f:
        for row in kept:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')
    return len(kept)

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--scrape', required=True)
    ap.add_argument('--account', required=True)
    ap.add_argument('--metrics-file', required=True)
    ap.add_argument('--gmv-cache', default='')
    ap.add_argument('--gmv-pending', default='')
    ap.add_argument('--date', default=None, help='覆盖快照日期（默认今天）')
    a = ap.parse_args()
    today = a.date or dt.date.today().isoformat()
    scrape = load_json(Path(a.scrape), {})
    gmv_cache = load_json(Path(a.gmv_cache), {}) if a.gmv_cache else {}
    pending = load_json(Path(a.gmv_pending), {}) if a.gmv_pending else {}
    pending_ids = {i.get('id') for i in pending.get('items', [])}
    recs = snapshot_records(scrape, a.account, today, gmv_cache, pending_ids)
    total = rewrite_metrics(Path(a.metrics_file), today, a.account, recs)
    summary = {'account': a.account, 'date': today, 'records': len(recs), 'gmv_done': sum((1 for r in recs if r['gmv_status'] == 'done')), 'gmv_pending': sum((1 for r in recs if r['gmv_status'] == 'pending')), 'metrics_total_lines': total}
    print(json.dumps(summary, ensure_ascii=False))
    return 0
if __name__ == '__main__':
    sys.exit(main())
