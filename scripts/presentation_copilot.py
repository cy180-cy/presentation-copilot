"""One local Flask process; explicit personal config; preview is the default."""
import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))  # Explicit project imports also under python -I.
CONFIG = ROOT / ".env.copilot"
RUNTIME = ROOT / ".copilot"
PID_FILE = RUNTIME / "run/server.json"
PRESET = {
    "AI_PROVIDER_FORMAT": "openai",
    "TEXT_MODEL_SOURCE": "openai", "TEXT_MODEL": "deepseek-flash",
    "TEXT_API_BASE": "https://tokenflux.dev/v1",
    "IMAGE_MODEL_SOURCE": "gemini", "IMAGE_MODEL": "nano-banana-2",
    "IMAGE_API_BASE": "https://tokenflux.dev",
}


def verify_paths():
    if Path(sys.prefix).resolve() != ROOT / ".venv" or sys.prefix == sys.base_prefix:
        raise RuntimeError("必须使用本项目 .venv/Scripts/python.exe。")
    for base in (ROOT, ROOT / ".venv", ROOT / "frontend/dist", RUNTIME):
        for cursor in (base, *base.parents):
            if cursor.exists() and (cursor.is_symlink() or getattr(cursor.lstat(), "st_file_attributes", 0) & 0x400):
                raise RuntimeError("个人运行目录存在链接/联接，停止写入。")
    if RUNTIME.exists():
        for base, directories, files in os.walk(RUNTIME, followlinks=False):
            for name in directories + files:
                item = Path(base) / name
                if item.is_symlink() or getattr(item.lstat(), "st_file_attributes", 0) & 0x400:
                    raise RuntimeError("运行目录中存在链接/联接，停止。")
    if CONFIG.is_symlink() or (CONFIG.exists() and getattr(CONFIG.lstat(), "st_file_attributes", 0) & 0x400):
        raise RuntimeError("配置文件不能是链接。")


def config_values():
    from dotenv import dotenv_values
    # No upward search, interpolation, inherited credentials, or other env files.
    return dict(dotenv_values(CONFIG, interpolate=False)) if CONFIG.exists() else {}


def resource_snapshot():
    import psutil
    return {"cpu_percent_1s": psutil.cpu_percent(interval=1),
            "available_memory_gib": round(psutil.virtual_memory().available / 2**30, 2),
            "project_disk_free_gib": round(shutil.disk_usage(RUNTIME if RUNTIME.exists() else ROOT).free / 2**30, 2)}


def require_resources(snapshot):
    if snapshot["available_memory_gib"] < 6 or snapshot["project_disk_free_gib"] < 5 or snapshot["cpu_percent_1s"] > 80:
        raise RuntimeError("共享主机资源不足：暂停个人任务，不处理其他服务。")


def choose_port(first):
    for port in range(first, first + 10):
        with socket.socket() as probe:
            if sys.platform == "win32":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("拟用环回端口无法独占绑定；不结束任何占用进程。")


