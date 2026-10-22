"""
enumerate_image.py — 图文账号作品枚举（2026-10-17）

与 tk_audio_download.py 的枚举同源（resolve_uid + yt-dlp tiktokuser:{uid} flat），
但不下载音频、不转写；指定日期时完整枚举，按 GMT+8 过滤作品 ID，供采集分支使用。

图文账号（accounts.json: media_type=image）在日更里用它替代「s1 音频下载 + 音频目录推导
video_id」：枚举近期 N 条 → 与 manifest 已知 ID 求差集 = 新增集 → s2 抓取这些新帖。

用法:
  python enumerate_image.py --account mitemma --from 2026-10-01 --to 2026-10-17
  python enumerate_image.py --account mitemma [--limit 40]  # 独立无日期调用
输出: stdout 末行 JSON  {"account","ids":[...],"count"}（ids 倒序，最新在前）
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
PROXY = os.environ.get('WB_PROXY', '')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'

def enumerate_recent(account: str, limit: int, date_from: str | None=None, date_to: str | None=None) -> list[str]:
    """指定日期时完整枚举并按 GMT+8 过滤；无日期的独立调用仍支持 limit。"""
    if limit < 0:
        raise ValueError('limit 不能为负数')
    if (date_from is None) != (date_to is None):
        raise ValueError('开始与结束日期必须同时提供')
    start = dt.date.fromisoformat(date_from) if date_from is not None else None
    end = dt.date.fromisoformat(date_to) if date_to is not None else None
    if start is not None and start > end:
        raise ValueError('开始日期不能晚于结束日期')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import tk_audio_download
    import yt_dlp
    uid = tk_audio_download.resolve_uid(account)
    opts = {'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist', 'proxy': PROXY, 'user_agent': UA}
    if limit > 0 and start is None:
        opts['playlistend'] = limit
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = None
        for attempt in range(3):
            try:
                info = ydl.extract_info(f'tiktokuser:{uid}', download=False)
                break
            except Exception as e:
                if attempt == 2:
                    raise
                time.sleep(20 * (attempt + 1))
    if not isinstance(info, dict):
        raise ValueError('图文枚举未返回有效账号数据')
    entries = info.get('entries')
    if entries is None:
        raise ValueError('图文枚举缺少作品列表')
    ids = []
    for e in entries:
        if not isinstance(e, dict) or not e.get('id'):
            continue
        if start is not None:
            ts = e.get('timestamp')
            if ts is None or isinstance(ts, bool):
                raise ValueError(f"作品 {e['id']} 缺少发布时间，无法保证日期范围完整")
            day = dt.datetime.fromtimestamp(float(ts), dt.timezone(dt.timedelta(hours=8))).date()
            if not start <= day <= end:
                continue
        ids.append(str(e['id']))
    seen, out = (set(), [])
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', required=True)
    ap.add_argument('--limit', type=int, default=40, help='无日期时的枚举上限（0=完整）；指定日期时完整枚举')
    ap.add_argument('--from', dest='date_from', default=None)
    ap.add_argument('--to', dest='date_to', default=None)
    a = ap.parse_args()
    try:
        ids = enumerate_recent(a.account, a.limit, a.date_from, a.date_to)
    except Exception as e:
        print(json.dumps({'account': a.account, 'ids': [], 'count': 0, 'error': f'{type(e).__name__}: {str(e)[:160]}'}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps({'account': a.account, 'ids': ids, 'count': len(ids), 'error': None}, ensure_ascii=False), flush=True)
    return 0
if __name__ == '__main__':
    sys.exit(main())
