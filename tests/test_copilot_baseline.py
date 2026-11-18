"""No-network checks for PC-001. Mock images never count as generated results."""
import base64
import io
import json
import os
from pathlib import Path
import sys

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts")]
import presentation_copilot as runner
from services.ai_providers import copilot_limits as limits


@pytest.fixture
def isolated_gate(monkeypatch, tmp_path):
    monkeypatch.setattr(limits, "_root", lambda: tmp_path)
    monkeypatch.setenv("PC_MODE", "live")
    monkeypatch.setenv("PC_MODEL_ROLES", "text,image")
    for key, value in runner.PRESET.items(): monkeypatch.setenv(key, value)
    for role in ("TEXT", "IMAGE"): monkeypatch.setenv(f"{role}_API_KEY", "offline-test-only")
    monkeypatch.setenv("PC_BUDGET_CREDITS", "20")
    monkeypatch.setenv("PC_IMAGE_OPERATIONS", "text_generate")
    return tmp_path


def test_preview_cannot_call_model(monkeypatch):
    monkeypatch.setenv("PC_MODE", "preview")
    class Model:
        model = limits.MODELS["text"]
        @limits.model_call("text")
        def generate(self, prompt):
            pytest.fail("Underlying model must not run")
    with pytest.raises(RuntimeError, match="预算"):
        Model().generate("test")


def test_call_cap_and_input_cap(isolated_gate):
    class Model:
        model = limits.MODELS["text"]
        @limits.model_call("text")
        def generate(self, prompt):
            return "mock"
    model = Model()
    with pytest.raises(RuntimeError, match="32768"):
        model.generate("中" * 12000)
    for _ in range(6):
        assert model.generate("test") == "mock"
    with pytest.raises(RuntimeError, match="上限"):
        model.generate("test")
    assert json.loads((isolated_gate / "call-budget.json").read_text())["text"] == 6


def test_uncertain_failure_pauses_following_calls(isolated_gate):
    calls = []
    class Model:
        model = limits.MODELS["image"]
        @limits.model_call("image")
        def generate(self, prompt):
            calls.append(1)
            raise TimeoutError("mock timeout, unknown billing")
    with pytest.raises(TimeoutError):
        Model().generate("test")
    with pytest.raises(RuntimeError, match="未确认失败"):
        Model().generate("test")
    assert len(calls) == 1


def test_pc005_shared_reservation_and_five_image_cap(isolated_gate, monkeypatch):
    monkeypatch.setenv('PC_BUDGET_CREDITS', '30')
    calls = []
    class Model:
        model = limits.MODELS['image']
        @limits.model_call('image')
        def generate(self, prompt):
            calls.append(prompt)
            return 'offline mock image'
    for _ in range(5):
        Model().generate('offline only')
    with pytest.raises(RuntimeError, match='上限'):
        Model().generate('sixth attempt must not reach provider')
    assert len(calls) == 5
    assert limits.budget_state()['reserved_credits'] == 20
    rows = [json.loads(row) for row in (isolated_gate / 'calls.jsonl').read_text().splitlines()]
    assert [row['attempt'] for row in rows] == [1, 2, 3, 4, 5]
    assert all(row['actual_credits'] is None for row in rows)
    assert limits.RESERVATIONS['text'] >= (32768 + 1024) * 15 / 1_000_000 + 4096 * 60 / 1_000_000
    assert limits.LIMITS['text'] * limits.RESERVATIONS['text'] + 20 + 3 <= 30


def test_pc005_rejects_above_authorized_credit_ceiling(isolated_gate, monkeypatch):
    monkeypatch.setenv('PC_BUDGET_CREDITS', '30.01')
    assert limits.execution_reason('text') == 'authorization_required'


def test_pc005_existing_ledger_is_shared_not_reset(isolated_gate):
    limits._save(isolated_gate / 'call-budget.json',
                 {'text': 2, 'image': 1, 'reserved_credits': 19.5, 'paused': False})
    class Model:
        model = limits.MODELS['text']
        @limits.model_call('text')
        def generate(self, prompt):
            pytest.fail('Existing reservation must block this call')
    with pytest.raises(RuntimeError, match='上限'):
        Model().generate('offline only')
    assert limits.budget_state()['text'] == 2
    assert limits.budget_state()['reserved_credits'] == 19.5


