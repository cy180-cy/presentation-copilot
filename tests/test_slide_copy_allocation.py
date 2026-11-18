"""Prompt-only allocation, sockets forbidden; fixtures use a temporary DB/Provider."""
import json
from pathlib import Path
import pytest
from test_copilot_capabilities import app, isolated, models, page_data, png
from services.slide_consistency import allocate_visible_text
from services.prompts import get_image_generation_prompt
from test_task_tracking import queued


TITLE = '一周行动计划'
COPY = ['演示材料', TITLE, '每周检查15处用水点', '建立漏水反馈记录', '每周汇总改进情况']
TEXT = '--- 页面文字 ---\n' + '\n'.join(COPY) + '\n--- 页面文字结束 ---'


def allocated(prompt):
    return json.loads(prompt.split('<visible_copy>\n')[1].split('\n</visible_copy>')[0])


@pytest.mark.parametrize('separator', ['.', ')', '、'])
@pytest.mark.parametrize('style', ['每个卡片已有序号徽章', '普通列表，无序号徽章'])
def test_list_marker_has_one_owner_and_body_keeps_business_numbers(separator, style):
    points = ['2024年检查18处，版本v2.1', '使用C#，节约15%']
    source = '--- 页面文字 ---\n# 技术计划\n' + '\n'.join(
        f'{i}{separator} {point}' for i, point in enumerate(points, 1)) + '\n--- 页面文字结束 ---'
    prompt = get_image_generation_prompt(source, '', '', page_title='技术计划', page_style_text=style)
    messages = [item for item in allocated(prompt) if item['role'] == 'key_message']
    assert messages == [{'role': 'key_message', 'text': text, 'list_marker': f'{i}{separator}'}
                        for i, text in enumerate(points, 1)]
    assert '已有序号徽章或版式要求独立大号序号时' in prompt
    assert '没有这类序号位置时，将 list_marker 放在对应 text 行首' in prompt
    assert '两种位置互斥，每项序号仅显示一次' in prompt
    assert '不再另设装饰徽章编号' not in prompt
    assert all(prompt.count(text) == 1 for text in points)
    assert all(f'{i}{separator} {text}' not in prompt for i, text in enumerate(points, 1))


@pytest.mark.parametrize('count', [3, 4, 5])
@pytest.mark.parametrize('separator', ['.', ')', '、'])
def test_simple_markdown_heading_and_continuous_list_have_one_numbered_copy(count, separator):
    points = ['记忆价值与类型', '记忆生命周期', '常见问题与评估指标', '工程实践建议', '后续安排'][:count]
    numbered = [f'{i}{separator} {point}' for i, point in enumerate(points, 1)]
    source = '--- 页面文字 ---\n# 目录\n' + '\n'.join(numbered) + '\n--- 页面文字结束 ---'
    prompt = get_image_generation_prompt(source, '', '', page_title='目录',
        page_style_text='四块卡片的玻璃配色', extra_requirements='按内容安排版式')
    items = allocated(prompt)
    assert items == [{'role': 'heading', 'text': '目录'}] + [
        {'role': 'key_message', 'text': point, 'list_marker': f'{i}{separator}'}
        for i, point in enumerate(points, 1)]
    assert f'business_item_count="{count}"' in prompt and '# 目录' not in prompt
    assert prompt.count('<visible_copy>') == 1
    assert all(prompt.count(point) == 1 for point in points)
    assert '两种位置互斥，每项序号仅显示一次' in prompt and '只安排一套完整列表' in prompt
    assert '不提供业务内容或条目数量' in prompt
    assert source.startswith('--- 页面文字 ---\n# 目录')  # saved input is untouched


