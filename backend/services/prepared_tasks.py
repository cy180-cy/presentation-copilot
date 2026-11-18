"""Freeze managed core tasks at enqueue; reuse AIService, guards and version save.

No new executor or recovery system. Upstream/non-core workers remain unchanged.
"""
import inspect
import json
import os
import shutil
from pathlib import Path
from datetime import datetime
from flask import g, has_request_context
from textwrap import dedent
from contextlib import ExitStack
from services.managed_resources import image_capacity, new_files_transaction
from models import db, Task, Page, Project, PageImageVersion, TaskTrace
from services import task_tracking as trace
from services.copilot_product import page_image_inputs, validate_local_image
from services.ai_providers.copilot_limits import image_operation_scope
from services.slide_consistency import plan_edit_sync, consistency_state

CORE = {'generate_images_task', 'generate_single_page_image_task',
        'edit_page_image_task', 'generate_descriptions_task'}


def current_single_page_snapshot(project, page, model, app):
    """Local prepare-only binding check; deliberately no Provider construction."""
    from types import SimpleNamespace
    from services.ai_service import AIService
    from services.file_service import FileService
    from services.task_manager import generate_single_page_image_task
    ai = AIService.__new__(AIService)
    ai.image_provider = SimpleNamespace(model=model)
    fs = FileService.__new__(FileService)
    fs.upload_folder = Path(app.config['UPLOAD_FOLDER'])
    args = single_page_generation_args(project, page, ai, fs, app)
    return prepare('quality-preview', generate_single_page_image_task, args, {}, snapshot_only=True)


def single_page_generation_args(project, page, ai, fs, app, use_template=True, language='zh'):
    """The HTTP and maintenance paths must prepare exactly the same inputs."""
    from services.task_manager import get_image_prompt_field_names
    outline, current_part, group = [], None, []
    for p in Page.query.filter_by(project_id=project.id).order_by(Page.order_index).all():
        content = p.get_outline_content()
        if not content:
            continue
        content = content.copy()
        if p.part:
            if current_part and current_part != p.part:
                outline.append({'part': current_part, 'pages': group})
                group = []
            current_part = p.part
            content.pop('part', None)
            group.append(content)
        else:
            if current_part:
                outline.append({'part': current_part, 'pages': group})
                current_part, group = None, []
            outline.append(content)
    if current_part:
        outline.append({'part': current_part, 'pages': group})
    requirements = project.extra_requirements or ''
    if project.template_style:
        requirements += f'\n\nppt页面风格描述：\n\n{project.template_style}'
    return (project.id, page.id, ai, fs, outline, use_template, project.image_aspect_ratio,
            app.config['DEFAULT_RESOLUTION'], app, requirements if requirements.strip() else None,
            language, get_image_prompt_field_names())


