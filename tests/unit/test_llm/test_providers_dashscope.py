"""Unit tests for :class:`DashScopeProvider`.

DashScope SDK 1.25.18 exposes the Qwen multimodal endpoint through
``AioMultiModalConversation.call`` and surfaces most failures as
non-200 ``DashScopeAPIResponse`` objects rather than raising
exceptions. Tests cover the four behaviour clusters that bridge that
SDK shape to the project's :class:`LLMProvider` contract:

1. Pure helpers — MIME detection, markdown fence stripping, message
   construction, response text extraction.
2. ``classify_error`` mapping from DashScope error shapes to the
   shared :class:`ErrorCategory` ladder.
3. ``complete`` async path with a mocked
   ``AioMultiModalConversation.call`` — success extraction, non-200
   wrapping into :class:`DashScopeResponseError`, multi-key
   round-robin via the per-call ``api_key`` kwarg.
4. ``complete_structured`` async path with markdown-fenced JSON.

All SDK access is mocked; no real DashScope API call is made.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel

from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.providers.dashscope import (
    DashScopeProvider,
    DashScopeResponseError,
    _detect_image_mime,
    _strip_markdown_json,
)
from course_supporter.llm.schemas import LLMRequest


def _make_provider(api_keys: tuple[str, ...] = ("test-key",)) -> DashScopeProvider:
    """Construct a provider without touching the SDK global base URL."""
    return DashScopeProvider(
        api_keys=api_keys,
        default_model="qwen3-vl-32b-instruct",
        base_url=None,
    )


def _success_response(
    text: str = "Hello.",
    tokens_in: int = 42,
    tokens_out: int = 7,
) -> MagicMock:
    """Mock matching the ``DashScopeAPIResponse`` shape consumed by ``complete``."""
    response = MagicMock()
    response.status_code = 200
    response.code = ""
    response.message = ""
    response.output = {"choices": [{"message": {"content": [{"text": text}]}}]}
    response.usage = {"input_tokens": tokens_in, "output_tokens": tokens_out}
    return response


def _error_response(status_code: int, code: str = "", message: str = "") -> MagicMock:
    """Mock matching the non-200 response shape ``complete`` wraps."""
    response = MagicMock()
    response.status_code = status_code
    response.code = code
    response.message = message
    response.output = None
    response.usage = None
    return response


class _SchemaForTest(BaseModel):
    """Minimal schema used as ``response_model`` placeholder."""

    ok: bool


# ── Pure helpers ────────────────────────────────────────────────


class TestPureHelpers:
    """Helpers that do not touch the SDK or async machinery."""

    def test_detect_image_mime_png(self) -> None:
        assert _detect_image_mime(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) == "image/png"

    def test_detect_image_mime_jpeg(self) -> None:
        assert _detect_image_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == "image/jpeg"

    def test_detect_image_mime_webp(self) -> None:
        # RIFF....WEBP needs the two-segment check
        data = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 4
        assert _detect_image_mime(data) == "image/webp"

    def test_detect_image_mime_unknown_falls_back_to_octet_stream(self) -> None:
        # Aligns with providers/gemini.py: an unrecognised payload returns
        # ``application/octet-stream`` so the SDK surfaces a clear error
        # rather than silently mislabeling bytes.
        assert _detect_image_mime(b"\x00garbage bytes") == "application/octet-stream"

    def test_strip_markdown_json_fenced(self) -> None:
        text = '```json\n{"status": "ok"}\n```'
        assert _strip_markdown_json(text) == '{"status": "ok"}'

    def test_strip_markdown_json_no_fence(self) -> None:
        text = '{"status": "ok"}'
        assert _strip_markdown_json(text) == '{"status": "ok"}'

    def test_strip_markdown_json_fence_without_lang_tag(self) -> None:
        text = '```\n{"status": "ok"}\n```'
        assert _strip_markdown_json(text) == '{"status": "ok"}'


# ── __init__ side effects ───────────────────────────────────────


class TestInitGlobalState:
    """Provider-init side effects on the DashScope SDK module globals.

    The SDK exposes no per-client config object; ``base_http_api_url``
    is a module-level write that binds every subsequent call. The
    tests below cover both branches of the init-time guard so the
    reviewer's "untested global state" concern is closed.
    """

    def test_base_url_sets_sdk_global(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import dashscope

        sentinel = "https://example.maas/api/v1"
        # monkeypatch auto-restores the original value at test teardown,
        # so we cannot leak state into other tests in the suite.
        monkeypatch.setattr(dashscope, "base_http_api_url", "ORIGINAL")
        DashScopeProvider(
            api_keys=("k",),
            default_model="qwen3-vl-32b-instruct",
            base_url=sentinel,
        )
        assert dashscope.base_http_api_url == sentinel

    def test_base_url_none_leaves_sdk_global_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import dashscope

        monkeypatch.setattr(dashscope, "base_http_api_url", "ORIGINAL")
        DashScopeProvider(
            api_keys=("k",),
            default_model="qwen3-vl-32b-instruct",
            base_url=None,
        )
        assert dashscope.base_http_api_url == "ORIGINAL"


# ── _build_messages ─────────────────────────────────────────────


class TestBuildMessages:
    """Message shape — system + user, multimodal vs text-only."""

    def test_system_and_text_only(self) -> None:
        provider = _make_provider()
        request = LLMRequest(prompt="hi", system_prompt="sys")
        msgs = provider._build_messages(request)
        assert msgs == [
            {"role": "system", "content": [{"text": "sys"}]},
            {"role": "user", "content": [{"text": "hi"}]},
        ]

    def test_text_only_no_system(self) -> None:
        provider = _make_provider()
        request = LLMRequest(prompt="hi")
        msgs = provider._build_messages(request)
        assert msgs == [{"role": "user", "content": [{"text": "hi"}]}]

    def test_multimodal_png_with_text(self) -> None:
        provider = _make_provider()
        png_magic = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        request = LLMRequest(prompt="describe", contents=[png_magic])
        msgs = provider._build_messages(request)
        assert len(msgs) == 1
        content = msgs[0]["content"]
        # First element is the image element with a data: URI
        assert content[0]["image"].startswith("data:image/png;base64,")
        # Last element is the text prompt
        assert content[-1] == {"text": "describe"}

    def test_multimodal_jpeg_mime_detected(self) -> None:
        provider = _make_provider()
        jpeg_magic = b"\xff\xd8\xff\xe0" + b"\x00" * 8
        request = LLMRequest(prompt="ok", contents=[jpeg_magic])
        msgs = provider._build_messages(request)
        assert msgs[0]["content"][0]["image"].startswith("data:image/jpeg;base64,")

    def test_non_bytes_content_items_ignored(self) -> None:
        """Only raw bytes are converted; SDK-native items pass through filter."""
        provider = _make_provider()
        request = LLMRequest(
            prompt="ok",
            contents=["already-shaped-string", {"text": "raw dict"}],
        )
        msgs = provider._build_messages(request)
        # No bytes => no image element; only the prompt text remains.
        assert msgs[0]["content"] == [{"text": "ok"}]


# ── _extract_text ───────────────────────────────────────────────


class TestExtractText:
    """Response output extraction across the SDK variant shapes."""

    def test_standard_choices_shape(self) -> None:
        provider = _make_provider()
        response = MagicMock()
        response.output = {
            "choices": [{"message": {"content": [{"text": "hello world"}]}}]
        }
        assert provider._extract_text(response) == "hello world"

    def test_output_text_fallback_shape(self) -> None:
        """Single-turn ``output.text`` shape (older DashScope responses)."""
        provider = _make_provider()
        response = MagicMock()
        response.output = {"text": "single-turn"}
        assert provider._extract_text(response) == "single-turn"

    def test_empty_output_returns_empty_string(self) -> None:
        provider = _make_provider()
        response = MagicMock()
        response.output = None
        assert provider._extract_text(response) == ""

    def test_empty_choices_list_returns_empty_string(self) -> None:
        """Empty ``choices`` is guarded — no IndexError on ``choices[0]``.

        Documents the safety property flagged by a review heuristic. The
        ``if not choices`` guard treats both ``None`` and ``[]`` as empty,
        so ``choices[0]`` never executes for an empty list.
        """
        provider = _make_provider()
        response = MagicMock()
        response.output = {"choices": []}
        assert provider._extract_text(response) == ""

    def test_string_content_returned_as_is(self) -> None:
        """If ``content`` is already a string the helper returns it directly."""
        provider = _make_provider()
        response = MagicMock()
        response.output = {"choices": [{"message": {"content": "string content"}}]}
        assert provider._extract_text(response) == "string content"

    def test_multiple_text_parts_concatenated(self) -> None:
        provider = _make_provider()
        response = MagicMock()
        response.output = {
            "choices": [
                {
                    "message": {
                        "content": [{"text": "foo "}, {"text": "bar"}],
                    }
                }
            ]
        }
        assert provider._extract_text(response) == "foo bar"


# ── classify_error ──────────────────────────────────────────────


class TestClassifyError:
    """Error → ErrorCategory mapping for all branches."""

    @pytest.mark.parametrize(
        ("status_code", "expected"),
        [
            (429, ErrorCategory.INFRASTRUCTURE),
            (408, ErrorCategory.INFRASTRUCTURE),
            (504, ErrorCategory.INFRASTRUCTURE),
            (500, ErrorCategory.INFRASTRUCTURE),
            (502, ErrorCategory.INFRASTRUCTURE),
            (503, ErrorCategory.INFRASTRUCTURE),
            (413, ErrorCategory.INPUT_OVERFLOW),
        ],
    )
    def test_response_error_by_status(
        self, status_code: int, expected: ErrorCategory
    ) -> None:
        provider = _make_provider()
        exc = DashScopeResponseError(status_code=status_code, code="", message="")
        assert provider.classify_error(exc) == expected

    def test_400_with_overflow_code_prefix(self) -> None:
        provider = _make_provider()
        exc = DashScopeResponseError(
            status_code=400,
            code="InvalidParameter.MaxTokenExceed.PromptTooLong",
            message="prompt exceeds limit",
        )
        assert provider.classify_error(exc) == ErrorCategory.INPUT_OVERFLOW

    def test_400_with_overflow_message_pattern(self) -> None:
        provider = _make_provider()
        exc = DashScopeResponseError(
            status_code=400,
            code="InvalidParameter",
            message="input length exceeds maximum allowed",
        )
        assert provider.classify_error(exc) == ErrorCategory.INPUT_OVERFLOW

    def test_400_plain_validation_is_semantic(self) -> None:
        provider = _make_provider()
        exc = DashScopeResponseError(
            status_code=400,
            code="InvalidParameter",
            message="temperature must be between 0 and 2",
        )
        assert provider.classify_error(exc) == ErrorCategory.SEMANTIC

    def test_401_auth_is_semantic(self) -> None:
        provider = _make_provider()
        exc = DashScopeResponseError(status_code=401, code="", message="")
        assert provider.classify_error(exc) == ErrorCategory.SEMANTIC

    def test_sdk_authentication_error_is_semantic(self) -> None:
        from dashscope.common.error import AuthenticationError

        provider = _make_provider()
        assert (
            provider.classify_error(AuthenticationError("invalid api key"))
            == ErrorCategory.SEMANTIC
        )

    def test_sdk_invalid_parameter_is_semantic(self) -> None:
        from dashscope.common.error import InvalidParameter

        provider = _make_provider()
        assert (
            provider.classify_error(InvalidParameter("bad param"))
            == ErrorCategory.SEMANTIC
        )

    def test_unknown_exception_falls_through_to_base(self) -> None:
        provider = _make_provider()
        # Base classifier returns SEMANTIC as the safe default.
        assert provider.classify_error(RuntimeError("???")) == ErrorCategory.SEMANTIC


# ── complete() async ────────────────────────────────────────────


class TestCompleteAsync:
    """``complete`` end-to-end via mocked ``AioMultiModalConversation.call``."""

    @pytest.mark.asyncio
    async def test_success_extracts_content_and_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from course_supporter.llm.providers import dashscope as ds_module

        provider = _make_provider()
        fake_call = AsyncMock(
            return_value=_success_response(text="ok", tokens_in=10, tokens_out=3)
        )
        monkeypatch.setattr(ds_module.AioMultiModalConversation, "call", fake_call)

        request = LLMRequest(
            prompt="hi", model="qwen3-vl-32b-instruct", temperature=0.0, max_tokens=64
        )
        response = await provider.complete(request)

        fake_call.assert_awaited_once()
        kwargs = fake_call.await_args.kwargs
        assert kwargs["model"] == "qwen3-vl-32b-instruct"
        assert kwargs["api_key"] == "test-key"
        assert kwargs["temperature"] == 0.0
        assert kwargs["max_tokens"] == 64
        assert response.content == "ok"
        assert response.tokens_in == 10
        assert response.tokens_out == 3
        assert response.provider == "dashscope"
        assert response.model_id == "qwen3-vl-32b-instruct"

    @pytest.mark.asyncio
    async def test_non_200_raises_response_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from course_supporter.llm.providers import dashscope as ds_module

        provider = _make_provider()
        fake_call = AsyncMock(
            return_value=_error_response(
                status_code=429,
                code="Throttling.RateQuota",
                message="rate limit exceeded",
            )
        )
        monkeypatch.setattr(ds_module.AioMultiModalConversation, "call", fake_call)

        request = LLMRequest(prompt="hi", model="qwen3-vl-32b-instruct")
        with pytest.raises(DashScopeResponseError) as exc_info:
            await provider.complete(request)
        assert exc_info.value.status_code == 429
        assert exc_info.value.code == "Throttling.RateQuota"
        # The wrapper exception should also classify back to INFRASTRUCTURE.
        assert provider.classify_error(exc_info.value) == ErrorCategory.INFRASTRUCTURE

    @pytest.mark.asyncio
    async def test_multi_key_round_robin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two API keys → two calls visit each key once via ``api_key`` kwarg."""
        from course_supporter.llm.providers import dashscope as ds_module

        provider = _make_provider(api_keys=("key-A", "key-B"))
        fake_call = AsyncMock(return_value=_success_response())
        monkeypatch.setattr(ds_module.AioMultiModalConversation, "call", fake_call)

        request = LLMRequest(prompt="hi", model="qwen3-vl-32b-instruct")
        await provider.complete(request)
        await provider.complete(request)

        keys_used = [c.kwargs["api_key"] for c in fake_call.await_args_list]
        assert keys_used == ["key-A", "key-B"]

    @pytest.mark.asyncio
    async def test_multimodal_contents_pass_into_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from course_supporter.llm.providers import dashscope as ds_module

        provider = _make_provider()
        fake_call = AsyncMock(return_value=_success_response())
        monkeypatch.setattr(ds_module.AioMultiModalConversation, "call", fake_call)

        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        request = LLMRequest(
            prompt="describe",
            model="qwen3-vl-32b-instruct",
            contents=[png],
        )
        await provider.complete(request)

        kwargs = fake_call.await_args.kwargs
        msgs = kwargs["messages"]
        # User message must carry the inline image first, then the text.
        user_content = msgs[-1]["content"]
        assert any(
            isinstance(c, dict)
            and c.get("image", "").startswith("data:image/png;base64,")
            for c in user_content
        )
        assert {"text": "describe"} in user_content