def prepare_environment(live, credits, region_edit=False, image_operations=None,
                        model_roles=None, text_total_timeout=720, batch_id=None,
                        confirm_template_retry_call=None, runtime_config=None):
    if region_edit and not live:
        raise RuntimeError('区域模型编辑须同时确认模型调用预算及本地配置。')
    values = config_values()
    roles = ['text', 'image'] if model_roles is None else model_roles
    if confirm_template_retry_call and not batch_id:
        raise RuntimeError('单次模板重试必须绑定原批次。')
    batch_operations = {'template_generate'} if confirm_template_retry_call else {'template_generate', 'slide_edit'}
    batch_credits = 15
    if batch_id in ('pc006-copy-allocation-validation', 'pc006-region-15-to-18-validation', 'pc0081-visitor-image-trace', 'pc0083-content-count-regression'):
        if confirm_template_retry_call:
            raise RuntimeError('本批不允许重试授权。')
        batch_credits = 5
        batch_operations = {'slide_edit'} if batch_id == 'pc006-region-15-to-18-validation' else {'template_generate'}
    if batch_id and (batch_id not in ('pc0061-quality-region', 'pc006-copy-allocation-validation', 'pc006-region-15-to-18-validation', 'pc0081-visitor-image-trace', 'pc0083-content-count-regression') or not live or credits != batch_credits or
                     roles != ['image'] or region_edit or
                     set(image_operations or []) != batch_operations):
        raise RuntimeError('启动范围与固定批次授权不一致。')
    if not roles or set(roles) - {'text', 'image'} or not 30 <= text_total_timeout <= 1800:
        raise RuntimeError('模型范围或文本总时限无效。')
    if live:
        authorization = (runtime_config or {}).get('authorization') or {}
        manual_balance = bool(authorization.get('balance'))
        credit_limit = 100000 if runtime_config else 30
        if not (0 < credits <= credit_limit or (credits == 0 and manual_balance)):
            if runtime_config:
                raise RuntimeError('受管运行需要有效积分上限或简约余额配置。')
            raise RuntimeError("须传入本次明确授权的积分上限，最多 30；不能沿用历史预算。")
        for name, expected in PRESET.items():
            if values.get(name) != expected:
                raise RuntimeError(f"{name} 与本轮固定预设不一致。")
        if not all(values.get(name, "").strip() for name in ("TEXT_API_KEY", "IMAGE_API_KEY")):
            raise RuntimeError("个人配置中缺少文本或图片 Key；不要发到聊天中。")
    essentials = {k: os.environ[k] for k in ("SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATHEXT") if k in os.environ}
    os.environ.clear()
    os.environ.update(essentials)
    for folder in ("data", "uploads", "tmp", "cache", "logs", "run", "profile", "cache/appdata", "cache/localappdata"):
        (RUNTIME / folder).mkdir(parents=True, exist_ok=True)
    os.environ.update({key: values.get(key) or '' for key in PRESET})
    ready = all(values.get(key) == value for key, value in PRESET.items()) and all(
        (values.get(key) or '').strip() for key in ('TEXT_API_KEY', 'IMAGE_API_KEY'))
    operations = image_operations or ['text_generate']
    if set(operations) - {'text_generate', 'template_generate', 'slide_edit', 'material_region_edit'}:
        raise RuntimeError('不支持的图片授权范围。')
    os.environ.update({
        "PATH": str(Path(sys.executable).parent) + os.pathsep + (str(Path(essentials.get("SYSTEMROOT", "C:/Windows")) / "System32") if sys.platform=='win32' else '/usr/bin:/bin'),
        "PC_MODE": "live" if live else "preview", "PC_BUDGET_CREDITS": str(credits),
        "PC_BATCH_ID": batch_id or '',
        "PC_MODEL_ROLES": ','.join(roles) if live else '',
        "PC_TEXT_TOTAL_TIMEOUT_SECONDS": str(text_total_timeout),
        "PC_REGION_EDIT_ENABLED": '1' if live and region_edit else '0',
        "PC_CONFIG_READY": '1' if ready else '0',
        "PC_TEXT_CONFIG_READY": '1' if (values.get('TEXT_API_KEY') or '').strip() and all(values.get(k) == PRESET[k] for k in ('TEXT_MODEL', 'TEXT_MODEL_SOURCE', 'TEXT_API_BASE')) else '0',
        "PC_IMAGE_CONFIG_READY": '1' if (values.get('IMAGE_API_KEY') or '').strip() and all(values.get(k) == PRESET[k] for k in ('IMAGE_MODEL', 'IMAGE_MODEL_SOURCE', 'IMAGE_API_BASE')) else '0',
        "PC_IMAGE_OPERATIONS": ','.join(operations) if live else '',
        "DATABASE_PATH": str(RUNTIME / "data/database.db"), "UPLOAD_FOLDER": str(RUNTIME / "uploads"),
        "TEMP": str(RUNTIME / "tmp"), "TMP": str(RUNTIME / "tmp"),
        "USERPROFILE": str(RUNTIME / "profile"), "APPDATA": str(RUNTIME / "cache/appdata"),
        "LOCALAPPDATA": str(RUNTIME / "cache/localappdata"),
        "XDG_CACHE_HOME": str(RUNTIME / "cache"), "HF_HOME": str(RUNTIME / "cache/huggingface"),
        "HF_HUB_OFFLINE": "1", "CUDA_VISIBLE_DEVICES": "", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1", "PYTHONNOUSERSITE": "1",
        "GENAI_MAX_RETRIES": "0", "GENAI_SDK_ATTEMPTS": "1", "OPENAI_MAX_RETRIES": "0",
        "OPENAI_TIMEOUT": "90", "GENAI_TIMEOUT": "180", "JSON_MAX_ATTEMPTS": "1",
        "MODEL_MAX_OUTPUT_TOKENS": "4096", "MAX_IMAGE_WORKERS": "1", "MAX_DESCRIPTION_WORKERS": "1",
        "OUTPUT_LANGUAGE": "zh", "LOG_LEVEL": "INFO", "FLASK_ENV": "production",
        "OPENAI_API_KEY": "disabled-global-provider", "GOOGLE_API_KEY": "disabled-global-provider",
        "IMAGE_CAPTION_MODEL_SOURCE": "openai", "IMAGE_CAPTION_MODEL": "disabled",
    })
    for name in ("TEXT_API_KEY", "IMAGE_API_KEY"):
        os.environ[name] = values[name] if live else ""
    if runtime_config:
        cfg = runtime_config
        os.environ.update(PC_RUNTIME_ROOT=str(RUNTIME), PC_RUN_VISITOR_LIMITS=json.dumps(cfg['visitor']),
                          PC_MIN_FREE_MEMORY_GIB=str(cfg['resources']['start_memory_gib']),
                          PC_RUN_POLICY=json.dumps({'authorization': cfg['authorization'], 'visitor': cfg['visitor']}) if live else '',
                          GENAI_TIMEOUT=str(cfg['image_timeout_seconds']))
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    import tempfile
    tempfile.tempdir = str(RUNTIME / "tmp")