def prepare(task_id, function, args, kwargs, *, snapshot_only=False):
    if not os.getenv('PC_MODE') or function.__name__ not in CORE:
        return None
    from services import task_manager as tm
    from services.prompts import get_image_edit_prompt, get_page_description_prompt, IMAGE_GENERATION_PROMPT_VERSION
    bound = inspect.signature(function).bind(task_id, *args, **kwargs)
    bound.apply_defaults()
    values = bound.arguments
    app = values['app']
    plan = getattr(g, 'pc_page_plan', None) if has_request_context() else None
    with app.app_context():
        project = db.session.get(Project, values['project_id'])
        if not project:
            raise ValueError('作品不存在。')
        ai = trace.freeze_service(values['ai_service'])
        editing = function.__name__ == 'edit_page_image_task'
        text = function.__name__ == 'generate_descriptions_task'
        ids = [values['page_id']] if 'page_id' in values else values.get('page_ids')
        pages = Page.query.filter_by(project_id=project.id).order_by(Page.order_index).all()
        if ids:
            pages = [p for p in pages if p.id in ids]
            if len(pages) != len(set(ids)):
                raise ValueError('页面不属于当前作品。')
        fs = values.get('file_service')
        entries = []
        for page in pages:
            desc, outline = page.get_description_content(), page.get_outline_content()
            old = PageImageVersion.query.filter_by(page_id=page.id, image_path=page.generated_image_path).first()
            item = {'page_id': page.id, 'description': desc, 'outline': outline,
                    'source_image_path': page.generated_image_path,
                    'has_source_image': bool(page.generated_image_path),
                    'source_version_id': old.id if old else None,
                    'source_version': old.version_number if old else None,
                    'input_hash': trace.digest([desc, outline]), 'references': [],
                    'edit_instruction': values.get('edit_instruction'),
                    'aspect_ratio': values.get('aspect_ratio'), 'resolution': values.get('resolution')}
            if text:
                page_outline = ai.flatten_outline(values['outline'])[page.order_index]
                fields = ai._get_extra_field_names()
                item['parse_fields'] = ai._get_parseable_field_names()
                item['thinking_budget'] = ai._get_text_thinking_budget()
                item['prompt'] = get_page_description_prompt(values['project_context'], values['outline'],
                    page_outline, page.order_index + 1,
                    part_info=f"\nThis page belongs to: {page_outline['part']}" if 'part' in page_outline else '',
                    language=values['language'], detail_level=values['detail_level'], extra_fields=fields)
                item['operation'] = 'page_description'
            else:
                description = (desc or {}).get('text') or '\n'.join((desc or {}).get('text_content', []))
                description = tm._append_extra_fields(description, desc, values.get('image_prompt_field_names'))
                if editing:
                    primary = fs.get_absolute_path(page.generated_image_path) if page.generated_image_path else None
                    extras = values.get('additional_ref_images')
                    item['prompt'] = get_image_edit_prompt(values['edit_instruction'], values.get('original_description'))
                else:
                    primary, style = tm.resolve_page_template(page, project, fs)
                    if not values['use_template']:
                        primary = None
                    if not primary and not style:
                        raise ValueError('缺少模板图片或文字风格。')
                    extras = ai.extract_image_urls_from_markdown(description)
                    page_outline = dict(outline or {})
                    if page.part:
                        page_outline['part'] = page.part
                    item['prompt'] = ai.generate_image_prompt(values['outline'], page_outline, description,
                        page.order_index + 1, has_material_images=bool(extras),
                        extra_requirements=values.get('extra_requirements'), language=values.get('language'),
                        has_template=bool(primary), aspect_ratio=values['aspect_ratio'], page_style_text=style)
                operation, main, extra = page_image_inputs(page, project, fs, primary, extras,
                    editing=editing, temp_dir=values.get('temp_dir'))
                item['operation'] = operation
                item['has_primary'] = bool(main)
                item['template_id'] = page.template_asset_id if not editing else None
                private = Path(fs.upload_folder).resolve() / project.id / 'task-inputs' / task_id / page.id
                if not snapshot_only:
                    private.mkdir(parents=True, exist_ok=True)
                for index, path in enumerate(([main] if main else []) + extra):
                    payload = Path(path).read_bytes()
                    target = private / (str(index) + Path(path).suffix.lower())
                    if not snapshot_only:
                        from services.managed_resources import output
                        with output(target, len(payload)) as temp:
                            temp.write_bytes(payload)
                        validate_local_image(target, private)
                    item['references'].append({'path': str(target), 'sha256': trace.digest(payload)})
            item['prompt_hash'] = trace.digest(item['prompt'])
            entries.append(item)
        snapshot = {**trace.preset(ai, 'text' if text else 'image'), 'operation': function.__name__,
                    'prompt_version': 'pc007-v1' if text or editing else IMAGE_GENERATION_PROMPT_VERSION,
                    'queued_at': trace.now(),
                    'enable_thinking': getattr(ai, 'enable_image_reasoning' if not text else 'enable_text_reasoning', None),
                    'language': values.get('language'),
                    'pages': [{k: v for k, v in p.items() if k != 'source_image_path'} for p in entries]}
        if plan and not editing:
            snapshot.update(plan)
        for item in snapshot['pages']:
            item['references'] = [{'sha256': r['sha256']} for r in item['references']]
        if snapshot_only:
            # No task, DB write, frozen-file copy, provider initialization or send.
            return snapshot
        trace.create_trace(task_id, snapshot)
        from services.generation_budget import attach_task
        attach_task(task_id, plan if not editing else None)
        # JSON copy is private execution input; subsequent ORM/config mutations cannot affect it.
        return (run, (task_id, project.id, json.dumps(entries), ai, fs, app, text, values.get('temp_dir')), {})


