"""Desktop-only backend entry point, with a private per-launch session token."""
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import openpyxl
import uvicorn
from fastapi.responses import JSONResponse

import config


def initialize_ledgers():
    cfg = config.load_accounts_raw()
    for key in ("xlsx", "img_xlsx"):
        target = Path(cfg[key])
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            book = openpyxl.Workbook()
            book.active.title = "说明"
            book.active.append(["新工作台：请先在账号管理中添加账号，再执行采集与台账同步。"])
            book.save(target)


initialize_ledgers()
from app import app
import runner
import scheduler
import store
from standalone_backend import install

install(app)

TOKEN = os.environ["WB_DESKTOP_TOKEN"]
_desktop_job_owned=False


@app.middleware("http")
async def desktop_session(request, call_next):
    if not secrets.compare_digest(request.headers.get("x-workbench-token", ""), TOKEN):
        return JSONResponse({"detail": "仅允许工作台桌面程序访问"}, status_code=403)
    return await call_next(request)


@app.get("/desktop/health")
def desktop_health():
    return {"ok": True, "instance": TOKEN, 'job_owned':_desktop_job_owned, 'pid':os.getpid()}


@app.get("/desktop/running")
def desktop_running():
    import app as legacy
    rows = runner.MANAGER.running()
    if legacy._trans_running:
        rows.append({'id': 'automatic-translation', 'kind': 'translation', 'status': 'running'})
    import tk_reverse
    with tk_reverse._LOCK:
        rows.extend({'id':'reverse-'+vid,'kind':'reverse','status':'running'} for vid,(_,worker) in tk_reverse._WORKERS.items() if worker.is_alive())
    return rows


if __name__ == "__main__":
    # Own the whole backend process tree, including reverse-video FFmpeg helpers.
    # Retain this handle until process exit; Windows then closes it and its children.
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        from types import SimpleNamespace
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetCurrentProcess.restype=wintypes.HANDLE
        _desktop_job=runner._create_kill_on_close_job()
        if _desktop_job and runner._assign_to_job(_desktop_job,SimpleNamespace(_handle=kernel.GetCurrentProcess())):
            _desktop_job_owned=True
            print('[desktop] Backend process tree is owned by a kill-on-close Job',flush=True)
        else:
            print('[desktop] Backend Job unavailable; launcher retains process-handle cleanup',flush=True)
        parent_id=int(os.environ.get('WB_DESKTOP_PARENT_PID','0'))
        if parent_id:
            kernel.OpenProcess.restype=wintypes.HANDLE
            kernel.OpenProcess.argtypes=(wintypes.DWORD,wintypes.BOOL,wintypes.DWORD)
            parent_handle=kernel.OpenProcess(0x00100000,False,parent_id)
            if parent_handle:
                import threading
                kernel.WaitForSingleObject.argtypes=(wintypes.HANDLE,wintypes.DWORD)
                def _parent_watch():
                    if kernel.WaitForSingleObject(parent_handle,0xFFFFFFFF)==0:
                        os._exit(0)
                threading.Thread(target=_parent_watch,daemon=True,name='desktop-parent-watch').start()
    store.sweep_stale_running()
    scheduler.start_background()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ["WB_DESKTOP_PORT"]), log_level="info")
