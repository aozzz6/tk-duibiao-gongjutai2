"""子进程任务管理器：后台跑 pipeline / daily_batch / 脚本，
逐行捕获 stdout+stderr，支持实时订阅（SSE）与终止。"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import os
import queue
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
import config
import store
MAX_LINES = 4000
_JOB_HANDLE = None
_JOB_TRIED = False
_JOB_LOCK = threading.Lock()
RUN_JOB_PREFIX = 'WB_RUN_'
CREATE_SUSPENDED = 4

def _create_kill_on_close_job(name: str | None=None):
    import ctypes
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount', 'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [('PerProcessUserTimeLimit', wintypes.LARGE_INTEGER), ('PerJobUserTimeLimit', wintypes.LARGE_INTEGER), ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD), ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD), ('SchedulingClass', wintypes.DWORD)]

    class _ExtLimit(ctypes.Structure):
        _fields_ = [('BasicLimitInformation', _BasicLimit), ('IoInfo', _IoCounters), ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t), ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    job = k32.CreateJobObjectW(None, name)
    if not job:
        return None
    info = _ExtLimit()
    KILL_ON_JOB_CLOSE = 8192
    BREAKAWAY_OK = 2048
    info.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        k32.CloseHandle(job)
        return None
    return job

def _job_handle():
    """惰性创建（并缓存）kill-on-close Job；不可用时返回 None。"""
    global _JOB_HANDLE, _JOB_TRIED
    if os.name != 'nt' or os.environ.get('WB_RUNNER_NO_JOB') == '1':
        return None
    with _JOB_LOCK:
        if not _JOB_TRIED:
            _JOB_TRIED = True
            try:
                _JOB_HANDLE = _create_kill_on_close_job()
            except Exception:
                _JOB_HANDLE = None
        return _JOB_HANDLE

def _run_job(run_id: str):
    """为本轮创建命名 Job：返回 (handle, name)。命名失败则退回共享无名 Job。"""
    if os.name != 'nt' or os.environ.get('WB_RUNNER_NO_JOB') == '1':
        return (None, None)
    name = f'{RUN_JOB_PREFIX}{run_id}'
    try:
        job = _create_kill_on_close_job(name)
    except Exception:
        job = None
    if job:
        return (job, name)
    return (_job_handle(), None)

def _assign_to_job(job, proc) -> bool:
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        return bool(k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))))
    except Exception:
        return False

def _resume_process(proc) -> bool:
    """把 CREATE_SUSPENDED 创建的子进程恢复执行（Popen 不暴露主线程句柄，用整进程恢复）。"""
    if os.name != 'nt':
        return True
    try:
        import ctypes
        from ctypes import wintypes
        ntdll = ctypes.WinDLL('ntdll', use_last_error=True)
        ntdll.NtResumeProcess.restype = ctypes.c_long
        ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
        status = ntdll.NtResumeProcess(wintypes.HANDLE(int(proc._handle)))
        return status == 0
    except Exception:
        return False

def _terminate_job(job) -> bool:
    """整棵树终止（比 taskkill /T 更严：Job 里所有后代，含父进程已退出后的遗留孙进程）。"""
    if not job:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.TerminateJobObject.restype = wintypes.BOOL
        k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        return bool(k32.TerminateJobObject(wintypes.HANDLE(job), 1))
    except Exception:
        return False

def _close_handle(handle) -> None:
    if not handle:
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        k32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:
        pass

@dataclass
class Run:
    id: str
    kind: str
    title: str
    cmd: list[str]
    cwd: str
    account: str = ''
    status: str = 'running'
    exit_code: int | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    lines: list[dict] = field(default_factory=list)
    line_count: int = 0
    subscribers: list[queue.Queue] = field(default_factory=list)
    proc: subprocess.Popen | None = None
    job: object = None
    job_name: str | None = None
    job_assigned: bool = False
    state_lock: object = field(default_factory=threading.RLock, repr=False)

    def snapshot(self) -> dict:
        return {'id': self.id, 'kind': self.kind, 'title': self.title, 'cmd': ' '.join(self.cmd), 'cwd': self.cwd, 'account': self.account, 'status': self.status, 'exit_code': self.exit_code, 'started_at': self.started_at, 'ended_at': self.ended_at, 'duration_s': round((self.ended_at or time.time()) - self.started_at, 1), 'line_count': self.line_count, 'tail': [l['text'] for l in self.lines[-20:]]}

class RunManager:

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()
        self._finish_hooks: list = []

    def add_finish_hook(self, fn) -> None:
        """注册终态钩子：每次运行进入终态后调用 fn(run)；单个钩子异常不影响其它钩子。"""
        self._finish_hooks.append(fn)

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def list(self, limit: int=50) -> list[dict]:
        rows = sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)
        return [r.snapshot() for r in rows[:limit]]

    def running(self) -> list[dict]:
        """全部运行中任务 —— 不受历史窗口限制。

        ★ 2026-10-20 验收 P2-2：list(limit=N) 是「先按启动时间取最新 N 条，再过滤
          running」。一条早起的长任务会被后续新增的 done 记录挤出窗口，导致
          overview 把仍在抓取的账号判成 running=false。运行态必须用本方法。
        """
        rows = sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)
        return [r.snapshot() for r in rows if r.status == 'running']

    def all(self) -> list[dict]:
        """本进程内全部任务快照（不再被 limit 截断），/api/runs 合并历史时用。"""
        rows = sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)
        return [r.snapshot() for r in rows]

    def lines(self, run_id: str, since: int=0) -> list[dict]:
        r = self._runs.get(run_id)
        return [line for line in r.lines if line['i'] >= since] if r else []

    def start(self, kind: str, title: str, cmd: list[str], cwd: str | None=None, account: str='') -> Run:
        run = Run(id=uuid.uuid4().hex[:12], kind=kind, title=title, cmd=cmd, cwd=cwd or str(config.PIPELINE_DIR), account=account)
        with self._lock:
            self._runs[run.id] = run
        store.append({'id': run.id, 'kind': kind, 'title': title, 'account': account, 'cmd': ' '.join(cmd), 'started_at': run.started_at, 'status': 'running'})
        threading.Thread(target=self._pump, args=(run,), daemon=True).start()
        return run

    def _pump(self, run: Run) -> None:
        try:
            with run.state_lock:
                if run.status != 'running':
                    return
                flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                if os.name == 'nt':
                    flags |= CREATE_SUSPENDED
                run.proc = subprocess.Popen(run.cmd, cwd=run.cwd, env=config.run_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=flags)
        except Exception as e:
            self._emit(run, f'[启动失败] {e}')
            self._finish(run, 'failed', -1)
            return
        job, job_name = _run_job(run.id)
        run.job, run.job_name = (job, job_name)
        run.job_assigned = bool(job) and _assign_to_job(job, run.proc)
        try:
            store.update(run.id, {'pid': run.proc.pid, 'job_name': job_name, 'job_assigned': run.job_assigned})
        except Exception:
            pass
        if not run.job_assigned:
            self._emit(run, '[warn] 子进程未能加入 Job，进程树不会随服务退出')
        if os.name == 'nt' and (not _resume_process(run.proc)):
            self._emit(run, '[启动失败] 子进程无法从挂起态恢复，已回收')
            self._kill_tree(run)
            run.job_assigned = False
            try:
                if run.proc.stdout is not None:
                    run.proc.stdout.close()
            except Exception:
                pass
            self._finish(run, 'failed', -1)
            return
        assert run.proc.stdout is not None
        with run.proc.stdout:
            for line in run.proc.stdout:
                self._emit(run, line.rstrip('\r\n'))
        code = run.proc.wait()
        self._emit(run, f'[进程退出] exit_code={code}')
        self._finish(run, 'done' if code == 0 else 'failed', code)

    def _kill_tree(self, run: Run) -> None:
        """终止整棵树：先 Job（覆盖父进程已退出的遗留孙进程），再 taskkill /T 兜底。"""
        if run.job and run.job_assigned and _terminate_job(run.job):
            deadline = time.time() + 5
            while time.time() < deadline:
                if run.proc is None or run.proc.poll() is not None:
                    return
                time.sleep(0.1)
        if run.proc is not None and run.proc.poll() is None:
            try:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(run.proc.pid)], capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            except Exception:
                pass

    def _emit(self, run: Run, text: str) -> None:
        item = {'i': run.line_count, 't': round(time.time() - run.started_at, 1), 'text': text}
        run.line_count += 1
        run.lines.append(item)
        if len(run.lines) > MAX_LINES:
            del run.lines[:len(run.lines) - MAX_LINES]
        for q in list(run.subscribers):
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    def _finish(self, run: Run, status: str, code: int) -> None:
        with run.state_lock:
            if run.status != 'running':
                return
            run.status = status
            run.exit_code = code
            run.ended_at = time.time()
        for q in list(run.subscribers):
            try:
                q.put_nowait({'done': True, 'status': status, 'exit_code': code})
            except queue.Full:
                pass
        store.update(run.id, {'status': status, 'exit_code': code, 'ended_at': run.ended_at, 'duration_s': round((run.ended_at or 0) - run.started_at, 1)})
        for fn in list(self._finish_hooks):
            try:
                fn(run)
            except Exception as e:
                try:
                    self._emit(run, f'[钩子异常] {type(e).__name__}: {e}')
                except Exception:
                    pass
        if run.job_name and run.job:
            _close_handle(run.job)
            run.job = None
            run.job_assigned = False

    def stop(self, run_id: str) -> bool:
        run = self._runs.get(run_id)
        if not run:
            return False
        with run.state_lock:
            if run.status != 'running':
                return False
            if run.proc:
                if run.proc.poll() is not None:
                    return False
                self._kill_tree(run)
                if run.proc.poll() is None:
                    self._emit(run, '[终止失败] 进程树未能终止，请重试')
                    return False
            self._emit(run, '[用户终止]')
            self._finish(run, 'stopped', -9)
            return True

    def subscribe(self, run_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=10000)
        run = self._runs.get(run_id)
        if run:
            run.subscribers.append(q)
        return q

    def unsubscribe(self, run_id: str, q: queue.Queue) -> None:
        run = self._runs.get(run_id)
        if run and q in run.subscribers:
            run.subscribers.remove(q)
MANAGER = RunManager()
