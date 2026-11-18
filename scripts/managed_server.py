"""One managed Waitress instance, one data lock, no implicit live authorization."""
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'backend'))
from services.runtime_config import load, no_links, authorized_now


class DataLock:
    def __init__(self, root): self.root=Path(root); self.file=None
    def __enter__(self):
        no_links(self.root); self.root.mkdir(parents=True,exist_ok=True)
        path=self.root/'service.lock';no_links(path)
        self.file=path.open('a+b')
        if self.file.seek(0,2)==0: self.file.write(b'0');self.file.flush()
        self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();self.file=None
            raise RuntimeError('同一数据目录已被服务或维护操作锁定。') from None
        return self
    def __exit__(self,*_):
        if self.file:
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
            self.file.close()


def select_paths(args, runner):
    cfg=load(args.runtime_config,runner.ROOT,enforce_authorization=False)
    from services.admin_runtime import effective
    cfg=effective(cfg,runner.ROOT)
    if any((args.enable_models,args.budget_credits,args.batch_id,args.trial_visitor,args.enable_region_edit,
            args.confirm_template_retry_call,args.confirm_final_template_retry_call)):
        raise ValueError('常驻配置不能与历史验收授权参数混用。')
    runner.RUNTIME=Path(cfg['data_dir']);runner.CONFIG=Path(cfg['secret_file']);runner.PID_FILE=runner.RUNTIME/'run/server.json'
    args.managed_config=cfg


def inspect(args, runner):
    cfg=args.managed_config;auth=cfg['authorization']; values=runner.config_values()
    ledger=runner.RUNTIME/'run/call-budget.json';state=None;issues=[]
    try:
        state=json.loads(ledger.read_text()) if ledger.exists() else None
        if state is None:issues.append('账本尚未初始化；仅新空实例可初始化')
        elif state.get('paused'):issues.append('历史失败/未知结果需核对')
    except (ValueError,OSError):issues.append('账本不可读，拒绝收费执行')
    if cfg['mode']=='preview':issues.append('preview：模型执行关闭')
    if cfg.get('_admin_paused'):issues.append('管理员已暂停新的模型发送')
    if not auth:issues.append('没有生效的运行授权/额度')
    elif not authorized_now(auth):issues.append('运行授权未生效或已过期；仍可查看状态、停止或备份')
    if any(values.get(k)!=v for k,v in runner.PRESET.items()):issues.append('模型/协议配置与受管预设不符')
    if any(not values.get(r+'_API_KEY') for r in ('TEXT','IMAGE')):issues.append('模型凭据未齐全')
    return {'source':cfg['_source'],'profile':cfg['profile'],'mode':cfg['mode'],'data_dir':cfg['data_dir'],
        'secret_file':cfg['secret_file'],'key_present':{r:bool(values.get(r+'_API_KEY')) for r in ('TEXT','IMAGE')},
        'server':'waitress','bind':f'127.0.0.1:{cfg["port"]}','http_threads':cfg['http_threads'],'model_concurrency':1,
        'authorization_id':auth['id'] if auth else None,'allowed_roles':auth['roles'] if auth else [],
        'allowed_operations':auth['operations'] if auth else [],'budget_limit':auth['credits'] if auth else 0,
        'ledger':{k:state.get(k) for k in ('text','image','reserved_credits','paused')} if isinstance(state,dict) else None,
        'resources':cfg['resources'],'unmet':issues,'admin_paused':bool(cfg.get('_admin_paused')),
        'admin_config_revision':cfg.get('_admin_revision',0),'model_probe_performed':False}


def active_tasks(app):
    from models import db,Task
    with app.app_context():
        return db.session.query(Task).filter(Task.status.in_(['PENDING','PROCESSING','RUNNING','QUEUED'])).count()


