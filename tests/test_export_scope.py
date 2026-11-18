"""Product scope regression: isolated visitors/data, fake providers, no sockets.
No retired conversion is run and no synthetic Editable file is produced.
"""
import ast
import builtins
import io
from pathlib import Path

import pytest
from pptx import Presentation

from test_visitor_isolation import app, isolated, trial, client, project, page, owner
from services import visitor_access as va
from services.ai_providers import copilot_limits as budget


@pytest.mark.parametrize('mode', ['preview', 'live'])
def test_removed_route_is_unregistered_and_never_initializes_or_queues(app, monkeypatch, mode):
    from models import Task
    from services import task_manager
    monkeypatch.setenv('PC_MODE', mode)
    assert not any('editable-pptx' in rule.rule for rule in app.url_map.iter_rules())
    assert not hasattr(task_manager, 'export_editable_pptx_with_recursive_analysis_task')
    from services.export_service import ExportService
    assert not hasattr(ExportService, 'create_editable_pptx_with_recursive_analysis')
    initialized = []
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.startswith(('services.image_editability', 'utils.pptx_builder', 'utils.pptx_math')):
            initialized.append(name)
            raise AssertionError('Retired export code must never load')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(task_manager.task_manager, 'submit_task', lambda *a, **kw: pytest.fail('No retired task may be queued'))
    a, b = client(app), client(app)
    pa, pb = project(a), project(b)
    pg, _ = page(app, a, pa)
    before = budget.budget_state()
    for c, pid in [(a, pa), (b, pb), (b, pa)]:
        for _ in range(2):
            response = c.post(f'/api/projects/{pid}/export/editable-pptx', json={'page_ids': [pg]})
            assert response.status_code in (404, 405), response.status_code
    assert a.get(f'/api/projects/{pa}').status_code == 200
    assert b.get(f'/api/projects/{pb}').status_code == 200
    assert 'editable_export' not in a.get('/api/product/status').json['data']['capabilities']
    with app.app_context(), va.scope(owner(app, pa)):
        assert Task.query.filter_by(project_id=pa).count() == 0
    assert initialized == [] and budget.budget_state() == before


def test_regular_exports_selected_version_and_download_ownership_survive(app):
    a, b = client(app), client(app)
    pid, other = project(a), project(b)
    pg, version = page(app, a, pid)
    before = budget.budget_state()
    original = a.get(f'/api/projects/{pid}').json
    for kind in ('pptx', 'pdf'):
        response = a.get(f'/api/projects/{pid}/export/{kind}?page_ids={pg}')
        assert response.status_code == 200, response.json
        url = response.json['data']['download_url']
        download = a.get(url)
        assert download.status_code == 200
        if kind == 'pptx':
            prs = Presentation(io.BytesIO(download.data))
            assert len(prs.slides) == 1
            assert len(prs.slides[0].shapes) == 1 and not prs.slides[0].shapes[0].has_text_frame
        else:
            import fitz
            with fitz.open(stream=download.data, filetype='pdf') as pdf: assert len(pdf) == 1
        download.close()
        assert b.get(url).status_code == 404
        assert b.get(f'/api/projects/{pid}/export/{kind}').status_code == 404
    assert a.get(f'/api/projects/{pid}').json == original
    versions = a.get(f'/api/projects/{pid}/pages/{pg}/image-versions').json['data']['versions']
    assert [v['version_id'] for v in versions] == [version]
    assert b.get(f'/api/projects/{other}').status_code == 200
    assert budget.budget_state() == before


def test_retired_format_not_offered_by_cli_or_release_manifest():
    from cli.banana_cli.models import ExportOptions
    from release_files import source_allowed
    # Typer is optional and absent in the managed Web environment. Inspect the
    # actual CLI command decorators without installing or mocking that package.
    root = Path(__file__).resolve().parents[1]
    commands = ast.parse((root / 'cli/banana_cli/commands/exports.py').read_text(encoding='utf-8'))
    registered = [d.args[0].value for n in commands.body if isinstance(n, ast.FunctionDef)
                  for d in n.decorator_list if isinstance(d, ast.Call)
                  and isinstance(d.func, ast.Attribute) and d.func.attr == 'command']
    assert 'editable-pptx' not in registered
    assert {'pptx', 'pdf', 'images'}.issubset(registered)
    with pytest.raises(ValueError): ExportOptions(formats=['editable_pptx'])
    assert set(ExportOptions.model_fields) == {'formats', 'filename_prefix', 'page_ids'}
    assert not source_allowed('.copilot/deliverables/pc012-offline-synthetic/presentation_synthetic_editable.pptx')
    assert not source_allowed('frontend/public/presentation_synthetic_editable.pptx')
    assert not source_allowed('docs/EDITABLE_PPTX_AUDIT.md')
