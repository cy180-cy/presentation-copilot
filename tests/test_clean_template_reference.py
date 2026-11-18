"""PC-015.2: shipped static reference -> upload -> frozen task -> mock Provider.

No OCR or visual quality claim is made by these structural/pixel assertions.
"""
import hashlib
import io
import json
from pathlib import Path

from PIL import Image
from test_visitor_isolation import app, isolated, trial, client, project, page, owner, call
from test_task_tracking import queued
from services import visitor_access as va

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'frontend/public/templates'
CLEAN = ASSETS / 'template_glass_clean_v1.png'
CLEAN_HASH = 'ce500cf9966ad04500d3829dbcfb38473be3fa18b589b81f1d64244ca324ea8e'
ORIGINAL_HASH = 'ef8c912a79e78ae879d3cb9c6e98c5a9d17841900394f215e184ebdd5619d6c4'
POINTS = ['1. 记忆价值与类型', '2. 记忆生命周期', '3. 常见问题与评估指标', '4. 工程实践建议']
TEXT = '--- 页面文字 ---\n# 目录\n' + '\n'.join(POINTS) + '\n--- 页面文字结束 ---'


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


def test_clean_asset_is_independent_and_preserves_unmasked_style_regions():
    assert sha(CLEAN.read_bytes()) == CLEAN_HASH
    original = ASSETS / 'template_glass.png'
    assert sha(original.read_bytes()) == ORIGINAL_HASH
    with Image.open(original) as old, Image.open(CLEAN) as new, Image.open(
            ASSETS / 'template_glass_clean_v1-thumb.webp') as thumb:
        assert old.size == new.size == (2752, 1536)
        assert thumb.size == (640, 357)
        # Rainbow ornament and glass border outside the manually edited regions.
        for box in [(0, 0, 260, 410), (900, 1080, 1120, 1360), (1400, 250, 2300, 350)]:
            assert old.crop(box).tobytes() == new.crop(box).tobytes()
        assert old.crop((360, 460, 800, 670)).tobytes() != new.crop((360, 460, 800, 670)).tobytes()


def test_actual_visitor_upload_prepared_snapshot_and_worker_use_clean_bytes(app, queued, monkeypatch):
    from models import db, Page, Project, Task, TaskTrace
    from services import prompts
    from services.task_tracking import digest
    from services.ai_providers import copilot_limits as limits
    a, b = client(app), client(app)
    pid = project(a)
    pg, _ = page(app, a, pid)
    with app.app_context(), va.scope(owner(app, pid)):
        obj = db.session.get(Page, pg)
        obj.set_outline_content({'title': '目录', 'points': [p.split('. ', 1)[1] for p in POINTS]})
        obj.set_description_content({'text': TEXT, 'extra_fields': {
            '配图与素材': '四象限编号卡片图：四个主题以图标和编号串联，表现目录结构。',
            '版式与重点': '居中标题，下方2×2卡片网格；视觉重点是四个编号卡片及其先后关系。',
            '演讲者备注': '此备注不应进入图片文案'}})
        db.session.commit()
    payload = CLEAN.read_bytes()
    response = a.post(f'/api/projects/{pid}/template', data={
        'template_image': (io.BytesIO(payload), CLEAN.name)}, content_type='multipart/form-data')
    assert response.status_code == 200, response.json
    url = response.json['data']['template_image_url']
    assert b.get(url).status_code == 404
    assert b.post(f'/api/projects/{pid}/template', data={
        'template_image': (io.BytesIO(payload), CLEAN.name)}).status_code == 404
    ai, calls, jobs, run = queued
    pixels = []
    class Capture(type(ai.image_provider)):
        def generate_image(self, prompt, ref_images=None, **kwargs):
            pixels.append([sha(im.convert('RGB').tobytes()) for im in ref_images or []])
            return super().generate_image(prompt, ref_images=ref_images, **kwargs)
    ai.image_provider = Capture()
    response = call(a, pid, pg, key='pc0152-clean-reference')
    assert response.status_code == 202, response.json
    task_id = response.json['data']['task_id']
    with app.app_context(), va.scope(owner(app, pid)):
        snapshot_bytes = db.session.get(TaskTrace, task_id).snapshot
        snapshot = json.loads(snapshot_bytes)
        uploaded = Path(app.config['UPLOAD_FOLDER']) / db.session.get(Project, pid).template_image_path
    item = snapshot['pages'][0]
    assert sha(uploaded.read_bytes()) == CLEAN_HASH
    assert item['references'] == [{'sha256': CLEAN_HASH}]
    assert snapshot['prompt_version'] == prompts.IMAGE_GENERATION_PROMPT_VERSION
    assert item['description']['text'] == TEXT  # original input is traceable
    assert item['prompt_hash'] == digest(item['prompt'])
    assert 'business_item_count="4"' in item['prompt'] and '# 目录' not in item['prompt']
    visible = json.loads(item['prompt'].split('<visible_copy>\n')[1].split('\n</visible_copy>')[0])
    assert visible[1:] == [{'role': 'key_message', 'text': point.split('. ', 1)[1],
                           'list_marker': f'{i}.'} for i, point in enumerate(POINTS, 1)]
    assert '两种位置互斥，每项序号仅显示一次' in item['prompt']
    assert all(item['prompt'].count(point.split('. ', 1)[1]) == 1 for point in POINTS)
    assert '此备注不应进入图片文案' not in item['prompt']
    assert '公司内部专用' not in item['prompt'] and '关键特征' not in item['prompt']
    frozen = Path(app.config['UPLOAD_FOLDER']) / pid / 'task-inputs' / task_id / pg / '0.png'
    assert sha(frozen.read_bytes()) == CLEAN_HASH
    # Subsequent configuration/reference changes cannot refill a frozen task.
    uploaded.write_bytes((ASSETS / 'template_glass.png').read_bytes())
    monkeypatch.setattr(prompts, 'IMAGE_GENERATION_PROMPT_VERSION', 'later-config-not-for-this-task')
    monkeypatch.setattr(prompts, 'get_image_generation_prompt', lambda *a, **kw: 'later prompt')
    run()
    assert len(calls) == 1 and calls[0]['prompt'] == item['prompt']
    with Image.open(CLEAN) as ref:
        assert pixels == [[sha(ref.convert('RGB').tobytes())]]
    assert calls[0]['resolution'] == '2K'
    with app.app_context(), va.scope(owner(app, pid)):
        assert db.session.get(Task, task_id).status == 'COMPLETED'
        assert db.session.get(TaskTrace, task_id).snapshot == snapshot_bytes
        assert db.session.get(Page, pg).get_description_content()['text'] == TEXT
    assert limits.budget_state()['image'] == 1
    assert limits.budget_state()['reserved_credits'] == 4
    assert sha(frozen.read_bytes()) == CLEAN_HASH


