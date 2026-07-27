"""Shared JSON-output normalization + per-provider ``expects_json`` honor.

Covers the hotfix (task 2.4.11): the shared ``strip_markdown_json`` helper
and each provider's contract that a JSON-expecting call returns bare JSON
(native JSON mode and/or fence stripping), while plain-text calls pass
through untouched.
"""

from __future__ import annotations

import itertools
from unittest.mock import AsyncMock, MagicMock, patch

from course_supporter.llm.json_extract import strip_markdown_json
from course_supporter.llm.schemas import LLMRequest

_FENCED = '```json\n{"a": 1}\n```'
_BARE = '{"a": 1}'


class TestStripMarkdownJson:
    """Pure-function behaviour of the shared helper."""

    def test_fenced_json(self) -> None:
        assert strip_markdown_json(_FENCED) == _BARE

    def test_fence_without_lang_tag(self) -> None:
        assert strip_markdown_json('```\n{"a": 1}\n```') == _BARE

    def test_bare_json_unchanged(self) -> None:
        assert strip_markdown_json(_BARE) == _BARE

    def test_surrounding_whitespace_stripped(self) -> None:
        assert strip_markdown_json('  {"a": 1}\n') == _BARE

    def test_multiline_fenced_json_inner_preserved(self) -> None:
        text = '```json\n{\n  "a": 1,\n  "b": 2\n}\n```'
        assert strip_markdown_json(text) == '{\n  "a": 1,\n  "b": 2\n}'

    def test_plain_text_without_fence_is_noop(self) -> None:
        # A Pass 2c denoised transcript (plain text) must pass through
        # untouched — the helper is only ever called for JSON stages, but
        # this guards the no-op contract regardless.
        text = "Привіт, це чистий транскрипт без JSON."
        assert strip_markdown_json(text) == text


async def test_gemini_honors_expects_json() -> None:
    """Gemini: native JSON mode + fence strip when ``expects_json``; raw otherwise."""
    from course_supporter.llm.providers.gemini import GeminiProvider

    with patch("course_supporter.llm.providers.gemini.genai.Client"):
        prov = GeminiProvider(api_keys=("k",), default_model="gemini-2.5-flash")

    resp = MagicMock()
    resp.text = _FENCED
    resp.usage_metadata = MagicMock(prompt_token_count=10, candidates_token_count=5)
    mock_client = MagicMock()
    mock_client.aio.models.generate_content = AsyncMock(return_value=resp)
    prov._client_cycle = itertools.cycle([mock_client])

    out = await prov.complete(
        LLMRequest(prompt="x", model="gemini-2.5-flash", expects_json=True)
    )
    assert out.content == _BARE
    cfg = mock_client.aio.models.generate_content.call_args.kwargs["config"]
    assert cfg.response_mime_type == "application/json"

    out_raw = await prov.complete(
        LLMRequest(prompt="x", model="gemini-2.5-flash", expects_json=False)
    )
    assert out_raw.content == _FENCED
    cfg_raw = mock_client.aio.models.generate_content.call_args.kwargs["config"]
    assert cfg_raw.response_mime_type is None


async def test_openai_compat_honors_expects_json() -> None:
    """OpenAI-compat (DeepSeek/Mistral): fence strip when ``expects_json``."""
    from course_supporter.llm.providers.openai_compat import OpenAICompatProvider

    with patch("course_supporter.llm.providers.openai_compat.openai.AsyncOpenAI"):
        prov = OpenAICompatProvider(
            api_keys=("k",), default_model="deepseek-v4-flash", provider_name="deepseek"
        )

    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content=_FENCED))]
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5)
    mock_client = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)
    prov._client_cycle = itertools.cycle([mock_client])

    out = await prov.complete(LLMRequest(prompt="x", expects_json=True))
    assert out.content == _BARE

    out_raw = await prov.complete(LLMRequest(prompt="x", expects_json=False))
    assert out_raw.content == _FENCED


async def test_anthropic_honors_expects_json() -> None:
    """Anthropic: fence strip when ``expects_json``."""
    from course_supporter.llm.providers.anthropic import AnthropicProvider

    with patch("course_supporter.llm.providers.anthropic.anthropic.AsyncAnthropic"):
        prov = AnthropicProvider(api_keys=("k",), default_model="claude-x")

    resp = MagicMock()
    resp.content = [MagicMock(text=_FENCED)]
    resp.usage = MagicMock(input_tokens=10, output_tokens=5)
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=resp)
    prov._client_cycle = itertools.cycle([mock_client])

    out = await prov.complete(LLMRequest(prompt="x", expects_json=True))
    assert out.content == _BARE

    out_raw = await prov.complete(LLMRequest(prompt="x", expects_json=False))
    assert out_raw.content == _FENCED
