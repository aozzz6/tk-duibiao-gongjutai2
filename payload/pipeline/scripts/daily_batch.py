"""daily_batch.py — 日更固定编排脚本（替代每日现写的 auto_daily_*.py）

背景（2026-10-20 方案 C 阶段 1）：
  08:00 / 23:59 两条自动化原先由 LLM 每天现写一个一次性脚本
  （pipeline/scripts/auto_daily_YYYYMMDD.py），日期窗口/日志路径全硬编码，
  且从不写 run_summary.json —— 于是 /api/pipeline/status 恒为空队列（stale=true），
  失败账号不会自动重试、面板也不会亮红点。本脚本把这条路固定下来。

2026-10-20 独立验收整改（P1-1/P1-2/P1-3/P2-1）：
  1. 阶段化成功标准：collect（pipeline）/ products / ledger_write / ledger_verify
     各自判 ok，任一段失败即 level=ERROR、exit=1，失败阶段+原因+恢复动作写进
     run_summary.stages / failed_stages / alerts，自动化与面板都能消费。
     ledger.synced 只在 write+verify 都确认成功时为 true，未确认前为 null。
  2. 超时/轮询持续异常一律先 stop 再确认终态；确认不了就停止自动重试、留待接管，
     不再出现「超时后立刻重试 → 同一账号两个 run 并行」。
  3. 每次执行独立 round_id：rounds/{round_id}.json 留档；--accounts 子集补跑按账号
     合并进当日整轮回执，不会把未补跑账号从 total/takeover 里抹掉。
  4. 启动即登记本轮身份（state=running）；前置检查失败与未捕获异常也写终态回执，
     面板能区分「本轮失败」与「上一轮旧 INFO」。

2026-10-21 独立验收整改（P1-2 / P2-2 / P2-3）：
  1. journal 的 interrupted 与接口 404 只算「管理器失联」，不再当作已终止：
     必须用落库 pid 核实旧进程确实退出（_pid_alive），否则停止自动重试、留待接管。
  2. 前置检查失败（本轮一个账号都没跑）写入独立轮次，但当日视图保留已有
     accounts / takeover，只把本轮前置告警并进 stages（persist_summary keep_previous）。
  3. 前置检查通过时显式记 precheck=ok，子集补跑成功不再继承上一轮的 precheck 告警；
     旧错误留在 rounds/ 历史。退出码只代表本轮，回执里用 exit_code/exit_scope 标注，
     与合并视图（level / last_round）区分。

职责（与 daily_update.py 回执字段逐字段对齐，pipeline_ops 无需改动）：
  1. 读 accounts.json，按 active / ledger_skip 过滤（与 daily_update 同口径）
  2. 逐账号串行 POST /api/run（s0/s1/s2/s5 + openai + allow_scrape_errors，与旧路径完全一致）
  3. 每账号最多 --retry 次尝试；单账号墙钟超时 → /api/runs/{id}/stop 强停并确认终态
  4. 商品挂车提取：仅 region in ('', us, uk) 的账号（欧洲账号不挂小黄车，跳过）
  5. 收尾 post_update.py write → verify（--no-verify 可跳过）
  6. 写 reports/{date}/run_summary.json + reports/latest_run_summary.json + rounds/
  7. 日志固定 reports/auto_{date}.log

用法：
  python scripts/daily_batch.py
  python scripts/daily_batch.py --accounts hoshino_sky0     # 子集补跑（结果并入整轮）
  python scripts/daily_batch.py --retry 2 --timeout 5400 --poll 20
  python scripts/daily_batch.py --no-products --no-verify   # 快速验证

退出码（只代表本轮，不代表当日合并视图）：
  0 = 本轮全阶段成功（INFO/WARN）；1 = 本轮有失败阶段/失败账号/前置检查不通过。
  合并视图可能因别的轮次仍为 ERROR —— 自动化请读 rounds[-1]（last_round）里的
  exit_code / level / failed_stages，或顶层的 exit_scope/exit_code。
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
PIPE_DIR = Path(__file__).resolve().parent.parent
TIKTOK_DIR = PIPE_DIR.parent
ACCOUNTS_JSON = PIPE_DIR / 'accounts.json'
WORKBENCH = os.environ['WB_WORKBENCH_URL']
if str(PIPE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPE_DIR))
import receipts as R

def reports_root() -> Path:
    """回执与日志根目录。

    ★ 必须与 webapp/config.py 的 REPORTS_DIR（= PIPELINE_DIR/"reports"）一致，
      也就是 accounts.json 的 reports_root；否则 pipeline_ops.latest_summary() 读不到，
      /api/pipeline/status 仍然 stale=true（2026-10-20 实测踩过：先写到了 tiktok/reports）。
    """
    cfg = _read_json(ACCOUNTS_JSON, {})
    root = os.environ.get('DAILY_BATCH_REPORTS_DIR') or (cfg.get('reports_root') if isinstance(cfg, dict) else None) or str(PIPE_DIR / 'reports')
    return Path(root)
DAILY_STEPS = ['s0_check', 's1_download', 's2_scrape', 's5_transcribe']
PRODUCT_REGIONS = ('', 'us', 'uk')
SUMMARY_NAME = R.SUMMARY_NAME
TERMINAL = ('done', 'failed', 'stopped')
JOURNAL = Path(os.environ.get('WB_RUNS_JSONL', str(PIPE_DIR.parent / 'webapp' / 'data' / 'runs.jsonl')))
JOURNAL_TERMINAL = ('done', 'failed', 'stopped', 'interrupted')
STAGES = R.STAGES
STOP_GRACE = 180
_PIPE_VENV = PIPE_DIR / '.venv' / 'Scripts' / 'python.exe'
PY_MAIN = str(_PIPE_VENV) if _PIPE_VENV.exists() else sys.executable
_SCRAPER = Path(sys.executable)
PY_EXTRACT = str(_SCRAPER) if _SCRAPER.exists() else PY_MAIN

class Log:
    """同时写日志文件与控制台（自动化只看控制台，事后查文件）。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.f = path.open('w', encoding='utf-8')

    def __call__(self, msg: str='') -> None:
        line = str(msg)
        self.f.write(line + '\n')
        self.f.flush()
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode('gbk', 'replace').decode('gbk'), flush=True)

    def close(self) -> None:
        try:
            self.f.close()
        except Exception:
            pass
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def _http(path: str, body: dict | None=None, timeout: float=60) -> dict:
    url = WORKBENCH + path
    if body is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(url, data=json.dumps(body).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='POST')
    with _LOCAL_OPENER.open(req, timeout=timeout) as r:
        raw = r.read().decode('utf-8')
    return json.loads(raw) if raw.strip() else {}