def recover_interrupted(app, root):
    """Crash evidence is retained; never resume or clear an unknown cost."""
    from models import db,Task,VisitorRequest
    from services.ai_providers import copilot_limits as budget
    with app.app_context():
        rows=db.session.query(Task).filter(Task.status.in_(['PENDING','PROCESSING','RUNNING','QUEUED'])).all()
        admissions=db.session.query(VisitorRequest).filter(VisitorRequest.task_id.is_(None),
            VisitorRequest.state.in_(['pending','accepted','unknown'])).all()
        if rows or admissions or (root/'run/interrupted.json').exists():
            with budget._lock:
                state=budget.budget_state();state['paused']=True
                budget._save(root/'run/call-budget.json',state)
            for row in rows:
                row.status='INTERRUPTED';row.error_message='服务中断，结果与费用需核对；不会自动重新执行。'
            for row in admissions:row.state='interrupted'  # Keep key/fingerprint; never replay it.
            db.session.commit()


def boundaries(app, cfg):
    from flask import request
    from sqlalchemy import text
    from models import db
    from urllib.parse import urlsplit
    expected=urlsplit(cfg['public_origin']).netloc
    def boundary():
        health=request.path=='/health' and request.host==f'127.0.0.1:{cfg["port"]}'
        if request.host!=expected and not health: return {'error':{'message':'访问地址无效。'}},400
        if cfg['proxy'] and not request.is_secure and not health: return {'error':{'message':'请使用HTTPS访问。'}},400
        if os.getenv('PC_DRAINING')=='1' and (request.method not in ('GET','HEAD','OPTIONS') or '/export/' in request.path):
            return {'error':{'message':'服务正在停止接收任务，请稍后再试。'}},503
    app.before_request_funcs.setdefault(None,[]).insert(0,boundary)
    app.config.update(DEBUG=False,TESTING=False,PC_PUBLIC_HTTPS=cfg['proxy'])
    def health():
        db.session.execute(text('select 1'))
        return {'status':'ok','mode':os.getenv('PC_MODE'),'accepting_tasks':os.getenv('PC_DRAINING')!='1'}
    app.view_functions['health_check']=health


def initialize_live_runtime(app):
    """Keep intentional send pauses separate from unsafe ledger initialization."""
    from services.ai_providers.copilot_limits import initialize_runtime, RuntimeGenerationPaused
    try:
        if os.getenv('PC_RUNTIME_INCONSISTENT') == '1':
            raise RuntimeError('未完成配置写入')
        initialize_runtime(app)
    except RuntimeGenerationPaused:
        logging.warning('历史调用或单次许可保持暂停；账目一致，可在后台校验新的余额配置。')
    except Exception:
        os.environ['PC_RUNTIME_INCONSISTENT'] = '1'
        logging.error('收费执行保持关闭：运行账目或配置一致性检查未通过。')


