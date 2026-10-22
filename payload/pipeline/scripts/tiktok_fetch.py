"""统一抓取层：L1 Scrapling Fetcher 优先，curl_cffi 兜底（v2 流水线专用）。

设计依据 scrapling-scraper 技能规程（逐级升级不跳级）：
- L1: scrapling Fetcher（Chrome TLS 指纹 + stealthy_headers + 代理）
- 兜底: curl_cffi chrome124（原方案，保证 scrapling 异常时流水线不断）

供 tk_audio_download.py（uid 解析）与 scrape_tiktok_video.py（详情抓取）
统一调用；解析逻辑（__UNIVERSAL_DATA_FOR_REHYDRATION__ 等）留在各脚本内。
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PROXY = os.environ.get('WB_PROXY', '')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
_last_strategy: str = ''

def last_strategy() -> str:
    """返回最近一次 fetch_html 实际使用的通道（scrapling / curl_cffi）。"""
    return _last_strategy

def fetch_html(url: str, timeout: int=30) -> str:
    """抓取 URL 返回 HTML 文本。先 L1 scrapling，失败降级 curl_cffi。"""
    global _last_strategy
    try:
        from scrapling.fetchers import Fetcher
        page = Fetcher.get(url, proxy=PROXY, impersonate='chrome124', timeout=timeout)
        html = getattr(page, 'html_content', None)
        if not html and getattr(page, 'body', None):
            html = page.body.decode('utf-8', errors='replace')
        if page.status == 200 and html and (len(html) > 1000):
            _last_strategy = 'scrapling'
            return html
        raise RuntimeError(f"scrapling status={getattr(page, 'status', '?')} len={len(html or '')}")
    except Exception:
        from curl_cffi import requests as cffi
        r = cffi.get(url, impersonate='chrome124', proxies={'http': PROXY, 'https': PROXY}, timeout=timeout, headers={'User-Agent': UA}, allow_redirects=True)
        r.raise_for_status()
        _last_strategy = 'curl_cffi'
        return r.text

def extract_universal_data(html: str) -> dict:
    """从 TikTok 页面 HTML 提取 __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON。"""
    import json, re
    m = re.search('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise RuntimeError('页面无内嵌数据（可能被风控或返回验证页）')
    return json.loads(m.group(1))
if __name__ == '__main__':
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else 'https://www.tiktok.com/@kee.yaden'
    h = fetch_html(url)
    print(f'strategy={last_strategy()} len={len(h)}')
