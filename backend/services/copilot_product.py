"""Small product-mode boundary; no model requests or credential discovery."""
import json
import os
from pathlib import Path
from contextlib import contextmanager

UNAVAILABLE = '生成服务暂不可用，请稍后重试。'
PREFERENCE_FIELDS = {'description_generation_mode', 'description_extra_fields', 'image_prompt_extra_fields', 'output_language'}
REASONS = {
    'admin_paused': '生成已暂停，手动编辑、查看作品和下载仍可使用。',
    'configuration_applying': '运行配置正在更新，请稍后再试。',
    'authorization_expired': '本次生成授权尚未生效或已到期，已有作品仍可查看和下载。',
    'preview': '当前为预览模式，可手动编辑和预览；模型执行尚未开放。',
    'configuration_missing': UNAVAILABLE,
    'provider_unsupported': '当前服务尚未接入这项能力。',
    'authorization_required': '当前操作尚未开放，可继续手动编辑。',
    'budget_exhausted': '本次可用额度已用完，请稍后重试。',
    'previous_failure': '上次执行未完成核对，模型执行已暂停。',
    'request_in_progress': '模型请求正在处理中，请勿重复提交。',
    'task_failed': '本次任务已有失败请求，请查看运行记录；不会自动重试。',
    'integration_required': '这项功能还需要接入额外服务，当前可使用手动编辑。',
    'trial_unconfigured': '试用额度尚未配置，已有作品仍可查看和下载。',
    'trial_exhausted': '当前访客试用额度已用完，手动编辑和下载仍可使用。',
}


def capability_status(role, operation=None):
    from services.ai_providers.copilot_limits import execution_reason, configured, provider_supported
    reason = execution_reason(role, operation)
    from services.visitor_access import enabled, identity, limits, usage_summary
    if enabled() and operation == 'material_region_edit' and reason is None:
        reason = 'integration_required'
    if reason is None and enabled() and identity():
        cap, used = limits(), usage_summary(identity())
        from services.manual_balance import configured as manual_balance
        manual = manual_balance()
        if not cap['credits'] and not manual:
            reason = 'trial_unconfigured'
        elif used[role] >= cap[role] or (not manual and used['reserved'] >= cap['credits']):
            reason = 'trial_exhausted'
    return {'implemented': True, 'provider_supported': provider_supported(role),
            'configured': configured(role), 'verified': False,
            'available': reason is None, 'reason': reason, 'message': REASONS.get(reason, '')}


def execution_error(role, operation=None):
    status = capability_status(role, operation)
    if not status['available']:
        code = 429 if status['reason'] == 'trial_exhausted' else 403 if status['reason'] == 'trial_unconfigured' else 503
        return {'error': {'code': status['reason'], 'message': status['message']}}, code
    return None


