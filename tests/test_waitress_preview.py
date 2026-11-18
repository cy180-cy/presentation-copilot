"""Real loopback Waitress, synthetic SSE only; no model clients or external sockets."""
import json
from pathlib import Path
import sys
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from flask import Flask, Response, request
from waitress import create_server

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'backend')]
from managed_server import boundaries
from services.runtime_config import DEFAULT


def test_sse_does_not_block_status_or_free_http_and_direct_proxy_headers_untrusted():
    app=Flask('offline-wsgi-sse'); release=threading.Event(); streaming=threading.Event()
    @app.get('/stream')
    def stream():
        def chunks():
            yield b'data: offline-only\n\n'
            streaming.set();release.wait(8)
            yield b'data: ended\n\n'
        return Response(chunks(),mimetype='text/event-stream')
    @app.get('/status')
    def status():return {'mode':'preview','secure':request.is_secure,'remote':request.remote_addr}
    @app.get('/free')
    def free():return 'existing synthetic file'
    server=create_server(app,host='127.0.0.1',port=0,threads=4,clear_untrusted_proxy_headers=True)
    port=int(server.effective_port);base=f'http://127.0.0.1:{port}'
    boundaries(app,{**DEFAULT,'port':port,'public_origin':base})
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    output=[]
    reader=threading.Thread(target=lambda:output.append(urlopen(base+'/stream',timeout=10).read()),daemon=True);reader.start()
    try:
        assert streaming.wait(3)
        start=time.monotonic()
        req=Request(base+'/status',headers={'X-Forwarded-Proto':'https','X-Forwarded-For':'198.51.100.100'})
        with urlopen(req,timeout=2) as r:state=json.load(r)
        assert state=={'mode':'preview','secure':False,'remote':'127.0.0.1'}
        with urlopen(base+'/free',timeout=2) as r:assert r.read()==b'existing synthetic file'
        assert time.monotonic()-start<2 and not output
        try:urlopen(Request(base+'/status',headers={'Host':'wrong.example.test'}),timeout=2)
        except HTTPError as e:assert e.code==400
        else:raise AssertionError('Host should be rejected')
    finally:
        release.set();reader.join(3);server.close();server.task_dispatcher.shutdown(timeout=3)
    assert b'data: ended' in output[0]