def install_network_guard(live, managed=False):
    import httpx
    import requests
    original = httpx.Client.send
    paths = {"/v1/chat/completions", f"/v1beta/models/{PRESET['IMAGE_MODEL']}:generateContent"}
    def send(client, request, *args, **kwargs):
        active = os.getenv('PC_MODE') == 'live' if managed else live
        if os.getenv('PC_ADMIN_PAUSED') == '1' or os.getenv('PC_ADMIN_APPLYING') == '1':
            active = False
        if not active or request.url.scheme != "https" or request.url.host != "tokenflux.dev" or request.url.path not in paths or request.method != "POST":
            raise RuntimeError("本地基线禁止此出站请求。")
        payload = json.loads(request.content)
        if request.url.path == "/v1/chat/completions" and payload.get("model") != PRESET["TEXT_MODEL"]:
            raise RuntimeError("禁止调用非本轮文本模型。")
        if request.url.path == f"/v1beta/models/{PRESET['IMAGE_MODEL']}:generateContent":
            from services.ai_providers.genai_client import standardize_inline_image_data
            if standardize_inline_image_data(payload):
                # Rebuild through HTTPX so the body cache and Content-Length agree.
                headers = request.headers.copy()
                headers.pop('content-length', None)
                request = httpx.Request(request.method, request.url, headers=headers,
                    content=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                    extensions=request.extensions)
        from services.task_tracking import sent
        sent()  # After local network checks, immediately before the one transport send.
        return original(client, request, *args, **kwargs)
    httpx.Client.send = send
    def blocked(*args, **kwargs):
        raise RuntimeError("本轮不启用抓取、远程文件下载或其他服务。")
    requests.Session.request = blocked


