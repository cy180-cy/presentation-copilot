"""PC-006: isolated files/DB, synthetic image Provider, sockets forbidden."""
import copy
import json
from pathlib import Path
import pytest
from test_copilot_capabilities import app, isolated, models, page_data, png
from services.slide_consistency import normalize_visible_text, plan_edit_sync


def description():
    return {'text': '--- 页面文字 ---\n一周行动计划\n每周检查12处用水点\n建立漏水反馈记录\n每周汇总改进情况\n--- 页面文字结束 ---',
            'extra_fields': {'版式与重点': '放大“12”处用水点数字', '演讲者备注': '每周检查12处用水点'},
            'generated_at': '2026-10-15'}


INSTRUCTION = '将每周检查12处用水点改为每周检查15处用水点，其他文字和整体风格尽量保持一致。'


def test_exact_duplicate_normalization_preserves_numbers_and_near_matches():
    text = '--- 页面文字 ---\n# 建立漏水反馈记录\n建立漏水反馈记录\n每周检查12处用水点\n每周检查15处用水点\n--- 页面文字结束 ---\n版式与重点：三栏'
    normalized, warnings = normalize_visible_text(text)
    assert normalized.count('建立漏水反馈记录') == 1
    assert '12处' in normalized and '15处' in normalized
    assert warnings == ['exact_duplicate', 'near_duplicate']
    assert normalize_visible_text(normalized)[0] == normalized
    repeated, _ = normalize_visible_text(text + '\n允许重复展示')
    assert repeated.count('建立漏水反馈记录') == 2


def test_number_sync_updates_notes_layout_outline_but_not_metadata():
    original = description()
    outline = {'title': '计划', 'points': ['每周检查12处用水点']}
    desc, updated, state = plan_edit_sync(original, outline, INSTRUCTION)
    assert state == 'synced_by_instruction'
    assert '12' not in json.dumps({k:v for k,v in desc.items() if k != 'generated_at'}, ensure_ascii=False)
    assert desc['extra_fields']['版式与重点'] == '放大“15”处用水点数字'
    assert updated['points'] == ['每周检查15处用水点']
    assert desc['generated_at'] == original['generated_at']
    assert original == description()


@pytest.mark.parametrize('instruction', ['把12改为15', '请优化内容', '将每周检查12处用水点改为15处，并删除其他内容'])
def test_ambiguous_edit_preserves_all_text(instruction):
    desc, outline, state = plan_edit_sync(description(), {}, instruction)
    assert state == 'needs_review' and desc == description() and outline == {}


def test_visual_edit_and_conflicting_numbers():
    desc, _, state = plan_edit_sync(description(), {}, '将背景改为蓝色')
    assert desc == description() and state == 'visual_only'
    desc, _, state = plan_edit_sync(description(), {'points': ['另有15个班级']}, INSTRUCTION)
    assert desc == description() and state == 'needs_review'
    desc, _, state = plan_edit_sync(description(), {'points': ['另有12个班级']}, INSTRUCTION)
    assert desc == description() and state == 'needs_review'


def test_literal_text_replacement():
    desc, _, state = plan_edit_sync(description(), {}, '将建立漏水反馈记录改为及时登记漏水问题')
    assert state == 'synced_by_instruction' and '及时登记漏水问题' in desc['text']


def test_legacy_fields_warn_without_deleting_layout():
    text = '建立漏水反馈记录\n建立漏水反馈记录\n版式与重点：三栏'
    normalized, warnings = normalize_visible_text(text)
    assert normalized == text and warnings == ['exact_duplicate']


def test_generation_prompt_has_single_visible_copy_and_keeps_template():
    from services.prompts import get_image_generation_prompt
    text = description()['text'].replace('建立漏水反馈记录', '# 建立漏水反馈记录\n建立漏水反馈记录')
    prompt = get_image_generation_prompt(text, '大纲中重复的内容', '计划', has_template=True)
    assert prompt.count('建立漏水反馈记录') == 1
    assert '配色和设计语言和模板图片严格相似' in prompt
    assert '大纲中重复的内容' not in prompt and '4K' not in prompt
    assert '不因页码为 1' in prompt and '唯一可见文案来源' in prompt


