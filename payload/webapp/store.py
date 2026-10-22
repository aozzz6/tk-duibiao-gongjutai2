"""运行记录持久化：data/runs.jsonl（追加写，每行一条 JSON）。"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import threading
import time
from pathlib import Path
import config
_lock = threading.Lock()

def append(record: dict) -> None:
    with _lock:
        with config.RUNS_JSONL.open('a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

def load_all(limit: int=500) -> list[dict]:
    if not config.RUNS_JSONL.exists():
        return []
    out: list[dict] = []
    for line in config.RUNS_JSONL.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-limit:]

def update(run_id: str, patch: dict) -> None:
    """就地更新某条运行记录（重写文件，记录量级小，代价可接受）。"""
    with _lock:
        rows = load_all(limit=10 ** 9)
        for r in rows:
            if r.get('id') == run_id:
                r.update(patch)
                break
        tmp = Path(str(config.RUNS_JSONL) + '.tmp')
        tmp.write_text('\n'.join((json.dumps(r, ensure_ascii=False) for r in rows)) + ('\n' if rows else ''), encoding='utf-8')
        tmp.replace(config.RUNS_JSONL)

def sweep_stale_running() -> int:
    """启动时清扫僵尸运行记录：runs.jsonl 里残留的 status=running 全部标记为
    interrupted（附 finished_at）。webapp 进程刚启动时内存中必然没有任何运行，
    文件里的 running 说明上一个进程没来得及 _finish。

    ★ 2026-10-21 独立验收 P1-2：这只是一条「推断」，不是「进程已退出」的证据。
      runner 用普通 Popen，子进程可能仍在跑（重启时未被杀），所以这里必须显式写
      terminated_verified=false / needs_takeover=true，让下游（daily_batch）不能把它
      当成已终止而去自动重试（否则同一账号会并行起第二个 run）。
    返回清扫条数。幂等：再次调用时无 running 即 0。"""
    with _lock:
        rows = load_all(limit=10 ** 9)
        now = time.time()
        n = 0
        for r in rows:
            if r.get('status') == 'running':
                r['status'] = 'interrupted'
                r['finished_at'] = now
                r['terminated_verified'] = False
                r['needs_takeover'] = True
                r['note'] = 'webapp 重启：仅据 journal 推断中断，未核实原进程是否已退出'
                n += 1
        if n:
            tmp = Path(str(config.RUNS_JSONL) + '.tmp')
            tmp.write_text('\n'.join((json.dumps(r, ensure_ascii=False) for r in rows)) + ('\n' if rows else ''), encoding='utf-8')
            tmp.replace(config.RUNS_JSONL)
    return n
