"""Real Flask file responses through the production redaction hook; no providers."""
import ast
import json
import os
from pathlib import Path

import pytest
from flask import Flask, request, send_file


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Load the actual nested hook without booting a model runtime or database.
    source = Path(__file__).resolve().parents[1] / 'scripts/presentation_copilot.py'
    tree = ast.parse(source.read_text(encoding='utf-8'))
    hook = next(node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name == 'redact_response')
    app = Flask(__name__)
    namespace = dict(app=app, os=os, json=json, request=request, UNAVAILABLE='unavailable')
    exec(compile(ast.Module(body=[hook], type_ignores=[]), str(source), 'exec'), namespace)
    monkeypatch.setenv('TEXT_API_KEY', 'offline-redaction-sentinel')
    monkeypatch.delenv('IMAGE_API_KEY', raising=False)
    for name, data in [('source-offer.json', json.dumps({'url': 'https://github.com/example/project/tree/abc', 'version': 'test'})),
                       ('LICENSE', 'Synthetic license text')]:
        (tmp_path / name).write_text(data, encoding='utf-8')

    @app.get('/open-source/<name>')
    def public_file(name):
        return send_file(tmp_path / name, mimetype='application/json' if name.endswith('.json') else 'text/plain')

    @app.get('/api/synthetic-response')
    def synthetic_response():
        return {'message': 'offline-redaction-sentinel'}

    return app.test_client()


@pytest.mark.parametrize('name', ['source-offer.json', 'LICENSE'])
def test_public_file_response_does_not_fail_with_configured_key(client, name):
    response = client.get('/open-source/' + name)
    assert response.status_code == 200
    if name.endswith('.json'):
        assert response.json['version'] == 'test'
    else:
        assert response.text == 'Synthetic license text'


def test_normal_json_response_still_redacts_secrets(client):
    response = client.get('/api/synthetic-response')
    assert response.status_code == 200
    assert response.json['message'] == '[REDACTED]'