@pytest.mark.parametrize('fail', [False, True])
def test_actual_edit_route_sync_versions_other_page_and_export(app, models, page_data, monkeypatch, fail):
    from models import db, Page, PageImageVersion, Task
    from services.task_manager import save_image_with_version
    from services import FileService
    from PIL import Image
    from controllers import export_controller
    project, page_id, old = page_data
    with app.app_context():
        page = db.session.get(Page, page_id)
        page.set_description_content(description())
        page.set_outline_content({'title': '计划', 'points': ['每周检查12处用水点']})
        other = Page(project_id=project, order_index=1)
        other.set_description_content({'text': '第一页不变'})
        db.session.add(other); db.session.flush()
        save_image_with_version(Image.new('RGB', (400,300), 'red'), project, other.id, FileService(app.config['UPLOAD_FOLDER']), other)
        other_id, other_path, other_desc = other.id, other.generated_image_path, other.description_content
        old_bytes = Path(app.config['UPLOAD_FOLDER'], old).read_bytes()
        other_bytes = Path(app.config['UPLOAD_FOLDER'], other_path).read_bytes()
    models[0].fail = fail
    client = app.test_client()
    response = client.post(f'/api/projects/{project}/pages/{page_id}/edit/image', json={'edit_instruction': INSTRUCTION})
    assert response.status_code == 202
    with app.app_context():
        page = db.session.get(Page, page_id)
        task = db.session.get(Task, response.json['data']['task_id'])
        assert task.status == ('FAILED' if fail else 'COMPLETED')
        assert PageImageVersion.query.filter_by(page_id=page_id).count() == (1 if fail else 2)
        other = db.session.get(Page, other_id)
        assert other.description_content == other_desc and other.generated_image_path == other_path
        assert Path(app.config['UPLOAD_FOLDER'], other_path).read_bytes() == other_bytes
        assert Path(app.config['UPLOAD_FOLDER'], old).read_bytes() == old_bytes
        if fail:
            assert page.get_description_content() == description() and page.generated_image_path == old
            return
        assert '15处' in page.get_description_content()['text']
        assert page.to_dict()['description_image_state'] == 'synced_by_instruction'
        assert task.get_progress()['edit_instruction'] == INSTRUCTION
        assert task.get_progress()['description_before'] == description()
        latest = page.generated_image_path
        versions = PageImageVersion.query.filter_by(page_id=page_id).order_by(PageImageVersion.version_number).all()
        old_id, new_id = versions[0].id, versions[1].id
    assert len(models[1]) == 1 and models[1][0]['operation'] == 'slide_edit'
    assert '12' in models[1][0]['prompt'] and '15' in models[1][0]['prompt']
    assert models[1][0]['pixels'][0].getpixel((0,0)) == (255,255,255)
    # Actual export routes; capture exactly which local images reach exporters.
    captures = []
    def export(*args, **kwargs):
        captures.append((args, kwargs))
        return None
    monkeypatch.setattr(export_controller.ExportService, 'create_pptx_from_images', export)
    monkeypatch.setattr(export_controller.ExportService, 'create_pdf_from_images', export)
    for version_id, path, state in [(old_id, old, 'needs_review'), (new_id, latest, 'synced_by_instruction')]:
        response = client.post(f'/api/projects/{project}/pages/{page_id}/image-versions/{version_id}/set-current')
        assert response.status_code == 200 and response.json['data']['description_image_state'] == state
        for ext in ('pptx', 'pdf'):
            client.get(f'/api/projects/{project}/export/{ext}')
            args, kwargs = captures[-1]
            paths = kwargs.get('image_paths', args[0] if args else [])
            assert Path(paths[0]) == Path(app.config['UPLOAD_FOLDER'], path)
            assert Path(paths[1]) == Path(app.config['UPLOAD_FOLDER'], other_path)
    assert len(captures) == 4 and len(models[1]) == 1


def test_save_failure_rolls_back_description_and_current_version(app, models, page_data, monkeypatch):
    from models import db, Page, PageImageVersion, Task
    from services import FileService
    project, page_id, old = page_data
    with app.app_context():
        page = db.session.get(Page, page_id); page.set_description_content(description()); db.session.commit()
    def fail(*args, **kwargs): raise OSError('offline disk failure')
    monkeypatch.setattr(FileService, 'save_cached_image', fail)
    response = app.test_client().post(f'/api/projects/{project}/pages/{page_id}/edit/image', json={'edit_instruction': INSTRUCTION})
    with app.app_context():
        page = db.session.get(Page, page_id)
        assert page.generated_image_path == old and page.get_description_content() == description()
        assert PageImageVersion.query.filter_by(page_id=page_id, is_current=True).one().version_number == 1
        assert PageImageVersion.query.filter_by(page_id=page_id).count() == 1
        assert db.session.get(Task, response.json['data']['task_id']).status == 'FAILED'


def test_concurrent_manual_save_is_not_overwritten(app, models, page_data, monkeypatch):
    from models import db, Page, PageImageVersion, Task
    project, page_id, old = page_data
    original_call = models[0].generate_image
    def edit_then_manual_save(*args, **kwargs):
        result = original_call(*args, **kwargs)
        page = db.session.get(Page, page_id)
        page.set_description_content({'text': '用户刚刚手动保存的新内容'})
        db.session.commit()
        return result
    monkeypatch.setattr(models[0], 'generate_image', edit_then_manual_save)
    response = app.test_client().post(f'/api/projects/{project}/pages/{page_id}/edit/image', json={'edit_instruction': INSTRUCTION})
    with app.app_context():
        page = db.session.get(Page, page_id)
        assert page.get_description_content()['text'] == '用户刚刚手动保存的新内容'
        assert page.generated_image_path == old
        assert PageImageVersion.query.filter_by(page_id=page_id).count() == 1
        assert db.session.get(Task, response.json['data']['task_id']).status == 'FAILED'
        assert page.to_dict()['description_image_state'] == 'needs_review'
