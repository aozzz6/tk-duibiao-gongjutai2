import json
import os
import sys
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
import config
import runner
import scheduler

SCRIPTS = config.SCRIPTS_DIR
sys.path.insert(0, str(SCRIPTS))
from ai_client import model_config
from standalone_tasks import ACTIONS, selected

NAMES = {'build': '建立台账表', 'backfill': '回填与校验', 'translate': '中文翻译并回填', 'report': '统计报告', 'analyze': 'AI 内容分析', 'full': '完整整理', 'collect': '增量采集与归档', 'collect_ai': '增量采集、翻译与分析', 'model_test': '模型连接测试'}


def install(app):
    import app as legacy
    replacements = {'/api/translation/backfill', '/api/translation/auto', '/api/automations', '/api/automations/{flow_id}/run', '/api/reverse/rebuild/{vid}', '/api/hot/{action}', '/api/hot/latest'}
    app.router.routes[:] = [route for route in app.router.routes if getattr(route, 'path', '') not in replacements]

    def status():
        try:
            model = model_config()
            configured, name, note = True, model['id'], '模型已配置；连接是否可用请执行连接测试'
        except ValueError as exc:
            configured, name, note = False, '', str(exc)
        try:
            vision=model_config(vision=True)
            vision_name=vision['id']
        except ValueError:
            vision_name=''
        return {'standalone': True, 'ai_configured': configured, 'ai_model': name, 'vision_model':vision_name, 'note': note, 'accounts': config.load_accounts().get('accounts', []), 'actions': NAMES}

    def start(payload: dict):
        action = payload.get('action')
        scope = payload.get('accounts')
        ai = payload.get('ai', False)
        if not isinstance(action, str) or action not in ACTIONS or not isinstance(ai, bool):
            raise HTTPException(400, '无效的任务或 AI 开关')
        if scope is not None and (not isinstance(scope, list) or not scope or any(not isinstance(s, str) for s in scope)):
            raise HTTPException(400, '请选择有效账号，或留空表示全部启用账号')
        try:
            if action != 'model_test':
                for name in scope or []:
                    config.validate_account_name(name)
                cfg, accounts = selected(scope)
                if action not in ('report',) and not accounts:
                    raise ValueError('请先添加至少一个启用的账号')
            if action in ('translate', 'analyze', 'collect_ai', 'model_test') or (action == 'full' and ai):
                model_config()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        with legacy._LEDGER_SYNC_LOCK:
            if runner.MANAGER.running() or legacy._trans_running or legacy._LEDGER_SYNC_ACTIVE:
                raise HTTPException(409, '已有任务正在执行，请完成或停止后再开始')
            command = [config.python_exe(), '-X', 'utf8', str(SCRIPTS / 'standalone_tasks.py'), action]
            if scope:
                command.extend(['--accounts', *list(dict.fromkeys(scope))])
            if ai and action == 'full':
                command.append('--ai')
            return runner.MANAGER.start('standalone', NAMES[action], command, account=','.join(scope) if scope else 'all').snapshot()

    def task_page():
        return FileResponse(config.WEBAPP_DIR / 'static' / 'standalone.html')

    app.add_api_route('/standalone', task_page, methods=['GET'])
    app.add_api_route('/api/standalone/status', status, methods=['GET'])
    app.add_api_route('/api/standalone/run', start, methods=['POST'])

    @app.get('/api/standalone/reports')
    def reports():
        root = config.REPORTS_DIR / 'standalone'
        files = sorted(root.glob('*.html'), key=lambda f: f.stat().st_mtime, reverse=True)[:100]
        return {'items': [{'name': f.name, 'size': f.stat().st_size, 'ai': f.stem.endswith('-analysis')} for f in files]}

    @app.get('/api/standalone/reports/{name}')
    def view_report(name: str):
        if name != Path(name).name or '\\' in name or not name.endswith(('.html', '.json')):
            raise HTTPException(400, '无效的报告名称')
        target = config.REPORTS_DIR / 'standalone' / name
        if not target.is_file():
            raise HTTPException(404, '报告不存在')
        return FileResponse(target, headers={'Cache-Control': 'no-store', 'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'"})

    import tk_reverse
    import reverse_local

    @app.post('/api/reverse/import')
    async def import_video(request: Request, filename: str='local.mp4'):
        import uuid
        if Path(filename).suffix.lower() not in {'.mp4','.mov','.webm','.mkv'}:
            raise HTTPException(400,'仅支持 mp4/mov/webm/mkv 视频')
        cache=Path(os.environ['WB_PROFILE_ROOT'])/'cache'
        temp=cache/('video-import-'+uuid.uuid4().hex+'.part')
        total=0
        try:
            with temp.open('xb') as output:
                async for chunk in request.stream():
                    total+=len(chunk)
                    if total>500*1024*1024:
                        raise HTTPException(413,'本地视频最大500MB')
                    output.write(chunk)
            if total==0:
                raise HTTPException(400,'视频文件为空')
            return reverse_local.import_video(temp,Path(filename).name[:200])
        finally:
            temp.unlink(missing_ok=True)

    @app.post('/api/reverse/refine/{vid}')
    def refine_video(vid: str,payload:dict):
        try:
            return reverse_local.refine(vid,payload.get('instruction'),payload.get('model') or '')
        except tk_reverse.ReverseError as exc:
            raise HTTPException(400,str(exc)) from None

    @app.post('/api/reverse/rebuild/{vid}')
    def rebuild_video(vid: str):
        try:
            return reverse_local.rebuild(vid)
        except tk_reverse.ReverseError as exc:
            raise HTTPException(400,str(exc)) from None

    @app.get('/api/hot/latest')
    def local_monitor_status():
        result=legacy.hot_latest()
        result['cookie_exists']=config.COOKIE_FILE.is_file() and bool(config.COOKIE_FILE.read_text(encoding='utf-8').strip())
        sources=config.read_json(config.PIPELINE_DIR/'hot_monitor_sources.json',{}).get('sources',[])
        result['configured_sources']=sum(1 for item in sources if item.get('enabled',True))
        return result

    @app.post('/api/hot/{action}')
    def local_monitor_action(action: str,payload:dict|None=None):
        if action in {'probe','snapshot'}:
            sources=config.read_json(config.PIPELINE_DIR/'hot_monitor_sources.json',{}).get('sources',[])
            if not any(item.get('enabled',True) for item in sources):
                raise HTTPException(400,'请先在软件设置中配置榜单监控源')
        return legacy.hot_action(action,payload)

    def translation():
        result = start({'action': 'translate'})
        return {**result, 'ok': True, 'triggered': True, 'note': '已开始独立翻译任务，可在独立任务页面查看日志'}
    app.add_api_route('/api/translation/backfill', translation, methods=['POST'])
    app.add_api_route('/api/translation/auto', translation, methods=['POST'])

    flows = [{'id': 'daily_radar', 'name': '增量采集与归档', 'desc': '下载、抓取、转写、台账回填与统计报告', 'schedule': '手动；定时可另行配置', 'accounts': [], 'action': 'collect', 'runnable': True, 'enabled': True}, {'id': 'daily_analysis', 'name': '独立内容分析', 'desc': '依据本机台账调用已配置模型，生成内容分析报告', 'schedule': '手动；需要模型 API', 'accounts': [], 'action': 'analyze', 'runnable': True, 'enabled': True}]

    @app.get('/api/automations')
    def automations():
        return {'items': flows, 'standalone': True}

    @app.post('/api/automations/{flow_id}/run')
    def run_automation(flow_id: str, payload: dict | None = None):
        flow = next((flow for flow in flows if flow['id'] == flow_id), None)
        if not flow:
            raise HTTPException(404, '未知流程')
        data = {'action': flow['action']}
        if (payload or {}).get('accounts'):
            data['accounts'] = payload['accounts']
        return start(data)

    def finished(run):
        if run.kind == 'standalone':
            try:
                legacy._refresh_pending_translation()
                legacy._overview_cache['ts'] = 0
                legacy._ledger_cache['ts'] = 0
            except Exception as exc:
                print('[独立任务] 刷新缓存失败：' + str(exc), flush=True)
    runner.MANAGER.add_finish_hook(finished)

    # Scheduling is local to this application's running backend.
    original_save = scheduler.save
    def save_schedule(cfg):
        if cfg.get('task', 'collect') not in ACTIONS - {'model_test'}:
            raise ValueError('未知的定时任务')
        if cfg.get('enabled') and cfg.get('task') in {'translate', 'analyze', 'collect_ai'}:
            model_config()
        return original_save(cfg)
    scheduler.save = save_schedule
    scheduler.DEFAULT_SCHEDULE['task'] = 'collect'

    def scheduled_command(accounts=None):
        accounts = scheduler.validate_accounts(accounts)
        action = scheduler.load().get('task', 'collect')
        if action not in ACTIONS - {'model_test'}:
            raise ValueError('未知定时任务')
        command = [config.python_exe(), '-X', 'utf8', str(SCRIPTS / 'standalone_tasks.py'), action]
        return command + (['--accounts', *accounts] if accounts else [])
    scheduler.build_daily_cmd = scheduled_command

    def trigger(accounts=None, reason='手动'):
        payload = {'action': scheduler.load().get('task', 'collect')}
        if accounts:
            payload['accounts'] = accounts
        try:
            return start(payload)
        except HTTPException as exc:
            raise ValueError(exc.detail) from None
    scheduler.trigger = trigger