def run(task_id, project_id, entries_json, ai, fs, app, text=False, temp_dir=None):
    from services import task_manager as tm
    entries = json.loads(entries_json)
    with app.app_context():
        task = db.session.get(Task, task_id)
        task.status = 'PROCESSING'
        task.set_progress({'total': len(entries), 'completed': 0, 'failed': 0})
        db.session.commit()
        try:
            for item in entries:
                with trace.scope(task_id, item['page_id'], app) as context, ExitStack() as resources, new_files_transaction():
                    page = db.session.get(Page, item['page_id'])
                    if not page or page.project_id != project_id:
                        raise ValueError('页面归属已变化。')
                    try:
                        if text:
                            with tm.text_resource_limiter.slot('tracked description'):
                                response = ai.text_provider.generate_text(item['prompt'], thinking_budget=item['thinking_budget'])
                            content, extra = ai._parse_extra_fields(dedent(response), item['parse_fields'])
                            result = {'text': content, 'generated_at': datetime.utcnow().isoformat()}
                            if extra:
                                result['extra_fields'] = extra
                        else:
                            resources.enter_context(image_capacity())
                            paths = []
                            for ref in item['references']:
                                path = validate_local_image(ref['path'], Path(fs.upload_folder) / project_id / 'task-inputs' / task_id / page.id)
                                if trace.digest(Path(path).read_bytes()) != ref['sha256']:
                                    raise ValueError('已固定的参考图发生变化，拒绝调用。')
                                paths.append(path)
                            main = paths[0] if item['has_primary'] else None
                            extra = paths[1:] if main else paths
                            with tm.image_resource_limiter.slot('tracked page image'), image_operation_scope(item['operation'], paths):
                                result = ai.generate_image(item['prompt'], main, item['aspect_ratio'], item['resolution'], additional_ref_images=extra or None)
                            if result is None:
                                raise ValueError('未取得图片。')
                            resources.callback(result.close)
                        # Input changed while queued/running: retain old correct output, never silently overwrite.
                        db.session.refresh(page)
                        if (page.generated_image_path != item['source_image_path'] or
                                page.get_description_content() != item['description'] or
                                page.get_outline_content() != item['outline']):
                            raise ValueError('input_changed')
                        if text:
                            page.set_description_content(result)
                            page.status = 'DESCRIPTION_GENERATED'
                            trace.link_output(page)
                        else:
                            consistency = 'generated_from_description'
                            if item['operation'] == 'slide_edit':
                                desc, outline, consistency = plan_edit_sync(item['description'], item['outline'], item['edit_instruction'])
                                if consistency == 'visual_only' and consistency_state(page) == 'needs_review':
                                    consistency = 'needs_review'
                                page.set_description_content(desc)
                                page.set_outline_content(outline)
                            tm.save_image_with_version(result, project_id, page.id, fs, page_obj=page,
                                                       consistency=consistency, commit=False)
                            db.session.flush()
                            version = PageImageVersion.query.filter_by(page_id=page.id, is_current=True).one()
                            trace.link_output(page, version)
                            result.close()
                        task.update_progress(completed=task.get_progress()['completed'] + 1)
                        db.session.commit()
                    except Exception:
                        db.session.rollback()
                        if context['call_id']:
                            trace.mark_save_failure()
                        else:
                            import uuid
                            trace.begin_call('text' if text else 'image', ai.text_provider if text else ai.image_provider, item['prompt'], uuid.uuid4().hex)
                            trace.update_call(status='local_rejected', reserved_credits=0, actual_credits=0,
                                              actual_credits_source='confirmed_not_sent', cost_status='not_sent',
                                              failure_stage='input_or_execution_guard', ended_at=trace.now())
                        raise
            task.status = 'COMPLETED'
            project = db.session.get(Project, project_id)
            project.status = 'DESCRIPTIONS_GENERATED' if text else 'COMPLETED'
        except Exception as exc:
            db.session.rollback()
            task = db.session.get(Task, task_id)
            task.status = 'FAILED'
            from services.managed_resources import ResourceLimit
            task.error_message = str(exc) if isinstance(exc, ResourceLimit) else '任务未完整完成，请查看任务详情；已保存页面和历史版本保留。'
            task.update_progress(failed=1)
        finally:
            if task.status == 'FAILED':
                # A failed/rejected job must not leave the controller's QUEUED
                # badge on a preserved, usable old image (or on unsent pages).
                for entry in entries:
                    retained = db.session.get(Page, entry['page_id'])
                    if retained and retained.project_id == project_id and retained.status in ('QUEUED', 'GENERATING', 'GENERATING_DESCRIPTION'):
                        retained.status = 'COMPLETED' if retained.generated_image_path else 'FAILED'
            task.completed_at = datetime.utcnow()
            db.session.commit()
            from services.generation_budget import finish_task
            finish_task(task_id, task.status == 'COMPLETED')
            # Uploaded crop has been copied/validated into immutable task inputs.
            if temp_dir:
                temp = Path(temp_dir).resolve()
                root = Path(fs.upload_folder).resolve()
                if temp.parent == root and temp.name.startswith('pc-edit-'):
                    shutil.rmtree(temp)
