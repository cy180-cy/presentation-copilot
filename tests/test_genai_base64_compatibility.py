"""Inspect final managed HTTP bodies with the real SDK and offline transport."""
import base64
import copy
import io
import json
from pathlib import Path
import sys

import httpx
from PIL import Image
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'backend')]
import presentation_copilot as runner
from config import Config
from services import task_tracking
from services.ai_providers import copilot_limits as limits

IMAGE_URL = 'https://tokenflux.dev/v1beta/models/nano-banana-2:generateContent'


@pytest.fixture
def wire(monkeypatch, tmp_path):
    import requests

    for name in ('PC_MODE', 'PC_ADMIN_PAUSED', 'PC_ADMIN_APPLYING'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(limits, '_root', lambda: tmp_path)
    for key, value in runner.PRESET.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv('IMAGE_API_KEY', 'offline-only')
    monkeypatch.setenv('PC_BUDGET_CREDITS', '8')
    monkeypatch.setenv('PC_IMAGE_OPERATIONS', 'template_generate')
    monkeypatch.setattr(Config, 'GENAI_MAX_RETRIES', 0)
    monkeypatch.setattr(Config, 'GENAI_SDK_ATTEMPTS', 1)
    monkeypatch.setattr(Config, 'MODEL_MAX_OUTPUT_TOKENS', 4096)
    monkeypatch.setattr(httpx.Client, 'send', httpx.Client.send)
    monkeypatch.setattr(requests.Session, 'request', requests.Session.request)
    sent, captured = [], []
    monkeypatch.setattr(task_tracking, 'sent', lambda: sent.append(True))
    output = io.BytesIO()
    Image.new('RGB', (6, 4), 'blue').save(output, format='PNG')

    def respond(request):
        captured.append(request)
        return httpx.Response(200, json={'candidates': [{'content': {
            'role': 'model', 'parts': [{'inlineData': {'mimeType': 'image/png',
            'data': base64.b64encode(output.getvalue()).decode('ascii')}}]}}]})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request',
                        lambda _self, request: transport.handle_request(request))
    runner.install_network_guard(True, managed=True)
    monkeypatch.setenv('PC_MODE', 'live')
    return captured, sent


def test_real_provider_clean_template_uses_standard_base64(wire):
    from google.genai import _transformers
    from services.ai_providers.image.genai_provider import GenAIImageProvider

    captured, sent = wire
    with Image.open(ROOT / 'frontend/public/templates/template_glass_clean_v1.png') as image:
        original = _transformers.pil_to_blob(image).data
        assert '-' in base64.urlsafe_b64encode(original).decode('ascii')
        provider = GenAIImageProvider(model='nano-banana-2', api_key='offline-only',
                                     api_base='https://tokenflux.dev')
        try:
            with limits.image_operation_scope('template_generate', ['owned-template.png']):
                result = provider.generate_image('目录：四项业务内容', ref_images=[image],
                                                 enable_thinking=False)
            assert result.size == (6, 4)
        finally:
            provider.client.close()
    assert len(captured) == 1
    assert len(sent) == 1
    assert limits.budget_state()['image'] == 1
    assert limits.budget_state()['reserved_credits'] == 4
    request = captured[0]
    body = json.loads(request.content)
    data = body['contents'][0]['parts'][0]['inlineData']['data']
    assert base64.b64decode(data, validate=True) == original
    assert data == base64.b64encode(original).decode('ascii')
    assert body['contents'][0]['parts'][-1]['text'] == '目录：四项业务内容'
    assert body['generationConfig']['imageConfig'] == {'aspectRatio': '16:9', 'imageSize': '2K'}
    assert request.headers['x-goog-api-key'] == 'offline-only'


def test_real_sdk_multiple_byte_references_and_response(wire):
    from google.genai import types
    from services.ai_providers.genai_client import make_genai_client

    captured, sent = wire
    images = [b'\xfb\xff\x00\xfa', b'\xff\xee\xdd\xfb\xff']
    client = make_genai_client(vertexai=False, api_key='offline-only', api_base='https://tokenflux.dev')
    try:
        response = client.models.generate_content(model='nano-banana-2', contents=[
            types.Part.from_bytes(data=data, mime_type=mime)
            for data, mime in zip(images, ['image/png', 'image/jpeg'])
        ] + ['保留提示词中的 -_ 和 +/'])
        with Image.open(io.BytesIO(response.parts[0].as_image().image_bytes)) as image:
            assert image.size == (6, 4)
    finally:
        client.close()
    parts = json.loads(captured[0].content)['contents'][0]['parts']
    assert [base64.b64decode(p['inlineData']['data'], validate=True) for p in parts[:-1]] == images
    assert parts[-1]['text'] == '保留提示词中的 -_ 和 +/'
    assert len(captured) == len(sent) == 1


