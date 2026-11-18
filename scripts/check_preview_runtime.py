"""Opt-in loopback smoke with fresh SYNTHETIC state; never uses the personal DB/keys."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'validation'),str(ROOT/'backend'),str(ROOT/'scripts')]
from offline_guard import activate
activate()
from runtime_maintenance import import_examples
from presentation_copilot import resource_snapshot, require_resources


def main():
    import requests
    require_resources(resource_snapshot())
    folder=ROOT/'.copilot/tmp'/('pc009-preview-'+str(time.time_ns()));folder.mkdir(parents=True)
    state=folder/'state';state.mkdir();secret=folder/'.env.copilot';secret.write_text('# OFFLINE PREVIEW ONLY\n')
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    base=f'http://127.0.0.1:{port}';config=folder/'runtime.json'
    config.write_text(json.dumps({'mode':'preview','data_dir':str(state),'secret_file':str(secret),
                                 'port':port,'public_origin':base}),encoding='utf8')
    import_examples(state,ROOT/'.copilot/examples/water-saving')
    cmd=[sys.executable,'-I','-B','-X','utf8',str(ROOT/'scripts/presentation_copilot.py')]
    env={k:v for k,v in os.environ.items() if not any(t in k.upper() for t in ('TOKEN','API_KEY','PC_'))}
    flags={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
    log=(folder/'startup.txt').open('w',encoding='utf8');process=None
    def start():
        p=subprocess.Popen(cmd+['serve','--runtime-config',str(config)],cwd=ROOT,env=env,stdout=log,stderr=log,**flags)
        for _ in range(80):
            if p.poll() is not None:raise RuntimeError('隔离预览启动失败，见 '+str(folder/'startup.txt'))
            try:
                r=requests.get(base+'/health',timeout=.5)
                if r.status_code==200:return p
            except requests.RequestException:pass
            time.sleep(.25)
        raise RuntimeError('隔离预览未及时就绪。')
    def stop(p):
        completed=subprocess.run(cmd+['stop','--runtime-config',str(config)],cwd=ROOT,env=env,capture_output=True,timeout=65,**flags)
        assert completed.returncode==0,'受管停止失败'
        p.wait(timeout=10)
    try:
        process=start()
        second=subprocess.run(cmd+['serve','--runtime-config',str(config)],cwd=ROOT,env=env,capture_output=True,timeout=10,**flags)
        assert second.returncode!=0 and '锁定' in second.stderr.decode('utf8')
        def visitor():
            session=requests.Session();session.trust_env=False
            r=session.get(base+'/api/visitor/session',timeout=3);assert r.status_code==200
            session.headers.update({'Origin':base,'X-PC-CSRF':r.json()['data']['csrf']})
            assert 'Secure' not in r.headers.get('Set-Cookie','')
            return session
        a,b=visitor(),visitor()
        def project(s):
            r=s.post(base+'/api/projects',json={'creation_type':'blank'},timeout=3);assert r.status_code==201
            return r.json()['data']['project_id']
        pa,pb=project(a),project(b)
        assert b.get(base+'/api/projects/'+pa,timeout=3).status_code==404
        assert a.get(base+'/api/projects/'+pa,timeout=3).status_code==200
        assert a.get(base+'/api/settings',timeout=3).status_code==403
        assert a.post(base+'/api/projects',json={'creation_type':'blank'},headers={'Origin':'https://invalid.example'},timeout=3).status_code==403
        r=a.get(base+'/api/visitor/example',timeout=3);assert r.json()['data']['source']=='pregenerated_demo'
        assert a.get(base+'/api/product/status',timeout=3).json()['data']['mode']=='preview'
        assert requests.get(base+'/health',headers={'Host':'wrong.example.test'},timeout=3).status_code==400
        page=a.post(base+f'/api/projects/{pa}/pages',json={'order_index':0,'outline_content':{'title':'OFFLINE PREVIEW'}},timeout=3).json()['data']['page_id']
        r=a.post(base+f'/api/projects/{pa}/pages/{page}/generate/image',json={},timeout=3)
        assert r.status_code==403 and not (state/'run/call-budget.json').exists()
        first_pid=process.pid;stop(process);process=None
        process=start()
        assert a.get(base+'/api/projects/'+pa,timeout=3).status_code==200
        assert b.get(base+'/api/projects/'+pa,timeout=3).status_code==404
        assert not (state/'run/call-budget.json').exists()
        result={'mode':'preview','first_pid':first_pid,'restart_pid':process.pid,'same_data_lock':True,
                'two_http_sessions':True,'preview_model_rejected':True,'persistent_identity':True,
                'host_origin_csrf_admin_checks':True,'public_example':True,'real_model_calls':0,'folder':str(folder)}
        (ROOT/'.copilot/logs/pc009/wsgi-smoke.json').write_text(json.dumps(result,indent=2),encoding='utf8')
        print(json.dumps(result,ensure_ascii=False))
    finally:
        if process and process.poll() is None:stop(process)
        log.close()


if __name__=='__main__':main()