def test_ordered_copy_preserves_csharp_units_years_and_numbers_inside_sentences():
    lines = ['# 技术计划', '1. C#：2024年检查18处，节水15%，版本v2.1',
             '2. 每次使用500毫升，文字包含合法的井号#']
    source = '--- 页面文字 ---\n' + '\n'.join(lines) + '\n--- 页面文字结束 ---'
    prompt = get_image_generation_prompt(source, '', '', page_title='技术计划')
    assert [item['text'] for item in allocated(prompt)] == ['技术计划'] + [line[3:] for line in lines[1:]]
    assert [item.get('list_marker') for item in allocated(prompt)[1:]] == ['1.', '2.']
    assert all(prompt.count(line[3:]) == 1 for line in lines[1:])
    # Explicitly supplied footer and symbols are preserved, even on fallback.
    footer = '公司内部专用 | 2024年'
    prompt = get_image_generation_prompt(source + '\n' + footer, '', '', page_title='技术计划')
    assert footer in prompt
    literal = get_image_generation_prompt(source, '', '', page_title='技术计划',
        extra_requirements='请保留标题中的#符号')
    assert '<visible_copy>' not in literal and '# 技术计划' in literal


@pytest.mark.parametrize('body', [
    '# 目录\n1. 项目甲\n3. 项目乙',
    '# 目录\n1. 项目甲\n1. 项目乙',
    '# 目录\n2. 项目甲\n3. 项目乙',
    '# 目录\n1. 项目甲\n2) 项目乙',
    '# 目录\n1. 项目甲\n  2. 嵌套项目乙',
    '# 目录\n1. 项目甲\n## 另一组\n2. 项目乙',
    '# 目录\n1. 项目甲\n2. 项目乙\n另一组内容',
    '# 目录\n```cpp\n#include <stdio.h>\n```',
    '# 目录\n|主题|值|\n|甲|18|',
    '# 目录\n2024. 年度计划\n2025. 年度计划',
    '# 目录\n1.5毫升\n2.5毫升',
])
def test_ambiguous_lists_and_complex_markdown_fall_back_unchanged(body):
    source = '--- 页面文字 ---\n' + body + '\n--- 页面文字结束 ---'
    assert allocate_visible_text(source, '目录') == source
    prompt = get_image_generation_prompt(source, '', '', page_title='目录')
    assert '<visible_copy>' not in prompt and body in prompt


@pytest.mark.parametrize('instruction', ['请扩写目录', '允许重复展示各章节'])
def test_numbered_list_respects_explicit_expansion_or_repetition(instruction):
    source = '--- 页面文字 ---\n# 目录\n1. 项目甲\n2. 项目乙\n--- 页面文字结束 ---'
    prompt = get_image_generation_prompt(source, '', '', page_title='目录', extra_requirements=instruction)
    assert '<visible_copy>' not in prompt and '# 目录' in prompt and instruction in prompt


def test_flat_copy_has_one_slot_per_item_without_losing_numbers_or_layout():
    source = TEXT + '\n版式与重点：放大“15”处用水点，横向三栏\n配图与素材：流程图'
    result = allocate_visible_text(source, TITLE)
    assert [item['text'] for item in allocated(result)] == COPY
    assert [item['role'] for item in allocated(result)] == ['small_label', 'heading'] + ['key_message'] * 3
    assert result.endswith(source.split('--- 页面文字结束 ---')[1])
    assert allocate_visible_text(result, TITLE) == result
    assert source == TEXT + '\n版式与重点：放大“15”处用水点，横向三栏\n配图与素材：流程图'


@pytest.mark.parametrize('body', [
    '# 一周行动计划\n## 检查\n每周检查15处用水点',
    '一周行动计划\n|项目|数量|\n|检查|15|',
    '一周行动计划\n```\n检查15处\n```',
    '一周行动计划\n$15+x$',
    '一周行动计划\n1. 检查15处',
    '一周行动计划\n![示意图](/files/example.png)',
    '一周行动计划\n[详情](https://example.com)',
    '一周行动计划\n' + '\n'.join(['短句'] * 25),
])
def test_complex_or_long_content_keeps_original_structure(body):
    source = '--- 页面文字 ---\n' + body + '\n--- 页面文字结束 ---'
    assert allocate_visible_text(source, TITLE) == source


