"""Tests for per-provider error classifiers (KD16, commit (b))."""

from collections.abc import Callable

import anthropic
import httpx
import openai
import pytest
from google.genai import errors as genai_errors

from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.providers.anthropic import AnthropicProvider
from course_supporter.llm.providers.base import LLMProvider
from course_supporter.llm.providers.gemini import GeminiProvider
from course_supporter.llm.providers.openai_compat import OpenAICompatProvider
from course_supporter.llm.schemas import LLMRequest, LLMResponse

# ── Helpers ────────────────────────────────────────────────────────


def _httpx_response(
    status: int,
    url: str = "https://api.example.com/v1/messages",
) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", url))


# ── Stub provider for base classifier coverage ─────────────────────


class _StubProvider(LLMProvider):
    """Concrete subclass that does NOT override classify_error."""

    provider_name = "stub"

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise NotImplementedError

    async def complete_structured(
        self,
        request: LLMRequest,
        response_schema: type,
    ) -> tuple[object, LLMResponse]:
        raise NotImplementedError


class TestBaseClassifier:
    """Default classifier returns SEMANTIC for any exception."""

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("boom"),
            RuntimeError("network down"),
            Exception("generic"),
            TimeoutError("timeout"),
        ],
    )
    def test_default_returns_semantic(self, exc: Exception) -> None:
        provider = _StubProvider()
        assert provider.classify_error(exc) is ErrorCategory.SEMANTIC


# ── Anthropic classifier ───────────────────────────────────────────


def _anthropic_provider() -> AnthropicProvider:
    return AnthropicProvider(api_keys=("dummy",), default_model="claude-x")


def _anthropic_rate_limit() -> Exception:
    return anthropic.RateLimitError(
        "rate limited",
        response=_httpx_response(429),
        body=None,
    )


def _anthropic_timeout() -> Exception:
    return anthropic.APITimeoutError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1"),
    )


def _anthropic_connection_error() -> Exception:
    return anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1"),
    )


def _anthropic_internal_server() -> Exception:
    return anthropic.InternalServerError(
        "upstream",
        response=_httpx_response(500),
        body=None,
    )


def _anthropic_overflow() -> Exception:
    return anthropic.BadRequestError(
        "prompt is too long: 200000 tokens > 199999 maximum",
        response=_httpx_response(400),
        body=None,
    )


def _anthropic_overflow_via_body() -> Exception:
    return anthropic.BadRequestError(
        "validation",
        response=_httpx_response(400),
        body={"error": {"message": "context window exceeded"}},
    )


def _anthropic_other_bad_request() -> Exception:
    return anthropic.BadRequestError(
        "missing field 'model'",
        response=_httpx_response(400),
        body=None,
    )


class TestAnthropicClassifier:
    @pytest.mark.parametrize(
        ("factory", "expected"),
        [
            (_anthropic_rate_limit, ErrorCategory.INFRASTRUCTURE),
            (_anthropic_timeout, ErrorCategory.INFRASTRUCTURE),
            (_anthropic_connection_error, ErrorCategory.INFRASTRUCTURE),
            (_anthropic_internal_server, ErrorCategory.INFRASTRUCTURE),
            (_anthropic_overflow, ErrorCategory.INPUT_OVERFLOW),
            (_anthropic_overflow_via_body, ErrorCategory.INPUT_OVERFLOW),
            (_anthropic_other_bad_request, ErrorCategory.SEMANTIC),
        ],
    )
    def test_known_exceptions(
        self,
        factory: Callable[[], Exception],
        expected: ErrorCategory,
    ) -> None:
        provider = _anthropic_provider()
        assert provider.classify_error(factory()) is expected

    def test_unknown_exception_falls_through_to_semantic(self) -> None:
        provider = _anthropic_provider()
        assert (
            provider.classify_error(ValueError("not an SDK exception"))
            is ErrorCategory.SEMANTIC
        )


# ── Gemini classifier ──────────────────────────────────────────────


def _gemini_provider() -> GeminiProvider:
    return GeminiProvider(api_keys=("dummy",), default_model="gemini-x")


def _genai_client_error(
    code: int,
    message: str = "",
) -> genai_errors.ClientError:
    response_json = {"error": {"code": code, "message": message}}
    response = httpx.Response(
        code,
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com"),
        json=response_json,
    )
    return genai_errors.ClientError(code, response_json, response)


def _genai_server_error(code: int = 503) -> genai_errors.ServerError:
    response_json = {"error": {"code": code, "message": "upstream"}}
    response = httpx.Response(
        code,
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com"),
        json=response_json,
    )
    return genai_errors.ServerError(code, response_json, response)