def test_personal_env_does_not_inherit_connections(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "RUNTIME", tmp_path)
    monkeypatch.setattr(runner, "CONFIG", tmp_path / "explicit.env")
    monkeypatch.setenv("DATABASE_URL", "postgresql://must-not-be-used.invalid/db")
    monkeypatch.setenv("HTTP_PROXY", "http://must-not-be-used.invalid")
    original = dict(os.environ)
    import dotenv
    import tempfile
    loader, tempdir = dotenv.load_dotenv, tempfile.tempdir
    try:
        runner.prepare_environment(False, 0)
        assert "DATABASE_URL" not in os.environ and "HTTP_PROXY" not in os.environ
        assert os.environ["TEXT_API_KEY"] == ""
        assert Path(os.environ["DATABASE_PATH"]).is_relative_to(tmp_path)
        assert dotenv.load_dotenv("unrelated.env") is False
    finally:
        os.environ.clear()
        os.environ.update(original)
        dotenv.load_dotenv, tempfile.tempdir = loader, tempdir


def test_managed_environment_accepts_zero_legacy_credits_with_manual_balance(monkeypatch, tmp_path):
    import dotenv
    import tempfile
    from services.runtime_config import DEFAULT

    monkeypatch.setattr(runner, 'RUNTIME', tmp_path)
    monkeypatch.setattr(runner, 'config_values', lambda: {
        **runner.PRESET, 'TEXT_API_KEY': 'offline-only', 'IMAGE_API_KEY': 'offline-only',
    })
    monkeypatch.setattr(os, 'environ', os.environ.copy())
    monkeypatch.setattr(dotenv, 'load_dotenv', dotenv.load_dotenv)
    monkeypatch.setattr(tempfile, 'tempdir', tempfile.tempdir)
    authorization = {'credits': 0, 'balance': {
        'id': 'offline-restart-balance', 'credits': 9, 'image_2k': 3, 'text_fallback': 0.8,
    }}
    config = {**DEFAULT, 'authorization': authorization}

    runner.prepare_environment(True, 0, runtime_config=config)

    assert os.environ['PC_MODE'] == 'live'
    assert os.environ['PC_BUDGET_CREDITS'] == '0'
    assert json.loads(os.environ['PC_RUN_POLICY'])['authorization'] == authorization
    with pytest.raises(RuntimeError, match='受管运行需要有效积分上限'):
        runner.prepare_environment(True, 0, runtime_config={**config, 'authorization': {'credits': 0}})
    for credits in (0, 31):
        with pytest.raises(RuntimeError, match='最多 30'):
            runner.prepare_environment(True, credits)


def test_network_guard_rejects_other_hosts_models_and_preview(monkeypatch):
    import requests
    # Restore process-local patches when the test ends.
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    monkeypatch.setattr(requests.Session, "request", requests.Session.request)
    runner.install_network_guard(False)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("No HTTP expected")))
    with pytest.raises(RuntimeError, match="出站"):
        client.post("https://tokenflux.dev/v1/chat/completions", json={"model": "deepseek-flash"})
    with pytest.raises(RuntimeError, match="出站"):
        client.post("https://unrelated.invalid/v1/chat/completions", json={"model": "deepseek-flash"})
    client.close()