def create_local_app(live, port, runtime_config=None):
    install_network_guard(live, managed=runtime_config is not None)
    sys.path.insert(0, str(ROOT / "backend"))
    os.environ["CORS_ORIGINS"] = runtime_config['public_origin'] if runtime_config else f"http://127.0.0.1:{port}"
    from app import app
    from flask import abort, request, send_from_directory
    from services.task_manager import task_manager
    task_manager.update_max_workers(1)
    app.config.update({key: os.environ[key] for key in PRESET})
    app.config.update(ENABLE_IMAGE_QUALITY_CONTROL=False, ENABLE_IMAGE_REASONING=False,
                      MAX_DESCRIPTION_WORKERS=1, MAX_IMAGE_WORKERS=1, DEFAULT_RESOLUTION="2K")
    from services.copilot_product import install_product_routes, UNAVAILABLE
    install_product_routes(app, ROOT)
    from services.visitor_access import install as install_visitors
    install_visitors(app, Path(app.config['SQLALCHEMY_DATABASE_URI'].removeprefix('sqlite:///')).parent.parent / 'run')
    if runtime_config:
        from controllers.admin_controller import install as install_admin
        install_admin(app, runtime_config, ROOT, admin_credentials, lambda cfg: prepare_runtime_models(app, cfg))
    dist = ROOT / '.copilot/frontend-dist'
    if not (dist / 'index.html').exists():
        raise RuntimeError('请先构建个人产品界面；不回退到上游自托管设置界面。')
    def frontend(filename=""):
        if filename.startswith(("api/", "files/")):
            abort(404)
        candidate = (dist / filename).resolve()
        if not candidate.is_relative_to(dist.resolve()):
            abort(404)
        if filename and candidate.is_file():
            return send_from_directory(dist, filename)
        # The desktop-compatible upstream build uses relative asset URLs.
        html = (dist / "index.html").read_text(encoding="utf8").replace('="./assets/', '="/assets/')
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}
    app.view_functions["index"] = frontend
    app.add_url_rule("/<path:filename>", "copilot_frontend", frontend)
    @app.before_request
    def baseline_scope():
        path = request.path
        if path.startswith("/api/settings"):
            return {"error": {"message": "此功能不对普通用户开放。"}}, 403
        from services.copilot_product import check_local_upload, execution_error
        import re
        local_upload = request.method == 'POST' and bool(re.fullmatch(
            r'/api/(user-templates|projects/renovation|projects/[\w-]+/(template|template-assets|template-assets/upload-pdf|template-mode/single-with-upload))', path))
        free_attachment = bool(re.fullmatch(r'/api/reference-files(?:/upload|/[\w-]+(?:/(?:associate|dissociate|download))?|/project/[\w-]+)', path))
        free_material = request.method == 'POST' and bool(re.fullmatch(r'/api/(?:materials/upload|projects/[\w-]+/materials/upload)', path))
        if free_material and request.args.get('generate_caption', '').lower() in ('true', '1', 'yes'):
            return {'error': {'message': '当前不启用上传后的自动识图，可直接上传素材。'}}, 403
        if local_upload:
            error = check_local_upload(request)
            if error:
                return error
        # Importing original pages and assigning images are free. Parsing,
        # template analysis and layout vision have no authorized service yet.
        if path.endswith(('/reanalyze', '/regenerate-renovation', '/renovation/process', '/materials/generate')) or '/auto-match' in path:
            return {"error": {"code": "SERVICE_UNAVAILABLE", "message": UNAVAILABLE}}, 503
        reference_listing = request.method == 'GET' and path.startswith('/api/reference-files/project/')
        # This one controller validates opt-in authorization before saving files.
        region_edit_request = request.method == 'POST' and bool(re.fullmatch(
            r'/api/projects/[\w-]+/materials/process', path))
        slide_edit_request = request.method == 'POST' and bool(re.fullmatch(
            r'/api/projects/[\w-]+/pages/[\w-]+/edit/image', path))
        if slide_edit_request:
            error = execution_error('image', 'slide_edit')
            if error:
                return error
        if not local_upload and not free_attachment and not free_material and not reference_listing and not region_edit_request and not slide_edit_request and (request.mimetype == "multipart/form-data" or any(part in path for part in
                ("oauth", "reference-files", "mineru", "renovation", "narration", "export/video", "edit/image", "extract-style"))):
            return {"error": {"message": "当前预览支持文字输入与图片式导出，此功能暂未开放。"}}, 403
        if path.startswith('/api/') and any(part in path for part in ('/generate/', '/refine/', '/generate-style', '/auto-match')):
            role = 'image' if path.endswith(('/image', '/images')) else 'text'
            error = execution_error(role)
            if error:
                return error
    @app.after_request
    def redact_response(response):
        secrets = [os.getenv(name) for name in ('TEXT_API_KEY', 'IMAGE_API_KEY') if os.getenv(name)]
        if response.is_json and response.status_code >= 500 and not (
                isinstance(response.get_json(), dict) and response.get_json().get('error', {}).get('code') in
                ('preview', 'configuration_missing', 'provider_unsupported', 'authorization_required', 'budget_exhausted', 'previous_failure')):
            response.set_data(json.dumps({'error': {'message': UNAVAILABLE}}, ensure_ascii=False))
        if secrets and (response.is_json or response.mimetype == "text/plain"):
            body = response.get_data(as_text=True)
            for secret in secrets:
                body = body.replace(secret, "[REDACTED]")
            response.set_data(body)
        return response
    return app