def test_only_inline_image_fields_change_and_headers_survive(wire):
    captured, sent = wire
    payload = {'contents': [{'role': 'user', 'parts': [
        {'text': 'C#、2024年、-_+/ 以及原始 data:image/png;base64,-_8='},
        {'inlineData': {'mimeType': 'image/png', 'data': '-_8='}},
        {'inlineData': {'mimeType': 'application/pdf', 'data': '-_8='}},
    ]}], 'generationConfig': {'imageConfig': {'imageSize': '2K', 'aspectRatio': '16:9'}}}
    expected = copy.deepcopy(payload)
    expected['contents'][0]['parts'][1]['inlineData']['data'] = '+/8='
    request = httpx.Request('POST', IMAGE_URL, content=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json', 'x-goog-api-key': 'offline-key',
                 'Authorization': 'Bearer offline-only', 'x-test': 'preserve'},
        extensions={'timeout': {'read': 5}, 'offline-marker': 'preserve'})
    with httpx.Client() as client:
        client.send(request)
    actual = captured[0]
    assert json.loads(actual.content) == expected
    assert actual.method == request.method and actual.url == request.url
    assert actual.extensions == request.extensions
    for name in ('content-type', 'authorization', 'x-goog-api-key', 'x-test'):
        assert actual.headers[name] == request.headers[name]
    assert int(actual.headers['content-length']) == len(actual.content)
    assert len(captured) == len(sent) == 1


@pytest.mark.parametrize('path,payload', [
    (IMAGE_URL, {'contents': [{'parts': [{'inlineData': {'mimeType': 'image/png', 'data': '+/8='}}]}]}),
    (IMAGE_URL, {'contents': [{'parts': [{'text': '普通文本 -_+/'}]}]}),
    ('https://tokenflux.dev/v1/chat/completions', {'model': 'deepseek-flash',
        'messages': [{'role': 'user', 'content': '文字和 -_+/ 原样保留'}]}),
])
def test_standard_images_and_text_requests_are_untouched(wire, path, payload):
    captured, sent = wire
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
    with httpx.Client() as client:
        client.post(path, content=body, headers={'content-type': 'application/json'})
    assert captured[0].content == body
    assert len(captured) == len(sent) == 1


@pytest.mark.parametrize('data', ['!!!!', 'data:image/png;base64,-_8=', 'abc', '', None, 42, '非ASCII'])
def test_invalid_image_encoding_never_reaches_transport_or_sent_marker(wire, data):
    captured, sent = wire
    with httpx.Client() as client, pytest.raises(ValueError, match='参考图片编码'):
        client.post(IMAGE_URL, json={'contents': [{'parts': [
            {'inlineData': {'mimeType': 'image/png', 'data': data}}]}]})
    assert captured == sent == []


@pytest.mark.parametrize('mode,flag,url', [
    ('preview', None, IMAGE_URL),
    ('live', 'PC_ADMIN_PAUSED', IMAGE_URL),
    ('live', 'PC_ADMIN_APPLYING', IMAGE_URL),
    ('live', None, IMAGE_URL.replace('tokenflux.dev', 'unrelated.invalid')),
    ('live', None, IMAGE_URL.replace('nano-banana-2:', 'nano-banana-2.1:')),
])
def test_existing_network_guards_run_before_normalization(wire, monkeypatch, mode, flag, url):
    captured, sent = wire
    monkeypatch.setenv('PC_MODE', mode)
    if flag:
        monkeypatch.setenv(flag, '1')
    with httpx.Client() as client, pytest.raises(RuntimeError, match='出站'):
        client.post(url, json={'contents': [{'parts': [
            {'inlineData': {'mimeType': 'image/png', 'data': '!!!!'}}]}]})
    assert captured == sent == []