# ── complete_structured() async ─────────────────────────────────


class TestCompleteStructuredAsync:
    """Schema injection + markdown fence stripping for JSON output."""

    @pytest.mark.asyncio
    async def test_markdown_fenced_json_parses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from course_supporter.llm.providers import dashscope as ds_module

        provider = _make_provider()
        fenced = '```json\n{"ok": true}\n```'
        fake_call = AsyncMock(return_value=_success_response(text=fenced))
        monkeypatch.setattr(ds_module.AioMultiModalConversation, "call", fake_call)

        request = LLMRequest(prompt="ask", system_prompt="be terse")
        parsed, raw = await provider.complete_structured(request, _SchemaForTest)
        assert isinstance(parsed, _SchemaForTest)
        assert parsed.ok is True
        # Sanity: schema was injected into the system prompt that reached the SDK.
        sys_msg = fake_call.await_args.kwargs["messages"][0]
        assert sys_msg["role"] == "system"
        sys_text = sys_msg["content"][0]["text"]
        assert "Respond ONLY with raw JSON" in sys_text
        # The original ``LLMResponse`` is returned verbatim alongside the parsed object.
        assert raw.content == fenced


# ── Registry + factory wiring ───────────────────────────────────


class TestProviderRegistryDashScope:
    """Registry + factory create the provider when the API key is set."""

    def test_registry_uses_dashscope_provider(self) -> None:
        from course_supporter.llm.providers import PROVIDER_REGISTRY

        assert PROVIDER_REGISTRY["dashscope"] is DashScopeProvider

    def test_factory_creates_dashscope_provider_instance(self) -> None:
        from course_supporter.config import Settings
        from course_supporter.llm.factory import create_providers

        s = Settings(
            alibaba_api_key="test-key",  # type: ignore[arg-type]
            _env_file=None,
        )
        providers = create_providers(s)
        assert isinstance(providers["dashscope"], DashScopeProvider)