def test_openai_wire_format_offline(monkeypatch):
    monkeypatch.delenv("PC_MODE", raising=False)
    from config import Config
    from openai import OpenAI
    from services.ai_providers.text.openai_provider import OpenAITextProvider
    monkeypatch.setattr(Config, "MODEL_MAX_OUTPUT_TOKENS", 4096)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 0,
            "model": "deepseek-flash", "choices": [{"index": 0, "message": {"role": "assistant", "content": "offline"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})
    provider = OpenAITextProvider(api_key="offline", api_base="https://tokenflux.dev/v1", model="deepseek-flash")
    provider.client.close()
    provider.client = OpenAI(api_key="offline", base_url="https://tokenflux.dev/v1", max_retries=0,
                             http_client=httpx.Client(transport=httpx.MockTransport(respond)))
    assert provider.generate_text("test") == "offline"
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer offline"
    assert body["max_tokens"] == 4096 and body["model"] == "deepseek-flash"
    provider.client.close()


@pytest.mark.parametrize('model,allowed', [
    ('nano-banana-2', True),
    ('gemini-3.1-flash-image', False),
    ('nano-banana-2.1', False),
])
def test_managed_image_network_guard_uses_current_protocol_id(monkeypatch, model, allowed):
    import requests
    from services import task_tracking

    monkeypatch.setattr(httpx.Client, 'send', httpx.Client.send)
    monkeypatch.setattr(requests.Session, 'request', requests.Session.request)
    monkeypatch.delenv('PC_ADMIN_PAUSED', raising=False)
    monkeypatch.delenv('PC_ADMIN_APPLYING', raising=False)
    marked_sent = []
    monkeypatch.setattr(task_tracking, 'sent', lambda: marked_sent.append(True))
    runner.install_network_guard(True)
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={'offline_mock': True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        url = f'https://tokenflux.dev/v1beta/models/{model}:generateContent'
        if allowed:
            assert client.post(url, json={'contents': []}).status_code == 200
            assert len(sent) == len(marked_sent) == 1
        else:
            with pytest.raises(RuntimeError, match='出站'):
                client.post(url, json={'contents': []})
            assert sent == marked_sent == []


def test_genai_wire_format_and_inline_image_offline(monkeypatch):
    monkeypatch.delenv("PC_MODE", raising=False)
    from config import Config
    from PIL import Image
    from google import genai
    from google.genai import types
    from services.ai_providers.image.genai_provider import GenAIImageProvider
    monkeypatch.setattr(Config, "MODEL_MAX_OUTPUT_TOKENS", 4096)
    monkeypatch.setattr(Config, "GENAI_SDK_ATTEMPTS", 1)
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.b64encode(buffer.getvalue()).decode()}}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 1, "totalTokenCount": 3}})
    provider = GenAIImageProvider(model="nano-banana-2", api_key="offline", api_base="https://tokenflux.dev")
    provider.client.close()
    provider.client = genai.Client(api_key="offline", http_options=types.HttpOptions(base_url="https://tokenflux.dev",
        retry_options=types.HttpRetryOptions(attempts=1), client_args={"transport": httpx.MockTransport(respond)}))
    assert provider.generate_image("test", enable_thinking=False).size == (2, 2)
    body = json.loads(requests[0].content)
    assert requests[0].url.path == "/v1beta/models/nano-banana-2:generateContent"
    assert requests[0].headers["x-goog-api-key"] == "offline"
    assert body["generationConfig"]["imageConfig"] == {"aspectRatio": "16:9", "imageSize": "2K"}
    assert body["generationConfig"]["maxOutputTokens"] == 4096 and len(requests) == 1
    provider.client.close()


def test_flask_static_ui_and_preview_rejection(monkeypatch):
    # In-process request checks, not evidence of a reachable browser service.
    monkeypatch.setenv("PC_MODE", "preview")
    import requests
    monkeypatch.setattr(httpx.Client, "send", httpx.Client.send)
    monkeypatch.setattr(requests.Session, "request", requests.Session.request)
    app = runner.create_local_app(False, 5011)
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200 and b'id="root"' in response.data
    assert client.get("/history").status_code == 200
    assert client.post("/api/settings/verify", json={}).status_code == 403
    project = client.post("/api/projects", json={"creation_type": "idea", "idea_prompt": "PC-001 离线接口检查；没有真实模型产物。"})
    assert project.status_code == 201
    project_id = project.json["data"]["project_id"]
    response = client.post(f"/api/projects/{project_id}/generate/outline", json={})
    assert response.status_code >= 400
    assert response.json["error"]["code"] == "preview"


def test_invisible_recorded_process_is_not_assumed_stopped(monkeypatch, tmp_path):
    import psutil
    path = tmp_path / "server.json"
    monkeypatch.setattr(runner, "PID_FILE", path)
    def invisible(pid):
        raise psutil.NoSuchProcess(pid)
    monkeypatch.setattr(psutil, "Process", invisible)
    path.write_text(json.dumps({"pid": 123, "stopped": False}), encoding="utf8")
    with pytest.raises(RuntimeError, match="不能确认"):
        runner.recorded_process()
    path.write_text(json.dumps({"pid": 123, "stopped": True}), encoding="utf8")
    assert runner.recorded_process() is None
