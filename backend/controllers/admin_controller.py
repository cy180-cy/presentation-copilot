"""Dedicated authenticated admin API; never changes visitor ownership."""
import json
import secrets
import sqlite3
from pathlib import Path
from flask import Blueprint, g, request
from services.admin_store import Store, COOKIE, SESSION_SECONDS
from services.admin_runtime import RuntimeControl


def install(app, cfg, code_root, credentials, prepare_models):
    store=Store(Path(cfg['data_dir'])/'data/database.db');store.migrate()
    control=RuntimeControl(app,store,cfg,code_root,credentials,prepare_models)
    app.extensions['pc_admin']=control
    bp=Blueprint('pc_admin',__name__,url_prefix='/api/admin')

    def error(code, message, status): return {'error':{'code':code,'message':message}},status

    @bp.before_request
    def guard():
        if request.method not in ('GET','HEAD','OPTIONS'):
            if request.headers.get('Origin') != cfg['public_origin'] or request.headers.get('Sec-Fetch-Site') == 'cross-site':
                return error('ADMIN_CSRF','请从同源管理页面操作。',403)
            if not request.is_json or request.content_length is None or request.content_length>16384:
                return error('ADMIN_INPUT','请提交有限大小的JSON。',400)
        token=request.cookies.get(COOKIE,'');session=store.session(token)
        g.admin_token=token;g.admin_session=session
        if request.endpoint=='pc_admin.session' and request.method=='GET':return
        if not session:return error('ADMIN_LOGIN_REQUIRED','管理会话已失效，请重新登录。',401)
        if request.method not in ('GET','HEAD','OPTIONS') and not secrets.compare_digest(request.headers.get('X-PC-Admin-CSRF',''),session['csrf']):
            return error('ADMIN_CSRF','管理页面凭据已过期，请刷新。',403)
        if request.endpoint!='pc_admin.login' and not session['authenticated']:
            return error('ADMIN_LOGIN_REQUIRED','请先登录开发者后台。',401)

    @bp.after_request
    def private(response):
        response.headers['Cache-Control']='private, no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers.pop('Access-Control-Allow-Origin',None)
        response.headers.pop('Access-Control-Allow-Credentials',None)
        return response

    @bp.errorhandler(ValueError)
    def invalid(exc):return error('ADMIN_VALIDATION',str(exc),400)
    @bp.errorhandler(sqlite3.Error)
    @bp.errorhandler(OSError)
    @bp.errorhandler(RuntimeError)
    def failed(_exc):return error('ADMIN_UNAVAILABLE','管理操作未完成，保留原配置；请核对本地运行状态。',503)

    def body(fields):
        data=request.get_json(silent=True)
        if not isinstance(data,dict) or set(data)!=set(fields):raise ValueError('请求字段不符合管理操作。')
        return data

    def cookie(response,token,age):
        response.set_cookie(COOKIE,token,max_age=age,httponly=True,secure=cfg['proxy'],samesite='Strict',path='/api/admin')
        return response

    @bp.get('/session')
    def session():
        row=g.admin_session
        with store.transaction() as c:initialized=c.execute('SELECT id FROM pc_admin_identity').fetchone() is not None
        if row:
            return {'data':{'authenticated':bool(row['authenticated']),'initialized':initialized,'csrf':row['csrf'],'expires_at':row['expires']}}
        token,csrf,initialized=store.create_session()
        return cookie(app.json.response({'data':{'authenticated':False,'initialized':initialized,'csrf':csrf}}),token,900)

    @bp.post('/login')
    def login():
        result,message=store.login(g.admin_token,body({'password'})['password'])
        if not result:return error('ADMIN_LOGIN_FAILED',message,401)
        token,csrf=result
        return cookie(app.json.response({'data':{'authenticated':True,'csrf':csrf}}),token,SESSION_SECONDS)

    @bp.post('/logout')
    def logout():
        body(set());store.logout(g.admin_token)
        return cookie(app.json.response({'data':{'authenticated':False}}),'',0)

    @bp.get('/overview')
    def overview():return {'data':control.view()}

    @bp.put('/draft')
    def draft():
        d=body({'draft','revision','draft_revision'})
        return {'data':control.draft(d['draft'],d['revision'],d['draft_revision'])}

    @bp.post('/validate')
    def validate():body(set());return {'data':control.preview_change()}

    @bp.post('/apply')
    def apply():
        d=body({'confirmation','phrase'});return {'data':control.apply(d['confirmation'],d['phrase'])}

    @bp.post('/pause')
    def pause():
        d=body({'phrase'})
        if d['phrase']!='暂停新生成':raise ValueError('需要确认暂停新生成。')
        return {'data':control.pause()}

    @bp.post('/resume')
    def resume():
        d=body({'confirmation','phrase'});return {'data':control.apply(d['confirmation'],d['phrase'],resume=True)}

    def page_number():
        try:page=int(request.args.get('page','1'))
        except ValueError:raise ValueError('页码无效。') from None
        if not 1<=page<=10000:raise ValueError('页码无效。')
        return page

    @bp.get('/tasks')
    def tasks():
        from models import Task, db, ResourceOwner, TaskTrace
        page=page_number();query=db.session.query(Task)
        status=request.args.get('status')
        if status:
            if status not in ('PENDING','PROCESSING','COMPLETED','FAILED','INTERRUPTED','CANCELLED'):raise ValueError('状态筛选无效。')
            query=query.filter(Task.status==status)
        rows=query.order_by(Task.created_at.desc()).offset((page-1)*20).limit(21).all()
        result=[]
        for task in rows[:20]:
            owner=db.session.get(ResourceOwner,('tasks',task.id));trace=db.session.get(TaskTrace,task.id)
            snap=json.loads(trace.snapshot) if trace else {}
            result.append({'task_id':task.id,'operation':task.task_type,'status':task.status,'created_at':task.created_at.isoformat(),
                'visitor':owner.visitor_id if owner else None,'model':snap.get('model'),'tracked':trace is not None})
        return {'data':{'items':result,'page':page,'has_more':len(rows)>20}}

    @bp.get('/tasks/<task_id>')
    def task_detail(task_id):
        from models import db, Task, TaskTrace
        from controllers.task_trace_controller import detail
        task=db.session.get(Task,task_id)
        if not task:return error('NOT_FOUND','任务不存在。',404)
        data=detail(task,db.session.get(TaskTrace,task_id))
        if data['snapshot']:
            # Admin sees provenance and usage, not private prompts/edit instructions.
            data['snapshot']['pages']=[{k:v for k,v in p.items() if k not in ('edit_instruction','references')} for p in data['snapshot']['pages']]
        allowed={'call_id','role','model','operation','status','started_at','ended_at','elapsed_seconds','usage',
                 'reserved_credits','estimated_credits','actual_credits','actual_credits_source','estimate_source',
                 'cost_status','failure_stage','error_type','provider_request_id','response_id','page_id'}
        data['calls']=[{k:v for k,v in c.items() if k in allowed} for c in data['calls']]
        # Read projection only: immutable historical TaskTrace rows stay intact.
        from services.ai_providers.copilot_limits import budget_state
        from services.ledger_settlement import entries
        settled=entries(budget_state())
        for call in data['calls']:
            row=settled.get(call['call_id'])
            if row:
                source = ('verified_invoice' if row['kind']=='invoice' else
                          f"{row['confirmation_source']}_confirmed_no_charge" if row['kind']=='confirmed_no_charge'
                          else 'confirmed_not_sent')
                call.update(actual_credits=row['actual_credits'],
                    actual_credits_source=source,
                    cost_status={'invoice':'settled', 'confirmed_not_sent':'not_sent',
                                 'confirmed_no_charge':'confirmed_no_charge'}[row['kind']],
                    settlement_evidence_sha256=row['evidence_sha256'])
        data['outputs']=[{k:v for k,v in o.items() if k in {'page_id','version_id','version_number','call_id','status'}} for o in data['outputs']]
        data['quality_note']='执行完成仅表示执行状态，图片内容仍需人工核查。'
        return {'data':data}

    @bp.get('/audit')
    def audit():
        page=page_number()
        with store.transaction() as c:
            rows=c.execute('SELECT id,at,actor,action,summary FROM pc_admin_audit ORDER BY id DESC LIMIT 21 OFFSET ?',((page-1)*20,)).fetchall()
        return {'data':{'items':[dict(r) for r in rows[:20]],'page':page,'has_more':len(rows)>20}}

    app.register_blueprint(bp)