def test_no_guessing_missing_title_boundaries_or_intentional_repetition():
    for source, title in [(TEXT, None), (TEXT, '未知标题'), (TEXT + '\n允许重复展示', TITLE),
                          ('\n'.join(COPY), TITLE), (TEXT + TEXT, TITLE),
                          (TEXT.replace(TITLE, TITLE + '\n' + TITLE), TITLE)]:
        assert allocate_visible_text(source, title) == source
    prompt = get_image_generation_prompt(TEXT + '\n允许重复展示', '', '', page_title=TITLE)
    assert '<visible_copy>' not in prompt


@pytest.mark.parametrize('has_template', [True, False])
def test_prompt_preserves_copy_style_materials_and_explicit_exceptions(has_template):
    prompt = get_image_generation_prompt(TEXT, '不要渲染的大纲', '章节不是标题',
        page_title=TITLE, has_template=has_template, has_material_images=True,
        page_style_text='测试配色风格', extra_requirements='按用户指定位置留白', language='zh')
    assert [item['text'] for item in allocated(prompt)] == COPY
    for item in COPY:
        assert prompt.count(item) == 1
    assert '不要渲染的大纲' not in prompt and '章节不是标题' not in prompt
    assert '测试配色风格' in prompt and '按用户指定位置留白' in prompt
    assert '素材图片' in prompt and '无字图标' in prompt and '省略第二文字层' in prompt
    assert '除非明确要求重复' in prompt
    assert ('配色和设计语言和模板图片严格相似' in prompt) == has_template
    assert len(prompt) < 2400  # Keep a bounded addition; no duplicated copy inventory.


def test_actual_route_passes_allocated_prompt_and_same_template_without_mutating_copy(app, models, page_data):
    from models import db, Page, Project, Task, PageImageVersion
    project_id, page_id, old = page_data
    template = Path(app.config['UPLOAD_FOLDER']) / project_id / 'templates/allocation.png'
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_bytes(png('red').getvalue())
    desc = {'text': TEXT, 'extra_fields': {'版式与重点': '三栏；放大“15”处用水点',
            '配图与素材': '流程图', '演讲者备注': '不应进入图片提示词的备注'}}
    outline = {'title': TITLE, 'points': COPY[2:]}
    with app.app_context():
        page = db.session.get(Page, page_id)
        page.part = '其他章节'
        page.set_description_content(desc); page.set_outline_content(outline)
        db.session.get(Project, project_id).template_image_path = str(template.relative_to(app.config['UPLOAD_FOLDER']))
        db.session.commit()
    response = app.test_client().post(f'/api/projects/{project_id}/pages/{page_id}/generate/image',
                                     json={'force_regenerate': True})
    assert response.status_code == 202
    assert len(models[1]) == 1
    call = models[1][0]
    assert [item['text'] for item in allocated(call['prompt'])] == COPY
    assert '不应进入图片提示词的备注' not in call['prompt']
    assert call['operation'] == 'template_generate' and call['resolution'] == '2K'
    assert len(call['pixels']) == 1 and call['pixels'][0].getpixel((0, 0)) == (255, 0, 0)
    with app.app_context():
        assert db.session.get(Task, response.json['data']['task_id']).status == 'COMPLETED'
        page = db.session.get(Page, page_id)
        assert page.get_description_content()['text'] == TEXT
        assert page.get_description_content()['extra_fields'] == desc['extra_fields']
        assert page.get_outline_content() == outline
        assert PageImageVersion.query.filter_by(page_id=page_id).count() == 2
        assert Path(app.config['UPLOAD_FOLDER'], old).exists()


@pytest.mark.parametrize('count', [3, 4, 5])
@pytest.mark.parametrize('manual', [False, True])
def test_exact_business_count_excludes_title_label_and_preserves_copy(count, manual):
    points = ['每周检查18处用水点', '发现漏水及时反馈', '每周汇总改进情况', '按需取水500毫升', '节约用水15%'][:count]
    lines = ['演示材料', TITLE] + points
    source = ('页面文字：\n' + '\n'.join(lines)) if manual else (
        '--- 页面文字 ---\n' + '\n'.join(lines) + '\n--- 页面文字结束 ---')
    prompt = get_image_generation_prompt(source, '', '', page_title=TITLE,
        page_style_text='参考样例四块卡片，保留配色和线条')
    assert f'business_item_count="{count}"' in prompt
    assert [item['text'] for item in allocated(prompt)] == lines
    assert sum(item['role'] == 'key_message' for item in allocated(prompt)) == count
    assert all(prompt.count(line) == 1 for line in lines)
    assert '不提供业务内容或条目数量' in prompt and '不为填满卡片新增行动' in prompt
    assert '可减少内容区、合并空位置或留白' in prompt