class TestGeminiClassifier:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (_genai_server_error(503), ErrorCategory.INFRASTRUCTURE),
            (_genai_server_error(500), ErrorCategory.INFRASTRUCTURE),
            (_genai_client_error(429, "quota"), ErrorCategory.INFRASTRUCTURE),
            (_genai_client_error(408, "timeout"), ErrorCategory.INFRASTRUCTURE),
            (_genai_client_error(413, "payload"), ErrorCategory.INPUT_OVERFLOW),
            (
                _genai_client_error(400, "input is too long for this model"),
                ErrorCategory.INPUT_OVERFLOW,
            ),
            (
                _genai_client_error(400, "missing required field"),
                ErrorCategory.SEMANTIC,
            ),
            (_genai_client_error(401, "auth"), ErrorCategory.SEMANTIC),
        ],
    )
    def test_known_exceptions(
        self,
        exc: Exception,
        expected: ErrorCategory,
    ) -> None:
        provider = _gemini_provider()
        assert provider.classify_error(exc) is expected

    def test_unknown_exception_falls_through_to_semantic(self) -> None:
        provider = _gemini_provider()
        assert (
            provider.classify_error(httpx.ConnectError("refused"))
            is ErrorCategory.SEMANTIC
        )


# ── OpenAI-compat classifier ───────────────────────────────────────


def _openai_compat_provider(
    provider_name: str = "openai",
) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        api_keys=("dummy",),
        default_model="model-x",
        provider_name=provider_name,
    )


def _openai_rate_limit() -> Exception:
    return openai.RateLimitError(
        "rate limited",
        response=_httpx_response(429, "https://api.openai.com/v1/chat/completions"),
        body=None,
    )


def _openai_timeout() -> Exception:
    return openai.APITimeoutError(
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
    )


def _openai_connection_error() -> Exception:
    return openai.APIConnectionError(
        request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"),
    )


def _openai_internal_server() -> Exception:
    return openai.InternalServerError(
        "upstream",
        response=_httpx_response(500, "https://api.openai.com/v1/chat/completions"),
        body=None,
    )


def _openai_overflow_by_code() -> Exception:
    return openai.BadRequestError(
        "context_length_exceeded",
        response=_httpx_response(400, "https://api.openai.com/v1/chat/completions"),
        body={"code": "context_length_exceeded", "message": "too many tokens"},
    )


def _deepseek_overflow_by_message() -> Exception:
    return openai.BadRequestError(
        "Maximum context length is 64000 tokens",
        response=_httpx_response(400, "https://api.deepseek.com/v1/chat/completions"),
        body=None,
    )


def _openai_other_bad_request() -> Exception:
    return openai.BadRequestError(
        "missing field 'model'",
        response=_httpx_response(400, "https://api.openai.com/v1/chat/completions"),
        body={"code": "missing_required_param"},
    )


def _httpx_read_timeout() -> Exception:
    # ``httpx.ReadTimeout`` is the concrete timeout the SDK surfaces
    # when the explicit ``httpx.Timeout(read=...)`` budget is hit. We
    # classify the base ``httpx.TimeoutException`` so any variant
    # (read / write / connect / pool) escalates the same way.
    return httpx.ReadTimeout("read timeout")


def _builtin_timeout_error() -> Exception:
    # In Py3.11+ ``asyncio.TimeoutError`` is an alias of builtin
    # ``TimeoutError``; the classifier matches the latter to cover
    # outer ``asyncio.wait_for`` guards layered around the SDK call.
    return TimeoutError()


class TestOpenAICompatClassifier:
    @pytest.mark.parametrize(
        ("factory", "expected"),
        [
            (_openai_rate_limit, ErrorCategory.INFRASTRUCTURE),
            (_openai_timeout, ErrorCategory.INFRASTRUCTURE),
            (_openai_connection_error, ErrorCategory.INFRASTRUCTURE),
            (_openai_internal_server, ErrorCategory.INFRASTRUCTURE),
            (_httpx_read_timeout, ErrorCategory.INFRASTRUCTURE),
            (_builtin_timeout_error, ErrorCategory.INFRASTRUCTURE),
            (_openai_overflow_by_code, ErrorCategory.INPUT_OVERFLOW),
            (_deepseek_overflow_by_message, ErrorCategory.INPUT_OVERFLOW),
            (_openai_other_bad_request, ErrorCategory.SEMANTIC),
        ],
    )
    def test_known_exceptions_openai(
        self,
        factory: Callable[[], Exception],
        expected: ErrorCategory,
    ) -> None:
        provider = _openai_compat_provider("openai")
        assert provider.classify_error(factory()) is expected

    @pytest.mark.parametrize("vendor", ["openai", "deepseek", "mistral"])
    def test_classifier_shared_across_compat_vendors(self, vendor: str) -> None:
        provider = _openai_compat_provider(vendor)
        assert (
            provider.classify_error(_openai_rate_limit())
            is ErrorCategory.INFRASTRUCTURE
        )
        assert (
            provider.classify_error(_deepseek_overflow_by_message())
            is ErrorCategory.INPUT_OVERFLOW
        )

    def test_unknown_exception_falls_through_to_semantic(self) -> None:
        provider = _openai_compat_provider()
        assert provider.classify_error(ValueError("nope")) is ErrorCategory.SEMANTIC
