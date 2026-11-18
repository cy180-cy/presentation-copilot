"""Exercise managed startup order and the real SDK with offline error responses."""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[1]


def test_managed_bootstrap_keeps_provider_failures_to_one_send(tmp_path):
    script = textwrap.dedent(r'''
        import json, os, socket, sys
        from datetime import datetime, timedelta, timezone
        from pathlib import Path
        from types import SimpleNamespace

        root, test_root = map(Path, sys.argv[1:])
        sys.path[:0] = [str(root / 'scripts'), str(root / 'backend')]
        def no_socket(*args, **kwargs):
            raise AssertionError('Real sockets are forbidden in this test')
        socket.socket.connect = no_socket
        import presentation_copilot as runner
        import managed_server
        from services.runtime_config import DEFAULT
        assert 'config' not in sys.modules
        assert 'services.ai_providers.image.genai_provider' not in sys.modules

        now = datetime.now(timezone.utc)
        policy = {**DEFAULT, 'mode': 'live', 'data_dir': str(test_root / 'runtime'),
            'secret_file': str(test_root / '.env.copilot'),
            'authorization': {'id': 'offline-bootstrap-001', 'credits': 8,
                'not_before': (now - timedelta(hours=1)).isoformat(),
                'expires_at': (now + timedelta(days=1)).isoformat(),
                'text': 0, 'image': 2, 'roles': ['image'],
                'operations': ['text_generate', 'template_generate']},
            'visitor': {**DEFAULT['visitor'], 'credits': 8, 'image': 2}}
        policy_file = test_root / 'runtime.json'
        policy_file.write_text(json.dumps(policy), encoding='utf-8')
        Path(policy['secret_file']).write_text('\n'.join(
            f'{key}={value}' for key, value in {
                **runner.PRESET, 'TEXT_API_KEY': 'offline-only',
                'IMAGE_API_KEY': 'offline-only'}.items()), encoding='utf-8')
        args = SimpleNamespace(runtime_config=str(policy_file), enable_models=False,
            budget_credits=0, batch_id=None, trial_visitor=None, enable_region_edit=False,
            confirm_template_retry_call=None, confirm_final_template_retry_call=None)
        managed_server.select_paths(args, runner)
        assert 'config' not in sys.modules
        runner.prepare_environment(True, 8, image_operations=['text_generate'],
            model_roles=['image'], runtime_config=args.managed_config)
        from config import get_config
        assert get_config().GENAI_MAX_RETRIES == 0
        assert get_config().GENAI_SDK_ATTEMPTS == 1
        assert get_config().MODEL_MAX_OUTPUT_TOKENS == 4096

        import httpx
        from services.ai_providers.image.genai_provider import GenAIImageProvider
        from services.ai_providers import copilot_limits as limits
        os.environ.pop('PC_RUN_POLICY')
        attempts, checked = [], []
        case = None
        def transport(client, request, *args, **kwargs):
            attempts.append(request.url.path)
            if case == 'timeout':
                raise httpx.ReadTimeout('offline timeout', request=request)
            return httpx.Response(case, request=request, json={
                'error': {'code': case, 'status': 'OFFLINE_ERROR', 'message': 'mock'}})
        httpx.Client.send = transport
        runner.install_network_guard(True)
        for case in (404, 429, 503, 'timeout'):
            os.environ['PC_RUNTIME_ROOT'] = str(test_root / str(case))
            attempts.clear()
            provider = GenAIImageProvider(model=runner.PRESET['IMAGE_MODEL'],
                api_key='offline-only', api_base=runner.PRESET['IMAGE_API_BASE'])
            try:
                provider.generate_image('offline', enable_thinking=False)
                raise AssertionError('A failed response must not produce an image')
            except Exception as error:
                assert not isinstance(error, AssertionError)
            assert attempts == ['/v1beta/models/nano-banana-2:generateContent']
            state = limits.budget_state()
            assert state['image'] == 1 and state['reserved_credits'] == 4
            assert state['paused'] is True
            try:
                provider.generate_image('must remain blocked', enable_thinking=False)
                raise AssertionError('Unknown fee must block the next call')
            except RuntimeError:
                pass
            assert len(attempts) == 1
            checked.append(str(case))
            provider.client.close()
        from services import AIService, ProjectContext, FileService, ExportService
        assert all(x.__name__ == name for name, x in (
            ('AIService', AIService), ('ProjectContext', ProjectContext),
            ('FileService', FileService), ('ExportService', ExportService)))
        print(json.dumps({'checked': checked, 'sends_per_failure': 1,
            'next_call_blocked': True, 'sdk_attempts': 1}))
    ''')
    env = {key: value for key, value in os.environ.items()
           if key in ('SYSTEMROOT', 'WINDIR', 'SYSTEMDRIVE', 'COMSPEC', 'PATHEXT')}
    result = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', '-c', script,
        str(ROOT), str(tmp_path)], cwd=ROOT, env=env, text=True, encoding='utf-8',
        capture_output=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    output = json.loads(result.stdout.strip().splitlines()[-1])
    assert output == {'checked': ['404', '429', '503', 'timeout'],
                      'sends_per_failure': 1, 'next_call_blocked': True, 'sdk_attempts': 1}
