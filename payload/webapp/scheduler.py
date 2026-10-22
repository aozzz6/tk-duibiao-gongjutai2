"""定时调度：每日定点跑 `pipeline/scripts/daily_batch.py`（回执契约 + 重试 + 超时 + 终验）。

★ 2026-10-21 独立验收 P1-D：入口从 `pipeline/daily_update.py` 改为 `daily_batch.py`。
  旧编排写回执时直接覆写当日 summary/latest，一次单账号补跑就会抹掉未跑账号与
  `ledger_verify` 失败（面板假绿）；现在定时任务与「待接管重跑」共用同一条入口。

配置存 data/schedule.json：
  {"enabled": true, "time": "09:30", "accounts": [], "last_run": "2026-10-09"}
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import datetime as dt
import re
import threading
import time
import config
import runner
import store
DEFAULT_SCHEDULE = {'enabled': False, 'time': '09:30', 'accounts': [], 'last_run': None}
_tick_lock = threading.Lock()

def load() -> dict:
    cfg = config.read_json(config.SCHEDULE_JSON, None)
    if not isinstance(cfg, dict):
        cfg = dict(DEFAULT_SCHEDULE)
    for k, v in DEFAULT_SCHEDULE.items():
        cfg.setdefault(k, v)
    return cfg

def save(cfg: dict) -> dict:
    cfg = {**DEFAULT_SCHEDULE, **cfg}
    if not isinstance(cfg['enabled'], bool):
        raise ValueError('enabled 必须是布尔值')
    if not isinstance(cfg['time'], str) or not re.fullmatch('(?:[01]\\d|2[0-3]):[0-5]\\d', cfg['time']):
        raise ValueError('执行时间必须为 HH:MM（00:00–23:59）')
    cfg['accounts'] = validate_accounts(cfg['accounts'])
    config.SCHEDULE_JSON.write_text(__import__('json').dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    return cfg

def validate_accounts(accounts: list[str] | None) -> list[str]:
    if accounts is None:
        return []
    if not isinstance(accounts, list):
        raise ValueError('accounts 必须是账号数组')
    return list(dict.fromkeys((config.validate_account_name(a) for a in accounts)))

def build_daily_cmd(accounts: list[str] | None=None) -> list[str]:
    """定时任务统一走编排层：失败自动重试 + 单账号超时 + 机器可读回执。

    ★ 2026-10-21 独立验收 P1-D：入口必须是 `scripts/daily_batch.py`。旧实现走
      `daily_update.py`，它每次直接覆写当日 summary/latest（不按账号合并、不写阶段/
      轮次），一次单账号补跑就能把整轮账号与 `ledger_verify` 失败抹掉 → 面板假绿。
      现在所有产品入口（定时、待接管重跑、自动化）统一走 daily_batch 的回执契约。
    """
    accounts = validate_accounts(accounts)
    cmd = [config.python_exe(), str(config.PIPELINE_DIR / 'scripts' / 'daily_batch.py'), '--retry', str(config.SCHEDULE_RETRY), '--timeout', str(config.SCHEDULE_TIMEOUT)]
    if accounts:
        cmd += ['--accounts', *accounts]
    return cmd

def trigger(accounts: list[str] | None=None, reason: str='手动') -> dict:
    cmd = build_daily_cmd(accounts)
    run = runner.MANAGER.start(kind='daily', title=f'每日增量更新（{reason}）', cmd=cmd, account=','.join(accounts) if accounts else 'all')
    return run.snapshot()

def _tick() -> None:
    if not _tick_lock.acquire(blocking=False):
        return
    try:
        cfg = load()
        if not cfg.get('enabled'):
            return
        now = dt.datetime.now()
        today = now.strftime('%Y-%m-%d')
        hhmm = now.strftime('%H:%M')
        if cfg.get('last_run') == today:
            return
        if hhmm < str(cfg.get('time', '09:30')):
            return
        accounts = cfg.get('accounts') or []
        trigger(accounts, reason=f'定时 {hhmm}')
        cfg['last_run'] = today
        save(cfg)
    finally:
        _tick_lock.release()

def loop(interval: int=30) -> None:
    while True:
        try:
            _tick()
        except Exception:
            pass
        time.sleep(interval)

def start_background() -> threading.Thread:
    t = threading.Thread(target=loop, daemon=True, name='wb-scheduler')
    t.start()
    return t