def test_edit_keeps_source_image_text_instead_of_substituting_clean_template(app, queued):
    from models import db, Page, Project, TaskTrace, Task
    a = client(app)
    pid = project(a)
    pg, _ = page(app, a, pid)
    original = (ASSETS / 'template_glass.png').read_bytes()
    with app.app_context(), va.scope(owner(app, pid)):
        obj = db.session.get(Page, pg)
        source = Path(app.config['UPLOAD_FOLDER']) / obj.generated_image_path
        source.write_bytes(original)  # synthetic project using the public sample
        obj.set_description_content({'text': TEXT + '\n用户要求保留：公司内部专用 | 2024年'})
        clean = Path(app.config['UPLOAD_FOLDER']) / pid / 'clean.png'
        clean.write_bytes(CLEAN.read_bytes())
        db.session.get(Project, pid).template_image_path = str(clean.relative_to(app.config['UPLOAD_FOLDER']))
        db.session.commit()
    revision = a.get(f'/api/projects/{pid}/input-revision').json['data']['revision']
    response = a.post(f'/api/projects/{pid}/pages/{pg}/edit/image', json={'edit_instruction': '将背景改为蓝色'},
        headers={'Idempotency-Key': 'pc0152-edit-source', 'X-PC-Input-Version': revision})
    assert response.status_code == 202, response.json
    task_id = response.json['data']['task_id']
    with app.app_context(), va.scope(owner(app, pid)):
        snapshot = json.loads(db.session.get(TaskTrace, task_id).snapshot)
    item = snapshot['pages'][0]
    assert snapshot['prompt_version'] == 'pc007-v1'
    assert item['operation'] == 'slide_edit'
    assert item['references'] == [{'sha256': ORIGINAL_HASH}]
    assert '# 目录' in item['prompt'] and '公司内部专用 | 2024年' in item['prompt']
    assert '<visible_copy>' not in item['prompt']
    queued[3]()
    assert len(queued[1]) == 1 and queued[1][0]['prompt'] == item['prompt']
    assert source.read_bytes() == original
    with app.app_context(), va.scope(owner(app, pid)):
        assert db.session.get(Task, task_id).status == 'COMPLETED'
