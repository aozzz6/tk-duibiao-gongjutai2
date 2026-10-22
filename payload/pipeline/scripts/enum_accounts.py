"""enum_accounts.py — 近三月视频量摸底（枚举不下载）

对指定账号用 tk_audio_download.enumerate_videos（yt-dlp extract_flat + uid 缓存），
按显式 GMT+8 过滤日期区间，统计区间内视频数 + 日期分布 + 首末样例。
仅枚举，不下载、不转写。用于定批次/总工作量。

用法: python scripts/enum_accounts.py [--from 2026-07-08 --to 2026-10-08]
      [--accounts charelttce kee.yaden mini.cases3 greenforu selsky88 lifewillcool]
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import datetime as dt
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tk_audio_download import enumerate_videos
GMT8 = dt.timezone(dt.timedelta(hours=8))
OUT = Path(__file__).resolve().parent.parent / 'enum_probe.json'

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='dfrom', default='2026-07-08')
    ap.add_argument('--to', dest='dto', default='2026-10-08')
    ap.add_argument('--accounts', nargs='+', default=['charelttce', 'kee.yaden', 'mini.cases3', 'greenforu', 'selsky88', 'lifewillcool'])
    a = ap.parse_args()
    d1 = dt.date.fromisoformat(a.dfrom)
    d2 = dt.date.fromisoformat(a.dto)
    report = {}
    for acc in a.accounts:
        try:
            entries = enumerate_videos(acc)
            in_range = []
            for e in entries:
                ts = e.get('timestamp')
                if not ts:
                    continue
                day = dt.datetime.fromtimestamp(ts, GMT8).date()
                if d1 <= day <= d2:
                    in_range.append({'id': str(e['id']), 'day': day.isoformat(), 'title': (e.get('title') or '')[:60]})
            in_range.sort(key=lambda x: x['day'])
            report[acc] = {'ok': True, 'total_listed': len(entries), 'in_range': len(in_range), 'range': f'{d1}~{d2}', 'first': in_range[0]['day'] if in_range else None, 'last': in_range[-1]['day'] if in_range else None, 'samples': in_range[:5]}
        except Exception as e:
            report[acc] = {'ok': False, 'error': str(e)[:200]}
        line = report[acc]
        print(f"[{acc}] ok={line.get('ok')} 区间视频={line.get('in_range', '?')} 总列出={line.get('total_listed', '?')}", flush=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[saved] {OUT}')
    print(json.dumps(report, ensure_ascii=False))
    return 0
if __name__ == '__main__':
    sys.exit(main())
