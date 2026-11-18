"""Shared GenAI client factory used by both text and image providers."""

import base64
import binascii
import logging
from google import genai
from google.genai import types
from config import get_config

logger = logging.getLogger(__name__)


def standardize_inline_image_data(payload: dict) -> bool:
    """Normalize only final Gemini image fields for strict Base64 proxies.

    Run after SDK serialization: google-genai 1.52 emits URL-safe Base64 for
    byte blobs, even when the input was constructed from standard Base64.
    """
    updates = []
    for content in payload.get('contents', []):
        for part in content.get('parts', []):
            inline = part.get('inlineData')
            if not isinstance(inline, dict) or not str(inline.get('mimeType', '')).startswith('image/'):
                continue
            data = inline.get('data')
            if not isinstance(data, str) or not data:
                raise ValueError('参考图片编码无效，未发送请求。')
            try:
                raw = base64.b64decode(data, altchars=b'-_', validate=True)
            except (ValueError, binascii.Error):
                raise ValueError('参考图片编码无效，未发送请求。') from None
            standard = base64.b64encode(raw).decode('ascii')
            if standard != data:
                updates.append((inline, standard))
    for inline, standard in updates:
        inline['data'] = standard
    return bool(updates)


def make_genai_client(
    *,
    vertexai: bool,
    api_key: str = None,
    api_base: str = None,
    project_id: str = None,
    location: str = None,
) -> genai.Client:
    """Construct a ``genai.Client`` for either AI Studio or Vertex AI."""
    timeout_ms = int(get_config().GENAI_TIMEOUT * 1000)

    if vertexai:
        logger.info("Creating GenAI client (Vertex AI) — project=%s, location=%s", project_id, location)
        return genai.Client(
            vertexai=True,
            project=project_id,
            location=location or "us-central1",
            http_options=types.HttpOptions(timeout=timeout_ms),
        )

    options = dict(timeout=timeout_ms, base_url=api_base)
    attempts = get_config().GENAI_SDK_ATTEMPTS
    if attempts:
        options['retry_options'] = types.HttpRetryOptions(attempts=attempts)
    opts = types.HttpOptions(**options)
    return genai.Client(http_options=opts, api_key=api_key)
