"""translate_pending.py — 批量翻译 N 列待译清单（google gtx 端点，走代理）

读 output/pending_trans.json（{acc:[{row,vid,text,lang}]}），逐条调
google translate gtx（POST，sl=en/es → zh-CN），带限流+重试+断点缓存，
输出 output/translations.json（{acc:{"row":"译文"}}），供
build_ledger_15col.py apply-trans 回填 N 列。

用法: python scripts/translate_pending.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import random
import subprocess
import time
from pathlib import Path
PIPE = Path(__file__).resolve().parent.parent
OUT = PIPE / 'output'
PENDING = OUT / 'pending_trans.json'
CACHE = OUT / 'trans_cache.json'
RESULT = OUT / 'translations.json'
GTX = 'https://translate.googleapis.com/translate_a/single'
CURL = 'curl'
PROXY = os.environ.get('WB_PROXY', '')

def translate(text, sl, retries=4):
    """用 curl 调 google gtx（requests 的 TLS 指纹被 gtx 拒 429，curl 被接受）。"""
    for i in range(retries):
        try:
            r = subprocess.run([CURL, '-s', '-x', PROXY, '-G', GTX, '--data-urlencode', 'client=gtx', '--data-urlencode', f'sl={sl}', '--data-urlencode', 'tl=zh-CN', '--data-urlencode', 'dt=t', '--data-urlencode', f'q={text}'], capture_output=True, text=True, encoding='utf-8', timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                j = json.loads(r.stdout)
                out = ''.join((seg[0] for seg in j[0] if seg and seg[0]))
                if out.strip():
                    return out.strip()
        except Exception:
            pass
        time.sleep(2 * (i + 1))
    return None

def main():
    pending = json.loads(PENDING.read_text(encoding='utf-8'))
    cache = json.loads(CACHE.read_text(encoding='utf-8')) if CACHE.exists() else {}
    result = {}
    total = sum((len(v) for v in pending.values()))
    done = 0
    fails = []
    for acc, lst in pending.items():
        result[acc] = {}
        for it in lst:
            vid = f"{acc}|{it['vid']}"
            text = it['text'].strip()
            if vid in cache:
                tr = cache[vid]
            else:
                tr = translate(text, it.get('lang', 'en'))
                if tr:
                    cache[vid] = tr
                    CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding='utf-8')
                else:
                    fails.append((acc, it['row'], text[:40]))
            if tr:
                result[acc][str(it['row'])] = tr
            done += 1
            if done % 50 == 0:
                print(f'  {done}/{total} 已翻 {len(cache)}', flush=True)
            time.sleep(random.uniform(0.1, 0.35))
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'完成: 译文 {sum((len(v) for v in result.values()))}/{total}，失败 {len(fails)}')
    if fails:
        print('失败样例:', fails[:8])
    print(f'-> {RESULT}')
if __name__ == '__main__':
    main()