@pytest.mark.parametrize('instruction', ['请扩写这些内容', '再新增两项建议', '补充节水行动建议', 'Please add two points', 'Expand the supplied points'])
def test_explicit_expansion_does_not_freeze_count(instruction):
    prompt = get_image_generation_prompt(TEXT, '', '', page_title=TITLE, extra_requirements=instruction)
    assert 'business_item_count=' not in prompt and '<visible_copy>' not in prompt
    assert instruction in prompt
    assert '除用户明确要求扩写或新增建议外' in prompt
    assert allocate_visible_text(TEXT + '\n' + instruction, TITLE) == TEXT + '\n' + instruction


@pytest.mark.parametrize('body', [TITLE+'\n|项目|数量|\n|检查|18|', TITLE+'\nx=18',
    TITLE+'\n每周检查18处用水点\n版式与重点：四栏', TITLE+'\n- 检查18处', TITLE+'\n# 检查18处'])
def test_manual_complex_or_mixed_layout_has_no_guessed_count(body):
    source = '页面文字：\n' + body
    assert allocate_visible_text(source, TITLE) == source


def test_prepared_manual_copy_snapshot_reaches_provider_unchanged(app, page_data, queued):
    from models import db, Project, Page, Task, TaskTrace, PageImageVersion
    from test_task_tracking import enqueue, details
    from services.task_tracking import digest
    from services.prompts import IMAGE_GENERATION_PROMPT_VERSION
    project, page_id, old = page_data
    source = '页面文字：\n校园节水行动\n每周检查18处用水点\n发现漏水及时反馈\n每周汇总改进情况'
    template = Path(app.config['UPLOAD_FOLDER']) / project / 'template.png'
    template.write_bytes(png('red').getvalue())  # synthetic reference, not evidence of image quality
    with app.app_context():
        page = db.session.get(Page, page_id)
        page.generated_image_path = None  # first generation; old fixture version remains untouched
        page.set_description_content({'text': source})
        page.set_outline_content({'title': '校园节水行动', 'points': source.splitlines()[2:]})
        db.session.get(Project, project).template_image_path = str(template.relative_to(app.config['UPLOAD_FOLDER']))
        db.session.commit()
    task_id = enqueue(app, page_data)
    with app.app_context():
        snapshot = json.loads(db.session.get(TaskTrace, task_id).snapshot)
    item = snapshot['pages'][0]
    assert snapshot['prompt_version'] == IMAGE_GENERATION_PROMPT_VERSION
    assert item['has_source_image'] is False and item['source_version'] is None
    assert item['operation'] == 'template_generate'
    assert item['description']['text'] == source
    assert 'business_item_count="3"' in item['prompt']
    assert item['prompt_hash'] == digest(item['prompt'])
    assert [i['text'] for i in allocated(item['prompt'])] == source.splitlines()[1:]
    queued[3]()
    assert len(queued[1]) == 1 and queued[1][0]['prompt'] == item['prompt']
    assert queued[1][0]['refs'] == [(255, 0, 0)] and queued[1][0]['resolution'] == '2K'
    record = details(app, project, task_id=task_id)
    assert record['snapshot']['pages'][0]['has_source_image'] is False
    assert record['operation'] == 'GENERATE_PAGE_IMAGE'
    assert len(record['calls']) == 1 and len(record['outputs']) == 1
    assert record['calls'][0]['call_id'] == record['outputs'][0]['call_id']
    with app.app_context():
        assert db.session.get(Task, task_id).status == 'COMPLETED'
        assert db.session.get(Page, page_id).get_description_content()['text'] == source
        assert PageImageVersion.query.filter_by(page_id=page_id).count() == 2
        assert Path(app.config['UPLOAD_FOLDER'], old).exists()