def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default
REPORTS_DIR = reports_root()

def _pid_alive(pid) -> bool | None:
    """旧 run 的子进程是否还活着：True=仍在跑 / False=确认已退出 / None=无法判断。

    ★ 2026-10-21 独立验收 P1-2：重启清扫只能把 journal 标成 interrupted，
      并没有终止、也没有核实原进程。只有这里明确返回 False（进程确实不在了）
      才允许自动重试；None 一律按「未确认」处理，留待接管。

    只做只读查询（OpenProcess + GetExitCodeProcess），不向目标进程发任何信号。
    pid 复用会把「已退出」误判成「还在跑」，方向偏保守，可以接受。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except Exception:
            return None
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        PROCESS_QUERY_LIMITED_INFORMATION = 4096
        STILL_ACTIVE = 259
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            err = ctypes.get_last_error()
            return False if err in (87, 1168) else None
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return None
            return code.value == STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    except Exception:
        return None

def _job_active_processes(job_name) -> int | None:
    """命名 Job 里还有几个活动进程：None = 查不到（旧 journal 无 job_name / Job 已销毁）。

    ★ 2026-10-21 独立验收 P1-R2（重试门禁）：runner 现在「挂起创建 → 入 Job → 恢复」，
      所以同一棵进程树（含子进程后来再创建的孙进程）都在这个命名 Job 里。
      父 pid 已退出但 Job 里仍有活动进程 = 旧树没死透，绝不能再起第二个 run
      —— 这正是验收演练里「父进程死、孙进程活，第二轮照样开跑」的根因。

    只做只读查询（OpenJobObject + QueryInformationJobObject），不向任何进程发信号。
    """
    if not job_name or os.name != 'nt':
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.OpenJobObjectW.restype = wintypes.HANDLE
        k32.OpenJobObjectW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
        k32.QueryInformationJobObject.restype = wintypes.BOOL
        k32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        JOB_OBJECT_QUERY = 4
        job = k32.OpenJobObjectW(JOB_OBJECT_QUERY, False, str(job_name))
        if not job:
            err = ctypes.get_last_error()
            return 0 if err == 2 else None
        try:

            class _BasicAccounting(ctypes.Structure):
                _fields_ = [('TotalUserTime', wintypes.LARGE_INTEGER), ('TotalKernelTime', wintypes.LARGE_INTEGER), ('ThisPeriodTotalUserTime', wintypes.LARGE_INTEGER), ('ThisPeriodTotalKernelTime', wintypes.LARGE_INTEGER), ('TotalPageFaultCount', wintypes.DWORD), ('TotalProcesses', wintypes.DWORD), ('ActiveProcesses', wintypes.DWORD), ('TotalTerminatedProcesses', wintypes.DWORD)]
            info = _BasicAccounting()
            if not k32.QueryInformationJobObject(job, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                return None
            return int(info.ActiveProcesses)
        finally:
            k32.CloseHandle(job)
    except Exception:
        return None

def _legacy_tree_state(rec: dict) -> dict:
    """旧 run 的「整棵树」状态：父 pid + 它那个命名 Job 里的活动进程数。

    `tree_alive=True` 表示父进程可能已退出、但同一棵树的其它进程（孙进程）还活着。
    """
    alive = _pid_alive(rec.get('pid'))
    procs = _job_active_processes(rec.get('job_name'))
    return {'pid': rec.get('pid'), 'pid_alive': alive, 'job_name': rec.get('job_name'), 'job_active': procs, 'tree_alive': bool(procs) if procs is not None else None}

def relaunch_detached() -> int:
    """--detach：以脱离父会话的独立进程重跑自己，父进程立即返回。

    ★ 2026-10-21 事故：自动化/开发会话回收时整组进程被杀，45 账号跑约 2h 的日更
      在 09:31 被腰斩（30/45 完成、无终态回执）。DETACHED_PROCESS 让任务不再属于
      调用方的进程组，会话结束不会连带杀掉它。
    """
    child_argv = [a for a in sys.argv[1:] if a != '--detach']
    log_path = REPORTS_DIR / f'auto_detached_{dt.date.today().isoformat()}.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    creation = 0
    if os.name == 'nt':
        creation = 8 | 512
        creation |= 16777216
    argv = [sys.executable, str(Path(__file__).resolve()), *child_argv]
    with log_path.open('ab') as fh:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, creationflags=creation, close_fds=True, cwd=str(PIPE_DIR.parent))
        except OSError:
            if not creation & 16777216:
                raise
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=fh, stderr=fh, creationflags=creation & ~16777216, close_fds=True, cwd=str(PIPE_DIR.parent))
    print(f'[detach] pid={proc.pid} log={log_path}')
    return 0

def load_accounts(only: list[str] | None=None) -> tuple[list[dict], list[dict]]:
    data = _read_json(ACCOUNTS_JSON, {})
    accs = data if isinstance(data, list) else data.get('accounts') or []
    active = [a for a in accs if a.get('active', True) and (not a.get('ledger_skip', False))]
    if only:
        wanted = {x.strip() for item in only for x in str(item).split(',') if x.strip()}
        active = [a for a in active if a['account'] in wanted]
        unmatched = sorted(wanted - {a['account'] for a in accs})
        if unmatched:
            print(f"[warn] --accounts 中 {len(unmatched)} 个账号不在 accounts.json：{', '.join(unmatched)}")
    return (accs, active)

def terminate_run(run_id: str, poll: int, grace: int=STOP_GRACE, log: Log | None=None) -> dict:
    """stop 旧任务并确认它已进入终态。

    返回 {stop_sent, stop_rc, confirmed, last_status}。
      · confirmed=True  ：已确认终态（done/failed/stopped）或服务端 404（任务不存在）
      · confirmed=False ：stop 失败、或 stop 后仍在 running —— 调用方必须停止自动重试
    """
    log = log or (lambda *a, **k: None)
    stop_sent = False
    stop_rc: int | None = None
    for i in (1, 2):
        try:
            _http(f'/api/runs/{run_id}/stop', {}, timeout=60)
            stop_sent, stop_rc = (True, 0)
            log(f'  stop 已发送 run_id={run_id}（第{i}次）')
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                log(f'  stop 404（管理器已无该 run，无法确认原进程已退出）run_id={run_id}')
                return {'stop_sent': False, 'stop_rc': 404, 'confirmed': False, 'last_status': 'gone'}
            stop_rc = e.code
            log(f'  stop HTTP {e.code}（第{i}次）')
        except Exception as e:
            stop_rc = -1
            log(f'  stop EXC（第{i}次）{e!r}')
        time.sleep(min(3, max(1, poll)))
    deadline = time.monotonic() + max(5, grace)
    last_status = 'unknown'
    while True:
        try:
            s = _http(f'/api/runs/{run_id}', timeout=60)
            last_status = s.get('status') or 'unknown'
            if last_status in TERMINAL:
                log(f'  stop 确认终态 run_id={run_id} status={last_status}')
                return {'stop_sent': stop_sent, 'stop_rc': stop_rc, 'confirmed': True, 'last_status': last_status}
        except urllib.error.HTTPError as e:
            if e.code == 404:
                log(f'  stop 后查询 404（管理器已无该 run），无法确认原进程已退出 run_id={run_id}')
                return {'stop_sent': stop_sent, 'stop_rc': stop_rc, 'confirmed': False, 'last_status': 'gone'}
            last_status = f'HTTP {e.code}'
        except Exception as e:
            last_status = f'EXC {e!r}'
        if time.monotonic() >= deadline:
            log(f'  [warn] stop 后 {grace}s 仍未确认终态 run_id={run_id} last={last_status} → 不再自动重试')
            return {'stop_sent': stop_sent, 'stop_rc': stop_rc, 'confirmed': False, 'last_status': last_status}
        time.sleep(min(max(1, poll), 5))

def run_from_journal(run_id: str) -> dict | None:
    """从 runs.jsonl 取该 run 的终态（服务重启后内存里已没有它）。

    ★ 2026-10-21：另一会话重启 webapp 时 /api/runs/{id} 直接 404，旧实现只会
      一直 poll EXC 直到 timeout，把已经跑完的账号记成失败、或把 abort 写进回执。
      runs.jsonl 是落盘的，这里只认 TERMINAL/JOURNAL_TERMINAL 里的终态。
    """
    if not JOURNAL.exists():
        return None
    truth: dict | None = None
    try:
        with JOURNAL.open(encoding='utf-8', errors='replace') as f:
            for line in f:
                if run_id not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get('id') != run_id:
                    continue
                if d.get('status') in JOURNAL_TERMINAL:
                    truth = d
    except OSError:
        return None
    return truth

def wait_run(run_id: str, poll: int, timeout: int, log: Log, stop_grace: int=STOP_GRACE) -> dict:
    """轮询到终态；超时（含轮询持续异常导致超时）先 stop 并确认，再判 failed。

    ★ 2026-10-20 验收 P1-2：旧实现在「GET 持续异常 + 超时」分支直接 return failed，
      从不调用 stop，外层立刻 POST 下一个 run → 同一账号两个 run 并行抓取/转写。
      现在所有超时路径都走 terminate_run，并用单调时钟算墙钟上限。
    """
    limit = max(1, int(timeout))
    deadline = time.monotonic() + limit
    snap: dict = {}
    last_exc: Exception | None = None
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(max(1, poll), max(0.05, left)))
        try:
            snap = _http(f'/api/runs/{run_id}', timeout=60)
            last_exc = None
        except Exception as e:
            last_exc = e
            log(f'  poll EXC {e!r}')
            j = run_from_journal(run_id)
            if j:
                tree = _legacy_tree_state(j)
                alive = tree['pid_alive']
                log(f"  旧 run 树状态 pid={tree['pid']} alive={alive} job={tree['job_name']} job_active={tree['job_active']}")
                if j.get('status') == 'interrupted' and alive is not False:
                    log(f"  run {run_id} 在 journal 里是 interrupted，旧进程状态未确认（pid={j.get('pid')} alive={alive}）→ 不自动重试")
                    return {'status': 'failed', 'run_id': run_id, 'journal': True, 'unverified': True, 'pid_alive': alive, 'job_name': tree['job_name'], 'job_active': tree['job_active'], 'abort_retry': True, 'abort_reason': '服务重启后该 run 只剩 journal 的 interrupted 推断，无法确认原进程已退出，已停止自动重试（留待接管）', 'tail': [f"journal interrupted: pid={j.get('pid')} alive={alive}"]}
                if tree['job_active']:
                    log(f"  run {run_id} 的父进程已退出，但 Job {tree['job_name']} 里仍有 {tree['job_active']} 个活动进程 → 不自动重试，留待接管")
                    return {'status': 'failed', 'run_id': run_id, 'journal': True, 'pid_alive': alive, 'job_name': tree['job_name'], 'job_active': tree['job_active'], 'abort_retry': True, 'abort_reason': f"旧进程树未完全退出（{tree['job_name']} 里仍有 {tree['job_active']} 个活动进程），已停止自动重试（留待接管）", 'tail': [f"tree alive: job={tree['job_name']} active={tree['job_active']} pid={tree['pid']}"]}
                log(f"  服务端已无该 run（疑似重启），按 runs.jsonl 终态处理：status={j.get('status')} exit={j.get('exit_code')} pid_alive={alive} job_active={tree['job_active']}")
                return {**j, 'journal': True, 'pid_alive': alive, 'job_name': tree['job_name'], 'job_active': tree['job_active']}
            continue
        if snap.get('status') in TERMINAL:
            return snap
    reason = f'timeout {limit}s' if last_exc is None else f'poll failed: {last_exc!r} (after {limit}s)'
    log(f'  单账号超时/轮询失败（{reason}），主动终止 run_id={run_id}')
    t = terminate_run(run_id, poll, stop_grace, log)
    out = {**(snap or {}), 'status': 'failed', 'run_id': run_id, 'stopped': t['stop_sent'], 'stop_confirmed': t['confirmed'], 'tail': [f'{reason} (killed by daily_batch)']}
    if not t['confirmed']:
        out['abort_retry'] = True
        out['abort_reason'] = f"旧任务未确认终止（stop_sent={t['stop_sent']} rc={t['stop_rc']} last={t['last_status']}），已停止自动重试"
    return out

def run_account(acc: dict, date_from: str, date_to: str, retry: int, timeout: int, poll: int, log: Log, stop_grace: int=STOP_GRACE) -> dict:
    name = acc['account']
    is_image = acc.get('media_type') == 'image'
    body = {'kind': 'pipeline', 'account': name, 'date_from': date_from, 'date_to': date_to, 'steps': list(DAILY_STEPS), 'lang': acc.get('lang') or 'en', 'backend': 'openai', 'allow_scrape_errors': True, 'no_resume': True}
    attempts = 0
    snap: dict = {}
    run_ids: list[str] = []
    stops: list[dict] = []
    aborted = False
    for attempt in range(1, max(1, retry) + 1):
        attempts = attempt
        try:
            r = _http('/api/run', body, timeout=120)
            rid = r.get('id') or r.get('run_id') or r.get('runId')
            if not rid:
                log(f'{name} attempt{attempt} POST 无 id: {json.dumps(r, ensure_ascii=False)[:200]}')
                snap = {'status': 'failed', 'tail': ['POST /api/run 未返回 id']}
                break
            run_ids.append(rid)
            log(f'{name} attempt{attempt} run_id={rid}')
            snap = wait_run(rid, poll, timeout, log, stop_grace)
            if snap.get('stop_confirmed') is not None:
                stops.append({'run_id': rid, 'stopped': snap.get('stopped'), 'confirmed': snap.get('stop_confirmed')})
            log(f"{name} attempt{attempt} status={snap.get('status')} exit={snap.get('exit_code')} duration={snap.get('duration_s')}s" + (f" stopped={snap.get('stopped')} confirmed={snap.get('stop_confirmed')}" if snap.get('stopped') is not None else ''))
        except Exception as e:
            log(f'{name} attempt{attempt} EXC {e!r}')
            snap = {'status': 'failed', 'tail': [repr(e)]}
        if snap.get('abort_retry'):
            aborted = True
            log(f"{name} {snap.get('abort_reason')}")
            break
        if snap.get('status') == 'done':
            break
        if attempt < retry:
            log(f'{name} 第 {attempt} 次失败，重试中…')
    failed = snap.get('status') != 'done'
    scrape = _read_json(Path(acc.get('out_dir') or PIPE_DIR / 'output') / f'scrape_{name}.json', {})
    items = [i for i in scrape.get('items') or [] if i.get('id')]
    errors = scrape.get('errors') or []
    anomalies = []
    if failed:
        tail = [str(x) for x in snap.get('tail') or []][-3:]
        reason = f"pipeline {snap.get('status')} exit={snap.get('exit_code')}"
        if snap.get('abort_retry'):
            reason += '；' + str(snap.get('abort_reason') or '旧任务未确认终止')
        anomalies = [reason] + tail
    skipped_all = False
    dur = snap.get('duration_s')
    if not failed and isinstance(dur, (int, float)) and (dur < 2):
        skipped_all = True
        anomalies = [f'疑似全部步骤被断点续跑跳过（duration={dur}s），检查 no_resume']
        log(f'[warn] {name} {anomalies[0]}')
    status = 'failed' if failed else 'warn' if errors or skipped_all else 'ok'
    return {'status': status, 'media_type': 'image' if is_image else 'video', 'region': acc.get('region') or '', 'exit_code': snap.get('exit_code'), 'attempts': attempts, 'duration_s': snap.get('duration_s'), 'new': None, 'scrape_items': len(items), 'scrape_errors': len(errors), 'coverage': f'{len(items)} 条', 'gates': not failed, 'anomalies': anomalies, 'run_ids': run_ids, 'stopped': bool(stops), 'stop_confirmed': all((x.get('confirmed') for x in stops)) if stops else None, 'stops': stops, 'aborted_retry': aborted, 'stages': {'collect': 'ok' if not failed else 'failed', 'products': 'skipped', 'ledger': 'pending'}, 'ledger': {'synced': None if not failed else False, 'payload': ''}}

def extract_products(accs: list[dict], log: Log) -> dict:
    """商品挂车提取兜底：仅 us/uk/未标注 地区，且只补「有缺口」的账号。

    ★ 现行 /api/run 在 steps 含 s2/s5 时会自动追加 s5b_products（app.py:330-335），
      所以正常情况下这里全部跳过；本层只补 s5b 没覆盖到的历史漏跑与失败账号。
    """
    out: dict[str, dict] = {}
    for a in accs:
        if a.get('media_type') == 'image':
            continue
        if (a.get('region') or '') not in PRODUCT_REGIONS:
            continue
        name = a['account']
        out_dir = Path(a.get('out_dir') or PIPE_DIR / 'output')
        scrape = out_dir / f'scrape_{name}.json'
        aux = Path(os.environ.__getitem__('WB_LEDGER_DIR')) / name
        target = aux / f'{name}_products.json'
        ids = [str(i['id']) for i in _read_json(scrape, {}).get('items') or [] if i.get('id')]
        have = _read_json(target, {})
        if not isinstance(have, dict):
            have = {}
        missing = [v for v in ids if not isinstance(have.get(v), list)]
        if not missing:
            log(f'PROD {name} 覆盖完整（{len(ids)} 条），跳过')
            out[name] = {'rc': 0, 'skipped': True}
            continue
        log(f'PROD {name} 缺口 {len(missing)}/{len(ids)} 条，补齐中…')
        cmd = [PY_EXTRACT, str(PIPE_DIR / 'scripts' / 'extract_products.py'), '--account', name, '--scrape', str(scrape), '--out', str(target)]
        try:
            p = subprocess.run(cmd, cwd=str(PIPE_DIR), capture_output=True, timeout=900)
            tail = (p.stdout or b'')[-400:].decode('utf-8', 'replace').strip().splitlines()
            log(f"PROD {name} rc={p.returncode} {(tail[-1] if tail else '')}")
            out[name] = {'rc': p.returncode, 'missing_before': len(missing)}
        except Exception as e:
            log(f'PROD {name} EXC {e!r}')
            out[name] = {'rc': -999, 'err': repr(e)}
    return out

def post_update(sub: str, log: Log, timeout: int=3600) -> dict:
    cmd = [PY_MAIN, str(PIPE_DIR / 'scripts' / 'post_update.py'), sub]
    try:
        p = subprocess.run(cmd, cwd=str(PIPE_DIR), capture_output=True, timeout=timeout)
        text = ((p.stdout or b'') + (p.stderr or b'')).decode('utf-8', 'replace')
        return {'rc': p.returncode, 'out': text}
    except Exception as e:
        log(f'post_update {sub} EXC {e!r}')
        return {'rc': -999, 'out': repr(e)}
_recompute = R.recompute
_merge_stages = R.merge_stages
_round_meta = R.round_meta
write_round_record = R.write_round_record
RECOVERY_ACTION = R.RECOVERY_ACTION
_stage = R.stage

def apply_stage_results(results: dict, products: dict, write_rc: int | None, verify_rc: int | None, verify_skipped: bool, log: Log) -> dict:
    """把商品/台账阶段的结论落到逐账号结果与阶段表上（P1-1）。"""
    prod_failed: list[str] = []
    for name, p in (products or {}).items():
        r = results.get(name)
        if not r:
            continue
        rc = p.get('rc')
        r['products_rc'] = rc
        if p.get('skipped'):
            r['stages']['products'] = 'skipped'
            continue
        if rc not in (0, None):
            prod_failed.append(name)
            r['stages']['products'] = 'failed'
            r['status'] = 'failed'
            r['anomalies'] = list(r.get('anomalies') or []) + [f'商品提取失败 rc={rc}']
            r['ledger'] = {'synced': False, 'payload': ''}
            log(f'[fail] {name} 商品提取 rc={rc} → 该账号判 failed')
        else:
            r['stages']['products'] = 'ok'
    ledger_ok = write_rc in (None, 0) and verify_rc in (None, 0)
    ledger_skipped = verify_skipped
    for name, r in results.items():
        if ledger_skipped:
            r['stages']['ledger'] = 'skipped'
            r['ledger'] = {'synced': None, 'payload': ''}
            continue
        if not ledger_ok:
            r['stages']['ledger'] = 'failed'
            r['ledger'] = {'synced': False, 'payload': ''}
            if r['status'] != 'failed':
                r['status'] = 'warn'
                r['anomalies'] = list(r.get('anomalies') or []) + ['台账未确认入账']
        elif r['status'] != 'failed':
            r['stages']['ledger'] = 'ok'
            r['ledger'] = {'synced': True, 'payload': ''}
    collect_failed = [n for n, r in results.items() if r['stages'].get('collect') == 'failed']
    stages = {'collect': _stage('collect', not collect_failed, skipped=not results, reason='; '.join(collect_failed) if collect_failed else '', failed=collect_failed), 'products': _stage('products', not prod_failed, skipped=not products, reason='; '.join(prod_failed) if prod_failed else '', failed=prod_failed, rc={n: p.get('rc') for n, p in (products or {}).items()}), 'ledger_write': _stage('ledger_write', write_rc in (None, 0) or ledger_skipped, rc=write_rc, skipped=ledger_skipped, reason='' if write_rc in (None, 0) else f'write rc={write_rc}'), 'ledger_verify': _stage('ledger_verify', verify_rc in (None, 0) or ledger_skipped, rc=verify_rc, skipped=ledger_skipped, reason='' if verify_rc in (None, 0) else f'verify rc={verify_rc}')}
    return stages

def _merge_stages(prev: dict | None, new: dict | None) -> dict:
    """子集补跑时合并阶段结论：本轮跳过的阶段保留上一轮的结论。"""
    out = dict(prev or {})
    for k, v in (new or {}).items():
        if isinstance(v, dict) and v.get('skipped') and (k in out):
            continue
        out[k] = v
    return out

def build_summary(accounts: list[dict], results: dict, date: str, started_at: float, round_id: str, kind: str, stages: dict) -> dict:
    return _recompute({'date': date, 'state': 'done', 'round_id': round_id, 'round_kind': kind, 'source': 'daily_batch', 'started_at': started_at, 'ended_at': time.time(), 'duration_s': round(time.time() - started_at, 1), 'total': len(results), 'counts': {}, 'accounts': results, 'stages': stages, 'takeover': [], 'alerts': []})

def _round_meta(summary: dict, ran: list[str]) -> dict:
    return {'round_id': summary.get('round_id'), 'round_kind': summary.get('round_kind') or 'full', 'accounts_run': ran, 'started_at': summary.get('started_at'), 'ended_at': summary.get('ended_at'), 'counts': summary.get('counts'), 'failed_stages': summary.get('failed_stages'), 'level': summary.get('level'), 'exit_code': summary.get('exit_code'), 'exit_scope': summary.get('exit_scope') or 'round'}

def write_round_record(round_path: Path, payload: dict) -> None:
    round_path.parent.mkdir(parents=True, exist_ok=True)
    round_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')

def persist_summary(summary: dict, reports_dir: Path, date: str, partial: bool, ran: list[str], log: Log, keep_previous: bool=False) -> dict:
    """写「当日合并回执 + latest 指针」（实现在 pipeline/receipts.persist）。

    ★ P1-3：子集补跑（--accounts）按账号合并进当日整轮，未补跑账号不消失；
      整轮（全量）才替换 accounts。所有轮次都写进 rounds[] 与本轮 round 文件。

    ★ 2026-10-21 独立验收 P2-2：keep_previous=True 用于「本轮一个账号都没跑到」
      （前置检查失败/未捕获异常）。这时本轮没有账号结论，当日视图必须保留已有
      accounts 与 takeover，只把本轮的前置告警并进 stages；本轮自己的独立留档
      （rounds/{round_id}.json）仍然是空账号 + precheck 失败，不会混淆。

    ★ 2026-10-21 独立验收 P1-D：与 daily_update 共用同一实现，任何入口补跑都不会
      再覆写掉未跑账号与全局阶段失败。
    """
    return R.persist(summary, reports_dir / date, reports_dir, date, partial, ran, log=log, keep_previous=keep_previous)

def main() -> int:
    ap = argparse.ArgumentParser(description='日更固定编排（回执 + 重试 + 超时 + 终验）')
    ap.add_argument('--detach', action='store_true', help='脱离父会话后台运行（父进程立即返回，适合自动化步骤）')
    ap.add_argument('--accounts', nargs='*', default=None, help='只跑指定账号（子集补跑，结果并入当日整轮）')
    ap.add_argument('--date', default=None, help='运行日期（默认今天）')
    ap.add_argument('--days', type=int, default=7, help='抓取窗口回看天数（默认 7）')
    ap.add_argument('--retry', type=int, default=2, help='每账号最多尝试次数（默认 2）')
    ap.add_argument('--timeout', type=int, default=5400, help='单账号墙钟上限秒')
    ap.add_argument('--poll', type=int, default=20, help='轮询间隔秒')
    ap.add_argument('--stop-grace', type=int, default=STOP_GRACE, help='stop 后等待终态确认秒数')
    ap.add_argument('--no-products', action='store_true', help='跳过商品挂车提取')
    ap.add_argument('--no-verify', action='store_true', help='跳过写台账与终验')
    a = ap.parse_args()
    if a.detach:
        return relaunch_detached()
    started_at = time.time()
    today = a.date or dt.date.today().isoformat()
    date_from = (dt.date.fromisoformat(today) - dt.timedelta(days=a.days)).isoformat()
    round_id = f"{dt.datetime.fromtimestamp(started_at).strftime('%Y%m%dT%H%M%S')}_{int(started_at * 1000) % 1000:03d}_{os.getpid()}"
    kind = 'partial' if a.accounts else 'full'
    report_dir = REPORTS_DIR / today
    log = Log(REPORTS_DIR / f'auto_{today}.log')
    log(f'daily_batch 开始 date={today} round={round_id} kind={kind} 窗口={date_from}~{today} retry={a.retry} timeout={a.timeout}s poll={a.poll}s')
    round_path = report_dir / 'rounds' / f'{round_id}.json'
    write_round_record(round_path, {'date': today, 'round_id': round_id, 'round_kind': kind, 'state': 'running', 'started_at': started_at, 'pid': os.getpid(), 'argv': sys.argv[1:], 'accounts_requested': a.accounts or 'all', 'source': 'daily_batch'})

    def finish_early(reason: str, stage: str='precheck', extra: dict | None=None, recovery: str | None=None) -> int:
        """前置失败 / 未捕获异常：必须留下终态回执（旧版直接 return 1，旧 INFO 仍是当前有效）。"""
        if recovery:
            rec = recovery
        elif '--accounts' in reason or '无可跑账号' in reason:
            rec = '检查 --accounts 拼写、accounts.json 的 active/ledger_skip'
        else:
            rec = RECOVERY_ACTION.get(stage, '排查后重跑')
        summary = _recompute({'date': today, 'state': 'failed', 'round_id': round_id, 'round_kind': kind, 'source': 'daily_batch', 'started_at': started_at, 'ended_at': time.time(), 'duration_s': round(time.time() - started_at, 1), 'total': 0, 'counts': {}, 'accounts': {}, 'takeover': [], 'alerts': [], 'stages': {stage: _stage(stage, False, reason=reason, recovery=rec)}, 'exit_code': 1, 'exit_scope': 'round', **(extra or {})})
        persist_summary(summary, REPORTS_DIR, today, False, [], log, keep_previous=True)
        log(f'[fatal] {reason} → 已写终态回执 level=ERROR stage={stage}')
        log.close()
        return 1
    try:
        try:
            _http('/api/health', timeout=10)
        except Exception as e:
            log(f'[fatal] 工作台不可达 {WORKBENCH}：{e!r}（先启动 webapp/app.py）')
            return finish_early(f'工作台不可达 {WORKBENCH}：{e!r}')
        all_accs, todo = load_accounts(a.accounts)
        skipped = [x['account'] for x in all_accs if not x.get('active', True) or x.get('ledger_skip')]
        log(f'total={len(all_accs)} 本轮={len(todo)} 跳过停用={len(skipped)}' + (f"（{', '.join(skipped)}）" if skipped else ''))
        if not todo:
            if a.accounts:
                n = len({x.strip() for item in a.accounts for x in str(item).split(',') if x.strip()})
                reason = f'--accounts 指定的 {n} 个账号均未匹配 active 列表（检查拼写、active=false 或 ledger_skip）'
            else:
                reason = '无可跑账号（检查 accounts.json 的 active/ledger_skip）'
            log(f'[fatal] {reason}')
            return finish_early(reason)
        results: dict[str, dict] = {}
        for i, acc in enumerate(todo, 1):
            name = acc['account']
            log(f"--- [{i}/{len(todo)}] {name} ({acc.get('media_type') or 'video'}) ---")
            results[name] = run_account(acc, date_from, today, a.retry, a.timeout, a.poll, log, a.stop_grace)
            log(f"RESULT {name} {results[name]['status'].upper()}")
        products: dict[str, dict] = {}
        if not a.no_products:
            products = extract_products(todo, log)
        write_rc = verify_rc = None
        if not a.no_verify:
            w = post_update('write', log)
            write_rc = w['rc']
            log(f'post_update write rc={write_rc}')
            if write_rc != 0:
                log(w['out'][-800:])
            v = post_update('verify', log)
            verify_rc = v['rc']
            log(f'post_update verify rc={verify_rc}')
            log(v['out'][-2500:])
        stages = apply_stage_results(results, products, write_rc, verify_rc, a.no_verify, log)
        stages['precheck'] = _stage('precheck', True)
        summary = build_summary(todo, results, today, started_at, round_id, kind, stages)
        summary['products'] = {n: p.get('rc') for n, p in products.items()}
        summary['post_update'] = {'write_rc': write_rc, 'verify_rc': verify_rc}
        round_rc = 1 if summary['failed_stages'] or summary['counts'].get('failed') else 0
        summary['exit_code'] = round_rc
        summary['exit_scope'] = 'round'
        persist_summary(summary, REPORTS_DIR, today, kind == 'partial', [x['account'] for x in todo], log)
        log(f"日更完成 round={round_id} level={summary['level']} ok={summary['counts'].get('ok', 0)} warn={summary['counts'].get('warn', 0)} failed={summary['counts'].get('failed', 0)} failed_stages={summary['failed_stages'] or '无'}")
        if summary['takeover']:
            log('待接管: ' + ', '.join((x['account'] for x in summary['takeover'])))
        for al in summary['alerts']:
            log(f"[alert] {al['stage']}: {al['reason']} → {al['recovery']}")
        log('ALL DONE')
        log.close()
        return round_rc
    except SystemExit:
        raise
    except BaseException as e:
        tb = traceback.format_exc(limit=3)
        log(f'[fatal] 未捕获异常 {e!r}\n{tb}')
        return finish_early(f'未捕获异常 {e.__class__.__name__}: {e}', stage='internal')
if __name__ == '__main__':
    sys.exit(main())