def validate_local_image(path, base):
    """No symlinks, remote URLs, path traversal or oversized decoded images."""
    from PIL import Image
    base = Path(base).resolve()
    candidate = Path(path)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base) or not resolved.is_file():
        raise ValueError('图片必须属于当前任务的本地目录。')
    for cursor in (candidate, *candidate.parents):
        if cursor == base.parent:
            break
        if cursor.is_symlink() or (cursor.exists() and getattr(cursor.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('图片路径不允许使用链接。')
    if resolved.stat().st_size > 20 * 2**20:
        raise ValueError('参考图片总大小上限为 20 MB。')
    with Image.open(resolved) as image:
        if image.format not in {'PNG', 'JPEG', 'WEBP', 'GIF'} or image.width * image.height > 20_000_000:
            raise ValueError('仅支持 PNG/JPG/WEBP/GIF，单图最多 2000 万像素。')
        image.verify()
    return str(resolved)


def page_image_inputs(page, project, file_service, primary, extras=None, *, editing=False, temp_dir=None):
    """Resolve only DB-associated assets and this request's private uploaded files."""
    from models import Material, ProjectTemplateAsset
    from services.task_manager import resolve_page_template
    if not project or page.project_id != project.id:
        raise ValueError('页面不属于当前作品。')
    if page.template_asset and page.template_asset.project_id != project.id:
        raise ValueError('模板不属于当前作品。')
    root = Path(file_service.upload_folder).resolve()
    project_root = root / project.id
    expected = (file_service.get_absolute_path(page.generated_image_path) if editing and page.generated_image_path
                else resolve_page_template(page, project, file_service)[0] if not editing else None)
    if editing and not expected:
        raise ValueError('请先生成或导入当前页图片。')
    if primary and (not expected or Path(primary).resolve() != Path(expected).resolve()):
        raise ValueError('参考图与当前页面绑定不一致。')
    main = validate_local_image(primary, project_root) if primary else None
    lookup = {}
    for item in Material.query.filter_by(project_id=project.id).all():
        absolute = file_service.get_absolute_path(item.relative_path)
        lookup[f'/files/{item.relative_path.replace(chr(92), "/")}'] = absolute
        lookup[str(Path(absolute).resolve())] = absolute
    for item in ProjectTemplateAsset.query.filter_by(project_id=project.id).all():
        absolute = file_service.get_absolute_path(item.image_path)
        lookup[item.get_image_url()] = absolute
        lookup[str(Path(absolute).resolve())] = absolute
    template = resolve_page_template(page, project, file_service)[0]
    if template:
        lookup[str(Path(template).resolve())] = template
    refs = []
    for value in extras or []:
        if not isinstance(value, str) or '://' in value or value.startswith('//'):
            raise ValueError('只支持已关联本作品的本地参考图，不支持外部 URL。')
        target = lookup.get(value)
        if not target and temp_dir:
            temp = Path(temp_dir).resolve()
            if temp.parent != root or not temp.name.startswith('pc-edit-'):
                raise ValueError('无效的任务临时目录。')
            if Path(value).resolve().parent == temp:
                target = validate_local_image(value, temp)
        if not target:
            raise ValueError('参考图尚未合法关联当前作品。')
        refs.append(validate_local_image(target, root))
    from services.ai_providers.copilot_limits import IMAGE_OPERATIONS
    operation = 'slide_edit' if editing else 'template_generate' if main else 'text_generate'
    paths = ([main] if main else []) + refs
    minimum, maximum = IMAGE_OPERATIONS[operation]
    if not minimum <= len(paths) <= maximum or sum(Path(p).stat().st_size for p in paths) > 20 * 2**20:
        raise ValueError('参考图片数量或总大小超限：生成最多 2 张素材图加 1 张模板，编辑最多原图加 2 张参考图，总计 20 MB。')
    return operation, main, refs


def call_page_image(ai_service, page, project, file_service, prompt, primary,
                    aspect_ratio, resolution, extras=None, *, editing=False,
                    original_description=None, temp_dir=None):
    """Runs in the actual worker; ContextVar does not rely on HTTP thread propagation."""
    from services.ai_providers.copilot_limits import image_operation_scope, require_execution
    kwargs = {'additional_ref_images': extras or None}
    if editing:
        kwargs['original_description'] = original_description
    call = ai_service.edit_image if editing else ai_service.generate_image
    if not product_mode():
        return call(prompt, primary, aspect_ratio, resolution, **kwargs)
    operation, primary, extras = page_image_inputs(page, project, file_service, primary, extras,
                                                  editing=editing, temp_dir=temp_dir)
    require_execution('image', operation)
    with image_operation_scope(operation, ([primary] if primary else []) + extras):
        kwargs['additional_ref_images'] = extras or None
        return call(prompt, primary, aspect_ratio, resolution, **kwargs)


@contextmanager
def material_region_scope(source, marked, temp_dir, upload_root):
    from services.ai_providers.copilot_limits import image_operation_scope, require_execution
    if not product_mode():
        yield
        return
    root, temp = Path(upload_root).resolve(), Path(temp_dir).resolve()
    if temp.parent != root:
        raise ValueError('素材临时目录与任务不匹配。')
    paths = [validate_local_image(p, temp) for p in (source, marked)]
    if sum(Path(p).stat().st_size for p in paths) > 20 * 2**20:
        raise ValueError('参考图片总大小上限为 20 MB。')
    require_execution('image', 'material_region_edit')
    with image_operation_scope('material_region_edit', paths):
        yield


def product_mode():
    return os.environ.get('PC_MODE') in ('preview', 'live')


def region_edit_available():
    return capability_status('image', 'material_region_edit')['available']


def check_region_edit_request(request):
    """Authorize before parsing/saving files, then bound the restored workflow."""
    error = execution_error('image', 'material_region_edit')
    if error:
        return error
    error = check_local_upload(request)
    if error:
        return error
    if (request.form.get('operation') != 'region_edit'
            or list(request.files.keys()) != ['source_image']
            or len(list(request.files.items(multi=True))) != 1):
        return {'error': {'message': '当前仅支持一张目标图的框选编辑。'}}, 400
    allowed = {'operation', 'prompt', 'aspect_ratio', 'apply_mode', 'selection'}
    if set(request.form) - allowed or len(request.form.get('prompt', '').encode('utf-8')) > 16000:
        return {'error': {'message': '编辑参数无效或指令过长。'}}, 400
    from PIL import Image
    file = request.files['source_image']
    try:
        selection = json.loads(request.form.get('selection', 'null'))
        fields = ('x', 'y', 'width', 'height', 'image_width', 'image_height')
        if not isinstance(selection, dict) or any(type(selection.get(k)) is not int for k in fields):
            raise ValueError()
        with Image.open(file.stream) as image:
            width, height = image.size
        x, y, w, h = (selection[k] for k in fields[:4])
        if (selection['image_width'] != width or selection['image_height'] != height
                or x < 0 or y < 0 or w <= 2 or h <= 2 or x + w > width or y + h > height):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        return {'error': {'message': '选区与目标图片不匹配，请重新框选。'}}, 400
    finally:
        file.stream.seek(0)
    return None


def check_slide_edit_input(request, data, files):
    """Only the page edit route accepts these bounded multipart/JSON fields."""
    from PIL import Image
    if request.content_length is None or request.content_length > 20 * 2**20:
        raise ValueError('编辑请求不得超过 20 MB。')
    if not isinstance(data, dict) or set(data) - {'edit_instruction', 'context_images', 'use_template', 'desc_image_urls'}:
        raise ValueError('不支持的编辑参数。')
    prompt = data.get('edit_instruction')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode('utf-8')) > 16000:
        raise ValueError('请填写编辑指令，长度不得超过 16000 字节。')
    context = data.get('context_images', {})
    if not isinstance(context, dict) or set(context) - {'use_template', 'desc_image_urls'}:
        raise ValueError('无效的参考图参数。')
    refs = context.get('desc_image_urls', data.get('desc_image_urls', []))
    if not isinstance(refs, list) or len(refs) + len(files) > 2:
        raise ValueError('当前页编辑最多使用两张附加参考图。')
    if set(request.files) - {'context_images'} or len(list(request.files.items(multi=True))) != len(files):
        raise ValueError('不支持的上传字段。')
    for file in files:
        try:
            if not (file.filename or '').lower().endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif')):
                raise ValueError()
            with Image.open(file.stream) as image:
                if image.format not in {'PNG', 'JPEG', 'WEBP', 'GIF'} or image.width * image.height > 20_000_000:
                    raise ValueError()
                image.verify()
        except Exception:
            raise ValueError('参考图片损坏、格式不支持或超过 2000 万像素。') from None
        finally:
            file.stream.seek(0)


def check_local_upload(request):
    """Bounded local imports; never contact a parser or model service."""
    import psutil
    from PIL import Image
    def error(message, status=400):
        return {'error': {'code': 'IMPORT_UNAVAILABLE', 'message': message}}, status
    if psutil.virtual_memory().available < float(os.getenv('PC_MIN_FREE_MEMORY_GIB', '6')) * 2**30:
        return error('当前本机可用内存不足，请稍后再导入。', 503)
    if request.content_length is None or request.content_length > 20 * 2**20:
        return error('本地验证暂支持 20 MB 以内的文件。', 413)
    if len(request.files) != 1:
        return error('请一次上传一个文件。')
    file = next(iter(request.files.values()))
    name = (file.filename or '').lower()
    pdf_route = request.path.endswith(('/renovation', '/upload-pdf'))
    try:
        if pdf_route:
            if not name.endswith('.pdf'):
                return error('当前本地环境未接入 PPT/PPTX 转换程序，请先导出 PDF 再上传。')
            import fitz
            with fitz.open(stream=file.read(), filetype='pdf') as document:
                if document.needs_pass or not 1 <= len(document) <= 10:
                    return error('本地验证暂支持未加密的 1–10 页 PDF。')
                if any(page.rect.width * page.rect.height * 4 > 12_000_000 for page in document):
                    return error('页面尺寸过大，请缩小 PDF 页面后再上传。')
        else:
            if not name.endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif')):
                return error('请上传 PNG、JPG、WEBP 或 GIF 图片。')
            with Image.open(file.stream) as image:
                if image.width * image.height > 20_000_000:
                    return error('图片超过 2000 万像素，请缩小后再上传。')
                image.verify()
    except Exception:
        return error('无法读取此文件，请检查文件格式或是否损坏。')
    finally:
        file.stream.seek(0)
    return None


def managed_provider(role):
    prefix = role.upper()
    values = {key: os.environ.get(f'{prefix}_{suffix}', '') for key, suffix in
              [('format', 'MODEL_SOURCE'), ('api_key', 'API_KEY'), ('api_base', 'API_BASE')]}
    if os.environ.get('PC_MODE') != 'live' or not all(values.values()):
        raise ValueError(UNAVAILABLE)
    return values


class DisabledCaptionProvider:
    def generate_text(self, *args, **kwargs):
        raise ValueError(UNAVAILABLE)

    generate_with_image = generate_text
    generate_text_stream = generate_text


def install_product_routes(app, root):
    from flask import request, send_file
    from models import db, Settings

    @app.get('/api/product/status')
    def product_status():
        # No SDK construction or health probes. Verification is evidence, not a gate.
        capabilities = {'text': capability_status('text')}
        for operation in ('text_generate', 'template_generate', 'slide_edit', 'material_region_edit'):
            capabilities[operation] = capability_status('image', operation)
        for feature in ('renovation_parse', 'template_analysis', 'material_generate', 'video', 'quality_check'):
            capabilities[feature] = {'implemented': True, 'provider_supported': False, 'configured': False,
                                     'verified': False, 'available': False, 'reason': 'integration_required',
                                     'message': REASONS['integration_required']}
        text = capabilities['text']
        return {'success': True, 'data': {'available': text['available'],
                'message': text['message'], 'mode': os.getenv('PC_MODE'), 'capabilities': capabilities}}

    @app.get('/api/product/region-edit/status')
    def region_edit_status():
        available = region_edit_available()
        return {'success': True, 'data': {'available': available,
                'message': '' if available else UNAVAILABLE}}

    @app.route('/api/product/preferences', methods=['GET', 'PUT'])
    def product_preferences():
        settings = Settings.get_settings()
        if request.method == 'PUT':
            values = request.get_json(silent=True)
            if not isinstance(values, dict) or set(values) - PREFERENCE_FIELDS:
                return {'error': {'message': '不支持的创作参数。'}}, 400
            for key, value in values.items():
                if key == 'output_language':
                    if value not in ('zh', 'en', 'ja', 'auto'):
                        return {'error': {'message': '无效的输出语言。'}}, 400
                elif key == 'description_generation_mode':
                    if value not in ('streaming', 'parallel'):
                        return {'error': {'message': '无效的描述生成方式。'}}, 400
                elif (not isinstance(value, list) or len(value) > 12 or
                      any(not isinstance(item, str) or len(item) > 60 for item in value)):
                    return {'error': {'message': '无效的描述字段。'}}, 400
            from services.visitor_access import enabled, save_preferences
            if enabled():
                save_preferences(values)
                settings = Settings.get_settings()
            else:
                for key, value in values.items():
                    setattr(settings, key, value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
                db.session.commit()
        # Never call Settings.to_dict(): it includes managed connection metadata.
        return {'success': True, 'data': {
            'description_generation_mode': settings.description_generation_mode or 'streaming',
            'description_extra_fields': settings.get_description_extra_fields(),
            'image_prompt_extra_fields': settings.get_image_prompt_extra_fields(),
            'output_language': settings.output_language or 'zh',
        }}

    @app.get('/open-source/LICENSE')
    def product_license():
        return send_file(root / 'LICENSE', mimetype='text/plain')
