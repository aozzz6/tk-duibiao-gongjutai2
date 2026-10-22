import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import json
import subprocess
import sys
from pathlib import Path
PIPE = Path(__file__).resolve().parent.parent
PY = sys.executable
POST = str(PIPE / 'scripts' / 'post_update.py')
PENDING = PIPE / 'output' / 'pending_trans.json'

def run(step: str, args: list[str]) -> tuple[bool, str]:
    proc = subprocess.run([PY, POST, *args], capture_output=True, text=True, encoding='utf-8', errors='replace')
    tail = [l for l in (proc.stdout or '').strip().splitlines() if l.strip()][-3:]
    ok = proc.returncode == 0
    detail = (tail[-1] if tail else '')[:220] or (proc.stderr or '').strip()[-220:]
    print(f"[{step}] rc={proc.returncode} {('OK' if ok else 'FAIL')} | {detail}", flush=True)
    return (ok, detail)

def main() -> int:
    ap = argparse.ArgumentParser(description='台账同步与待译队列生成')
    ap.add_argument('--accounts', nargs='*', default=None, help='只处理指定账号（缺省=accounts.json 中全部 active 账号）')
    ap.add_argument('--no-verify', action='store_true', help='跳过最后一步磁盘终验')
    a = ap.parse_args()
    if a.accounts == []:
        print('[error] --accounts 需要至少一个账号，未执行')
        return 1
    extra = ['--accounts', *a.accounts] if a.accounts else []
    results = {}
    results['write'] = run('write 台账同步', ['write', *extra])
    cfg = json.loads((PIPE / 'accounts.json').read_text(encoding='utf-8'))
    chosen = [x for x in cfg.get('accounts', []) if x.get('active', True) and (a.accounts is None or x.get('account') in a.accounts)]
    image_only = bool(chosen) and all((x.get('media_type') == 'image' for x in chosen))
    if image_only:
        results['export_trans'] = (True, '图文无音频翻译，保留视频待译队列')
    else:
        results['export_trans'] = run('export-trans 导出翻译任务', ['export-trans', *extra])
    if not a.no_verify:
        results['verify'] = run('verify 磁盘终验', ['verify', '--skip-api', *extra])
    pending = {}
    if PENDING.exists():
        try:
            pending = json.loads(PENDING.read_text(encoding='utf-8'))
        except Exception:
            pass
    n_pending = sum((len(v) for v in pending.values() if isinstance(v, list)))
    all_ok = all((ok for ok, _ in results.values()))
    print(json.dumps({'steps': {k: 'ok' if ok else 'fail' for k, (ok, _) in results.items()}, 'pending_trans': n_pending, 'pending_file': str(PENDING), 'next': '在软件的独立任务中执行「中文翻译并回填」' if n_pending else '无待译条目', 'all_ok': all_ok}, ensure_ascii=False))
    return 0 if all_ok else 1
if __name__ == '__main__':
    sys.exit(main())