def serve(args, runner):
    import psutil
    from waitress import create_server
    cfg=args.managed_config;root=runner.RUNTIME
    with DataLock(root/'run'):
        if runner.recorded_process(): raise RuntimeError('已有归属记录中的个人服务，拒绝重复启动。')
        res=runner.resource_snapshot();limits=cfg['resources']
        if res['available_memory_gib']<limits['start_memory_gib'] or res['project_disk_free_gib']<limits['disk_gib'] or res['cpu_percent_1s']>limits['start_cpu_percent']:
            raise RuntimeError('资源不足，暂停本项目。')
        # Never select another port behind a fixed reverse proxy.
        import socket
        with socket.socket() as probe:
            if os.name=='nt':probe.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
            probe.bind(('127.0.0.1',cfg['port']))
        live=cfg['mode']=='live';auth=cfg['authorization']
        runner.prepare_environment(live,auth['credits'] if auth else 0,
            image_operations=auth['operations'] if auth else ['text_generate'],
            model_roles=auth['roles'] if auth else ['text','image'],
            text_total_timeout=cfg['text_timeout_seconds'],runtime_config=cfg)
        os.environ['PC_ADMIN_PAUSED']='1' if cfg.get('_admin_paused') else '0'
        secrets=[os.getenv(k) for k in ('TEXT_API_KEY','IMAGE_API_KEY') if os.getenv(k)]
        class Redactor(logging.Formatter):
            def format(self,record):
                message=super().format(record)
                for secret in secrets+[os.getenv(k) for k in ('TEXT_API_KEY','IMAGE_API_KEY') if os.getenv(k)]:message=message.replace(secret,'[REDACTED]')
                return message
        handler=RotatingFileHandler(root/'logs/server.log',maxBytes=5*1024*1024,backupCount=3,encoding='utf8')
        handler.setFormatter(Redactor('%(asctime)s %(levelname)s %(name)s %(message)s'))
        logging.basicConfig(level=logging.INFO,handlers=[handler],force=True)
        app=runner.create_local_app(live,cfg['port'],cfg);boundaries(app,cfg);recover_interrupted(app,root)
        if live:
            initialize_live_runtime(app)
        options=dict(host='127.0.0.1',port=cfg['port'],threads=cfg['http_threads'],connection_limit=cfg['connection_limit'],
            max_request_body_size=cfg['visitor']['upload_bytes'],max_request_header_size=16384,
            channel_timeout=max(cfg['text_timeout_seconds'],cfg['image_timeout_seconds'])+60,
            expose_tracebacks=False,clear_untrusted_proxy_headers=True,ident='PresentationCopilot')
        if cfg['proxy']:options.update(trusted_proxy='127.0.0.1',trusted_proxy_count=1,trusted_proxy_headers={'x-forwarded-for','x-forwarded-proto'})
        server=create_server(app,**options);process=psutil.Process()
        if os.name=='nt':process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        record=dict(pid=process.pid,create_time=process.create_time(),command=str(runner.ROOT/'scripts/presentation_copilot.py'),
            url=f'http://127.0.0.1:{cfg["port"]}',mode=cfg['mode'],server='waitress',profile=cfg['profile'],batch_id=None,
            runtime_config=cfg['_source'],shutdown_grace_seconds=cfg['shutdown_grace_seconds'],stopped=False)
        runner.PID_FILE.write_text(json.dumps(record),encoding='utf8')
        stop=threading.Event()
        for sig in (signal.SIGINT,signal.SIGTERM):signal.signal(sig,lambda *_:stop.set())
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        print(json.dumps(record,ensure_ascii=False),flush=True)
        from services.ai_providers import copilot_limits as budget
        deadline=None
        try:
            while thread.is_alive():
                if (root/'run/stop.request').exists():
                    req=json.loads((root/'run/stop.request').read_text())
                    if req.get('create_time')==record['create_time']:stop.set()
                mem=psutil.virtual_memory().available/2**30;rss=process.memory_info().rss/2**20
                import shutil
                disk=shutil.disk_usage(root).free/2**30
                if mem<limits['stop_memory_gib'] or rss>limits['rss_mib'] or disk<limits['disk_gib']:stop.set()
                if stop.is_set():
                    os.environ['PC_DRAINING']='1'
                    if deadline is None:deadline=time.monotonic()+cfg['shutdown_grace_seconds']
                    with server.task_dispatcher.lock:
                        http_busy=server.task_dispatcher.active_count or bool(server.task_dispatcher.queue)
                    if not active_tasks(app) and not budget._network_worker_running.is_set() and not http_busy:break
                    if time.monotonic()>=deadline:
                        budget._save(root/'run/interrupted.json',{'at':time.time(),'reason':'grace_expired_unknown_provider_result'})
                        runner.PID_FILE.write_text(json.dumps({**record,'stopped':True}),encoding='utf8')
                        os._exit(2)  # A provider may still charge; next startup pauses, never retries.
                time.sleep(.5)
        finally:
            os.environ['PC_DRAINING']='1';server.close();server.task_dispatcher.shutdown(timeout=5)
            from services.task_manager import task_manager
            task_manager.executor.shutdown(wait=False,cancel_futures=True)
            runner.PID_FILE.write_text(json.dumps({**record,'stopped':True}),encoding='utf8')
