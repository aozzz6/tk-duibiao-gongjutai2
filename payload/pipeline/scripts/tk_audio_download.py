"""
tk_audio_download.py — Step1 音频下载（v1 规格：yt-dlp 原生 audio 流）

输入: --account --from --to (YYYY-MM-DD) --out
输出: 产物 {account}_{video_id}.mp3 + stdout 末行 JSON 摘要
摘要 schema（对齐 gate_download）:
  {"matched": n, "downloaded": n, "skipped": n, "failed": [{"id","error"}]}
幂等: 产物文件已存在且 >0 字节 → skipped
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
PROXY = os.environ.get('WB_PROXY', '')
ENUM_LIMIT = int(os.environ.get('TK_ENUM_LIMIT', '80'))
GMT8 = dt.timezone(dt.timedelta(hours=8))

def resolve_uid(account: str) -> str:
    """主页 SSR 不内嵌视频列表，但含 uid → yt-dlp 需 tiktokuser:{uid} 形式
    （yt-dlp 2026.8 的 @account 直接提取报 'Unable to extract secondary user ID'）。
    uid 为稳定标识：解析成功后落盘缓存，绕开间歇性主页风控。
    抓取走统一层 tiktok_fetch（L1 scrapling → curl_cffi 兜底）。"""
    import re
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tiktok_fetch import fetch_html
    cache = Path(__file__).resolve().parent / '.uid_cache.json'
    cache_data = json.loads(cache.read_text(encoding='utf-8')) if cache.exists() else {}
    if account in cache_data:
        return cache_data[account]
    last_err = None
    for attempt in range(3):
        try:
            html = fetch_html(f'https://www.tiktok.com/@{account}')
            m = re.search('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
            if not m:
                raise RuntimeError('主页无内嵌数据（可能被风控，检查代理）')
            data = json.loads(m.group(1))
            ui = (data.get('__DEFAULT_SCOPE__', {}).get('webapp.user-detail', {}).get('userInfo') or {}).get('user', {})
            uid = ui.get('id')
            if not uid:
                raise RuntimeError(f'未解析到 uid（账号可能不存在或私密）: {account}')
            cache_data[account] = str(uid)
            cache.write_text(json.dumps(cache_data, ensure_ascii=False, indent=2), encoding='utf-8')
            return str(uid)
        except Exception as e:
            last_err = e
            wait = 20 * (attempt + 1)
            print(f'[retry] uid 解析失败({e})，{wait}s 后重试', file=sys.stderr)
            time.sleep(wait)
    raise last_err

def enumerate_videos(account: str):
    """两阶段枚举：主页 HTML 取 uid → yt-dlp tiktokuser:{uid} flat 枚举。

    ★ 2026-10-14：加 playlistend 截断（默认 80 条）。原实现拉全量，实测
    thesymmetrymarket（3181 条）**耗时 290s**——比整条转写链还长，而日更只关心
    最近 recall_days 天的几条。TikTok 主页 feed 按时间倒序，取前 N 条即可覆盖
    近日窗口（N=80 对日更绰绰有余；账号单日发帖 >80 条才会漏，现实不会）。
    需要全量时用 `--enum-all` 或 ENUM_LIMIT=0 显式打开。
    """
    import yt_dlp
    uid = resolve_uid(account)
    opts = {'quiet': True, 'no_warnings': True, 'extract_flat': 'in_playlist', 'proxy': PROXY, 'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'}
    if ENUM_LIMIT > 0:
        opts['playlistend'] = ENUM_LIMIT
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = None
        for attempt in range(3):
            try:
                info = ydl.extract_info(f'tiktokuser:{uid}', download=False)
                break
            except Exception as e:
                if attempt == 2:
                    raise
                wait = 20 * (attempt + 1)
                print(f'[retry] 枚举失败({e.__class__.__name__})，{wait}s 后重试', file=sys.stderr)
                time.sleep(wait)
    entries = info.get('entries') or []
    return [e for e in entries if e.get('id')]

def download_audio(account: str, vid: str, out_dir: Path) -> None:
    import yt_dlp
    target = out_dir / f'{account}_{vid}.mp3'
    opts = {'quiet': True, 'no_warnings': True, 'proxy': PROXY, 'outtmpl': str(out_dir / f'{account}_{vid}.%(ext)s'), 'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '64'}]}
    with yt_dlp.YoutubeDL(opts) as ydl:
        last = None
        for attempt in range(3):
            try:
                ydl.download([f'https://www.tiktok.com/@{account}/video/{vid}'])
                return
            except Exception as e:
                last = e
                time.sleep(5 * (attempt + 1))
        raise last

def main() -> int:
    global ENUM_LIMIT
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', required=True)
    ap.add_argument('--from', dest='date_from', required=True)
    ap.add_argument('--to', dest='date_to', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--enum-limit', type=int, default=None, help=f'枚举条数上限（缺省取环境变量 TK_ENUM_LIMIT，当前 {ENUM_LIMIT}；0=全量，很慢）')
    ap.add_argument('--enum-all', action='store_true', help='等价 --enum-limit 0（全量枚举，账号作品多时可达数分钟）')
    ap.add_argument('--workers', type=int, default=int(os.environ.get('TK_DL_WORKERS', '3')), help='下载并发数（默认 3；1=退回串行）')
    a = ap.parse_args()
    if a.enum_all:
        ENUM_LIMIT = 0
    elif a.enum_limit is not None:
        ENUM_LIMIT = a.enum_limit
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    d1 = dt.datetime.strptime(a.date_from, '%Y-%m-%d').date()
    d2 = dt.datetime.strptime(a.date_to, '%Y-%m-%d').date()
    entries = None
    try:
        entries = enumerate_videos(a.account)
    except Exception as e:
        existing = sorted(out_dir.glob(f'{a.account}_*.mp3'))
        if existing:
            print(f'[warn] 枚举失败({str(e)[:80]})，降级为以盘上 {len(existing)} 个产物计数', file=sys.stderr)
            summary = {'account': a.account, 'range': f'{a.date_from}~{a.date_to}', 'matched': len(existing), 'no_timestamp': 0, 'downloaded': 0, 'skipped': len(existing), 'failed': [], 'degraded': f'enumerate_failed: {str(e)[:100]}'}
            print(json.dumps(summary, ensure_ascii=False))
            return 0
        raise
    matched = []
    no_ts = 0
    for e in entries:
        ts = e.get('timestamp')
        if ts is None:
            no_ts += 1
            continue
        day = dt.datetime.fromtimestamp(ts, GMT8).date()
        if d1 <= day <= d2:
            matched.append({'id': str(e['id']), 'day': day.isoformat(), 'title': (e.get('title') or '')[:50]})
    downloaded = skipped = 0
    failed = []
    total_dl = len(matched)
    todo = []
    for mi, m in enumerate(matched, start=1):
        target = out_dir / f"{a.account}_{m['id']}.mp3"
        if target.exists() and target.stat().st_size > 0:
            skipped += 1
            print(f'[进度] 音频下载 {mi}/{total_dl}：已有，跳过', flush=True)
        else:
            todo.append((mi, m))
    if todo:
        workers = max(1, min(a.workers, len(todo)))
        print(f'[info] 待下载 {len(todo)} 条，并发 {workers}', file=sys.stderr)
        done_cnt = [0]

        def _one(item):
            mi, m = item
            print(f'[进度] 音频下载 {mi}/{total_dl}：正在下载…', flush=True)
            download_audio(a.account, m['id'], out_dir)
            done_cnt[0] += 1
            print(f'[进度] 音频下载 {mi}/{total_dl}：完成', flush=True)
        if workers == 1:
            for item in todo:
                try:
                    _one(item)
                    downloaded += 1
                except Exception as e:
                    failed.append({'id': item[1]['id'], 'error': str(e)[:150]})
                time.sleep(1)
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(_one, it): it for it in todo}
                for f in as_completed(futs):
                    it = futs[f]
                    try:
                        f.result()
                        downloaded += 1
                    except Exception as e:
                        failed.append({'id': it[1]['id'], 'error': str(e)[:150]})
    summary = {'account': a.account, 'range': f'{a.date_from}~{a.date_to}', 'matched': len(matched), 'no_timestamp': no_ts, 'downloaded': downloaded, 'skipped': skipped, 'failed': failed}
    print(json.dumps(summary, ensure_ascii=False))
    return 0
if __name__ == '__main__':
    sys.exit(main())