def admin_credentials():
    """Read-only safe projection. Key presence is not a connectivity test."""
    values=config_values()
    return {'ready':all(values.get(k)==v for k,v in PRESET.items()) and all((values.get(k) or '').strip() for k in ('TEXT_API_KEY','IMAGE_API_KEY')),
        'source':'.env.copilot', 'preset':'copilot-personal-verified-pair',
        'models':[{ 'role':r.lower(),'model':values.get(r+'_MODEL') if values.get(r+'_MODEL')==PRESET[r+'_MODEL'] else '与受管预设不同',
            'provider':PRESET[r+'_MODEL_SOURCE'], 'protocol':'OpenAI Chat Completions' if r=='TEXT' else 'Gemini GenerateContent',
            'configured':bool((values.get(r+'_API_KEY') or '').strip()),
            'validation':'曾通过限定案例；不是当前连通探测'} for r in ('TEXT','IMAGE')], 'probe_performed':False}


def prepare_runtime_models(app, cfg):
    """Only invoked by explicit admin apply, with admissions blocked and zero tasks."""
    values=config_values()
    if not admin_credentials()['ready']: raise ValueError('模型配置未齐全或与预设不符。')
    for k in (*PRESET, 'TEXT_API_KEY', 'IMAGE_API_KEY'):
        os.environ[k]=values[k];app.config[k]=values[k]
    from services.ai_providers import get_text_provider, get_image_provider
    with app.app_context():
        # Construction only. Every actual send retains the original guards.
        try:
            get_text_provider();get_image_provider()
        except Exception:
            raise RuntimeError('受管Provider构造失败；未发送请求。') from None
        from services.ai_service_manager import clear_ai_service_cache
        clear_ai_service_cache()


def recorded_process():
    import psutil
    if not PID_FILE.exists():
        return None
    record = json.loads(PID_FILE.read_text(encoding="utf8"))
    if record.get("stopped"):
        return None
    try:
        process = psutil.Process(record["pid"])
        arguments = process.cmdline()
        same_script = any((Path(process.cwd()) / arg).resolve() == Path(__file__).resolve()
                          for arg in arguments if arg.endswith("presentation_copilot.py"))
        if abs(process.create_time() - record["create_time"]) > 0.1 or not same_script or "serve" not in arguments:
            raise RuntimeError("PID 创建时间或命令不匹配，拒绝操作。")
        return process
    except psutil.NoSuchProcess:
        raise RuntimeError("记录中的进程当前不可见，不能确认已停止；请回到原启动终端核对，不操作其他进程。") from None


def serve(args):
    if getattr(args, 'runtime_config', None):
        import managed_server
        return managed_server.serve(args, sys.modules[__name__])
    from managed_server import DataLock
    with DataLock(RUNTIME / 'run'):
        return serve_legacy(args)


def serve_legacy(args):
    if bool(args.trial_visitor) != (args.batch_id in ('pc0081-visitor-image-trace', 'pc0083-content-count-regression')):
        raise RuntimeError('指定访客仅能用于本次固定访客验收批次。')
    import psutil
    if recorded_process():
        raise RuntimeError("本项目已有记录中的服务，先检查 status，不重复启动。")
    resources = resource_snapshot()
    require_resources(resources)
    port = choose_port(args.port)
    prepare_environment(args.enable_models, args.budget_credits, args.enable_region_edit, args.image_operations,
                        args.model_roles, args.text_total_timeout_seconds, args.batch_id,
                        args.confirm_template_retry_call or args.confirm_final_template_retry_call)
    secrets = [os.environ[name] for name in ("TEXT_API_KEY", "IMAGE_API_KEY") if args.enable_models]
    class RedactingFormatter(logging.Formatter):
        def format(self, record):
            text = super().format(record)
            for secret in secrets:
                text = text.replace(secret, "[REDACTED]")
            return text
    handler = logging.FileHandler(RUNTIME / "logs/server.log", encoding="utf8")
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    app = create_local_app(args.enable_models, port)
    from werkzeug.serving import ThreadedWSGIServer
    class LocalServer(ThreadedWSGIServer):
        allow_reuse_address = False
        def server_bind(self):
            if sys.platform == "win32":
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()
    server = LocalServer("127.0.0.1", port, app)
    if args.batch_id:
        from services.ai_providers.copilot_limits import initialize_batch, authorize_template_retry, authorize_final_template_retry
        try:
            if args.confirm_template_retry_call and args.confirm_final_template_retry_call:
                raise RuntimeError('不能同时应用两项授权。')
            if args.confirm_template_retry_call:
                authorize_template_retry(args.confirm_template_retry_call)
            if args.confirm_final_template_retry_call:
                authorize_final_template_retry(args.confirm_final_template_retry_call)
            if args.batch_id in ('pc0081-visitor-image-trace', 'pc0083-content-count-regression'):
                from services.visitor_access import bind_trial
                bind_trial(app, args.trial_visitor)
            else:
                initialize_batch(args.batch_id)
        except Exception:
            server.server_close()
            raise
    process = psutil.Process()
    priority = "unchanged"
    try:
        process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        priority = "below_normal"
    except (psutil.AccessDenied, AttributeError):
        pass
    record = {"pid": process.pid, "create_time": process.create_time(), "command": str(Path(__file__).resolve()),
              "url": f"http://127.0.0.1:{port}", "mode": os.environ["PC_MODE"], "priority": priority,
              "batch_id": args.batch_id,
              "region_edit_enabled": os.environ['PC_REGION_EDIT_ENABLED'] == '1',
              "startup_resources": resources, "stopped": False}
    PID_FILE.write_text(json.dumps(record, indent=2), encoding="utf8")
    stop = threading.Event()
    def monitor():
        while not stop.wait(5):
            memory = process.memory_info()
            row = {"time": time.time(), "rss_bytes": memory.rss, "peak_working_set_bytes": getattr(memory, "peak_wset", None),
                   "process_cpu_percent": process.cpu_percent(), "host_available_bytes": psutil.virtual_memory().available}
            with (RUNTIME / "logs/resources.jsonl").open("a", encoding="utf8") as target:
                target.write(json.dumps(row) + "\n")
            if memory.rss > 1536 * 2**20 or row["host_available_bytes"] < 4 * 2**30:
                logging.error("Personal service resource threshold reached; stopping this server only.")
                server.shutdown()
                return
    threading.Thread(target=monitor, daemon=True).start()
    print(json.dumps(record, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        record["stopped"] = True
        PID_FILE.write_text(json.dumps(record, indent=2), encoding="utf8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "serve", "status", "stop", "admin-init", "admin-reset", "ledger-settle", "ledger-cycle", "image-retry"))
    parser.add_argument("--port", type=int, default=5011)
    parser.add_argument('--runtime-config', help='Explicit nonsecret runtime policy; managed Waitress, preview by default')
    parser.add_argument('--settlement-file', help='Private, manually verified invoice/unsent evidence manifest; dry-run by default')
    parser.add_argument('--apply-settlement', help='Apply only the exact confirmation hash returned by ledger-settle dry-run')
    parser.add_argument('--cycle-id', help='Explicit one-time approved initial cycle ID; never set by HTTP or restart')
    parser.add_argument('--cycle-credits', type=float, help='Approved application cycle cap; not a provider balance sync')
    parser.add_argument('--cycle-revision-id', help='Explicit idempotent amendment of the current cycle cap')
    parser.add_argument('--authorization-reason', help='User approval evidence for maintenance scope')
    parser.add_argument('--apply-cycle', help='Apply only the exact offline ledger-cycle dry-run hash')
    parser.add_argument('--retry-failed-call', help='Explicitly approved failed image call; old fee stays reserved')
    parser.add_argument('--historical-quality-file', help='Explicit finite historical-page quality approval manifest')
    parser.add_argument('--quality-source-call', help='Successful current image call to recheck once with a newly prepared prompt')
    parser.add_argument('--apply-image-retry', help='Apply exact single-image retry preview hash while stopped')
    parser.add_argument('--daily-use-file', help='Explicit acceptance closeout manifest; preserves the applied manual balance')
    parser.add_argument("--enable-models", action="store_true", help="Only after explicit budget + local config confirmation")
    parser.add_argument("--enable-region-edit", action="store_true", help="Only after explicit image editing authorization; also requires --enable-models")
    parser.add_argument("--budget-credits", type=float, default=0)
    parser.add_argument('--trial-visitor', help='Existing normal visitor identity; only a fixed visitor acceptance batch')
    parser.add_argument('--batch-id', choices=['pc0061-quality-region', 'pc006-copy-allocation-validation', 'pc006-region-15-to-18-validation', 'pc0081-visitor-image-trace', 'pc0083-content-count-regression'],
                        help='Resume a fixed authorization; never reset its starting counters')
    parser.add_argument('--confirm-template-retry-call',
                        help='Explicit approval of a failed call: reassign final batch attempt to template only')
    parser.add_argument('--confirm-final-template-retry-call',
                        help='Explicit further approval: third and final template attempt, same 15-credit ceiling')
    parser.add_argument('--model-roles', nargs='+', choices=['text', 'image'], default=['text', 'image'],
                        help='Narrow this run to explicitly authorized model roles')
    parser.add_argument('--text-total-timeout-seconds', type=int, default=720,
                        help='Local text waiting deadline, 30..1800 seconds; no automatic retry')
    parser.add_argument('--image-operations', nargs='+', default=['text_generate'],
                        choices=['text_generate', 'template_generate', 'slide_edit', 'material_region_edit'],
                        help='Only explicitly approved operations; does not enable models or add budget')
    parser.add_argument('--retire-image-permit', action='append', default=[], help='Explicit stale historical permit to revoke during same-cycle amendment')
    args = parser.parse_args()
    if args.runtime_config:
        import managed_server
        managed_server.select_paths(args, sys.modules[__name__])
    verify_paths()
    if args.retire_image_permit and (args.action != 'ledger-cycle' or not args.cycle_revision_id):
        raise ValueError('撤销旧许可仅用于显式周期修订。')
    if args.action == 'image-retry':
        if not args.runtime_config or sum(bool(v) for v in (args.retry_failed_call,args.quality_source_call,args.historical_quality_file,args.daily_use_file)) != 1:
            raise ValueError('单次许可需要明确运行配置，且只选择失败重试或成功页质量复验之一。')
        if any((args.enable_models, args.enable_region_edit, args.budget_credits,
                args.settlement_file, args.apply_settlement, args.cycle_id,
                args.cycle_credits, args.apply_cycle, args.cycle_revision_id)):
            raise ValueError('单次重试不能与结算、周期或额外模型授权混用。')
        if args.apply_image_retry and recorded_process():
            raise RuntimeError('本项目服务仍运行；请先停止后应用单次重试许可。')
        if not args.daily_use_file and not admin_credentials()['ready']:
            raise ValueError('当前模型配置与已批准预设不符。')
        if args.quality_source_call or args.historical_quality_file:
            # Maintenance assembles prompts only. No keys or live SDK transport.
            prepare_environment(False, 0, runtime_config=args.managed_config)
            install_network_guard(False)
        from image_retry_maintenance import run
        print(json.dumps(run(RUNTIME, args.quality_source_call or args.retry_failed_call, PRESET['IMAGE_MODEL'],
                             args.apply_image_retry, quality=bool(args.quality_source_call), historical_file=args.historical_quality_file,daily_file=args.daily_use_file), ensure_ascii=False, indent=2))
        return
    if args.retry_failed_call or args.quality_source_call or args.apply_image_retry or args.historical_quality_file or args.daily_use_file:
        raise ValueError('单次重试参数仅用于image-retry。')
    if args.action == 'ledger-cycle':
        if not args.runtime_config or not args.cycle_id or args.cycle_credits is None:
            raise ValueError('周期迁移需要明确运行配置、周期ID和预算。')
        if args.enable_models or args.enable_region_edit or args.settlement_file or args.apply_settlement:
            raise ValueError('周期迁移不得与模型授权或结算混用。')
        if recorded_process():raise RuntimeError('本项目服务仍运行，请保持停止后迁移。')
        from cycle_maintenance import run
        print(json.dumps(run(RUNTIME,args.cycle_id,args.cycle_credits,args.apply_cycle, revision_id=args.cycle_revision_id, reason=args.authorization_reason, retire_permit_ids=args.retire_image_permit),ensure_ascii=False,indent=2))
        return
    if args.cycle_id or args.cycle_credits is not None or args.apply_cycle or args.cycle_revision_id or args.authorization_reason:
        raise ValueError('周期参数仅用于ledger-cycle。')
    if args.action == 'ledger-settle':
        if not args.runtime_config or not args.settlement_file:
            raise ValueError('结算需要明确的运行配置和私有证据清单。')
        if recorded_process():
            raise RuntimeError('本项目服务仍运行；结算仅在停止后执行，不自动停止服务。')
        from ledger_maintenance import run
        print(json.dumps(run(RUNTIME, args.settlement_file, args.apply_settlement), ensure_ascii=False, indent=2))
        return
    if args.settlement_file or args.apply_settlement:
        raise ValueError('结算参数仅用于ledger-settle。')
    if args.action in ('admin-init','admin-reset'):
        if not args.runtime_config:
            raise RuntimeError('管理员初始化需要明确的--runtime-config。')
        import getpass
        if not sys.stdin.isatty(): raise RuntimeError('请在本机交互式PowerShell使用隐藏输入设置密码。')
        from services.admin_store import Store
        store=Store(RUNTIME/'data/database.db')
        # Tables are installed by the managed preview upgrade, never create a new DB here.
        if not store.path.exists(): raise RuntimeError('请先启动受管preview服务。')
        first=getpass.getpass('设置管理员密码（12–128字符，隐藏输入）：')
        second=getpass.getpass('再次输入密码：')
        if first!=second: raise RuntimeError('两次密码不一致，未保存。')
        store.initialize(first,reset=args.action=='admin-reset')
        print('管理员凭据已保存；旧管理会话已失效。访客身份和预算未修改。')
        return
    if args.runtime_config and args.action == 'check':
        import managed_server
        print(json.dumps(managed_server.inspect(args, sys.modules[__name__]), ensure_ascii=False, indent=2))
        return
    if args.action == "check":
        values = config_values()
        print(json.dumps({"python": sys.executable, "config": str(CONFIG),
                          "required_fields_present": {key: bool(values.get(key)) for key in (*PRESET, "TEXT_API_KEY", "IMAGE_API_KEY")},
                          "models": {role: {
                              "model_id": values.get(f'{role}_MODEL') if values.get(f'{role}_MODEL') == PRESET[f'{role}_MODEL'] else '缺失或与预设不同',
                              "protocol": values.get(f'{role}_MODEL_SOURCE') if values.get(f'{role}_MODEL_SOURCE') == PRESET[f'{role}_MODEL_SOURCE'] else '缺失或与预设不同',
                              "address": values.get(f'{role}_API_BASE') if values.get(f'{role}_API_BASE') == PRESET[f'{role}_API_BASE'] else '缺失或与预设不同（不打印自定义地址）',
                              "source": '.env.copilot', "key_present": bool(values.get(f'{role}_API_KEY')),
                              "connection_verified": False} for role in ('TEXT', 'IMAGE')},
                          "resources": resource_snapshot()}, ensure_ascii=False, indent=2))
    elif args.action in ("status", "stop"):
        process = recorded_process()
        if process and args.action == "stop":
            record = json.loads(PID_FILE.read_text(encoding="utf8"))
            if record.get('server') == 'waitress':
                (RUNTIME/'run/stop.request').write_text(json.dumps({'create_time': record['create_time']}), encoding='utf8')
                process.wait(timeout=record.get('shutdown_grace_seconds',45)+15)
            else:
                process.terminate()
                process.wait(timeout=10)
            record["stopped"] = True
            PID_FILE.write_text(json.dumps(record, indent=2), encoding="utf8")
        print(json.dumps({"running": bool(process) and args.action != "stop", "pid": process.pid if process else None}))
    else:
        serve(args)


if __name__ == "__main__":
    main()
