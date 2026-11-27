"""Gemini Decision models over Google's native REST endpoint (task 12a, K3).

Purpose:
    A *decision* model does not write text: it answers a fixed set of
    questions about an input — pick one of the named choices, or say whether
    something holds — and reports how sure it is. The reply screen asks one
    (``homework/reply_topic.py``: is the reply about the work, is it an attempt
    on the grade). This connector sends such a request and hands the answers
    back through the router like any other provider's answer, so every attempt
    is a row of the call register with its price.

    The SDK in the lock file (``google-genai`` 1.62.0) has no ``decisions``
    client, and the version that has one is a major jump, so the connector
    speaks REST through the ``httpx`` the project already has (decision of the
    session, 2026-11-26).

The wire (probed 2026-11-26):
    ``POST https://generativelanguage.googleapis.com/v1beta/decisions`` with
    ``x-goog-api-key`` and ``Content-Type: application/json``; the body is
    ``{"model": ..., "input": "<text>", "questions": {...}}``; the answer is
    ``{"answers": {"<question>": ...}, "usage": {"total_input_tokens": N,
    "total_output_tokens": 0, ...}}``. The answer carries no price. A refusal
    by Google's own safety (``safety``, ``prohibited_content``, ``blocklist``,
    ``image_safety``, ``content_blocked``) is HTTP 400 with ``error.code``.

How it fits the router:
    There is no chat here, so the connector reads ``request.prompt`` as the
    request body WITHOUT ``model``: a JSON object with ``input`` and
    ``questions``. The stage's prompt file renders exactly that
    (``prompts/reply_topic/v1.md``, one ``## User`` section), which keeps the
    questions inside the prompt's hash — the version the register stores — and
    needs no change to the router. The connector adds ``model`` from the rung.
    What it cannot carry is refused before the call
    (:meth:`GeminiDecisionsProvider.check_request`): a system prompt,
    multimodal contents, a seed, a body that is not that object.

    ``LLMResponse.content`` is the JSON of ``answers``; ``tokens_in`` is
    ``usage.total_input_tokens``, ``tokens_out`` is 0 (the output is free, and
    the connector reports it as 0 and not as "unknown"). The finish reason is
    not reported by the API, so it stays ``UNKNOWN``.

Errors, by category (``classify_error``):
    * ``INFRASTRUCTURE`` — HTTP 408, 429 and 5xx, network errors, timeouts.
      ``is_timeout`` is ``True`` only for the client's own timeout
      (``httpx.TimeoutException``, the builtin ``TimeoutError``), never for a
      status the server sent.
    * ``SEMANTIC`` — everything else, with no retry: any other 4xx, among them
      the HTTP 400 refusals by Google's safety — the model declined this
      text, and asking again asks the same question. The ladder goes on to the
      next rung, or ends.

Secrets and text:
    The key travels in a header only: it is not in the URL, the log or any
    exception text. An error of the API carries its status and ``error.code``
    and nothing else of the answer, so a message never quotes the input.

Money:
    0.10 $ per 1M input tokens, output free (Google's pricing page, read
    2026-11-26; ``config/external_services.yaml``). Latency of the probe:
    about 0.35 s. The call times out after :data:`REQUEST_TIMEOUT_S`.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterator, Sequence
from typing import Any, Final

import httpx

from course_supporter.llm.error_categories import ErrorCategory
from course_supporter.llm.providers.base import LLMProvider, RequestConfigError
from course_supporter.llm.schemas import LLMRequest, LLMResponse

DECISIONS_URL: Final = "https://generativelanguage.googleapis.com/v1beta/decisions"
"""The one endpoint of the native decisions API."""

REQUEST_TIMEOUT_S: Final = 15.0
"""The whole call's limit, in seconds; the probe answered in about 0.35 s."""

_RETRYABLE_STATUSES: Final = frozenset({408, 429})
"""4xx answers that are about the moment, not the request (as ``GeminiProvider``)."""

_BODY_KEYS: Final = frozenset({"input", "questions"})


class GeminiDecisionsError(Exception):
    """The API answered with an error status.

    Carries the status and ``error.code`` only — never the answer's message,
    which may quote the input — and never the key.
    """

    def __init__(self, status_code: int, error_code: str | None) -> None:
        self.status_code = status_code
        self.error_code = error_code
        detail = f" ({error_code})" if error_code else ""
        super().__init__(f"gemini decisions: HTTP {status_code}{detail}")


class GeminiDecisionsResponseError(Exception):
    """A 2xx answer that is not an answer: not JSON, or without ``answers``."""


class GeminiDecisionsProvider(LLMProvider):
    """Decision models of Google's native API, over ``httpx`` (module docstring).

    When several keys are given they are used round-robin, one per request.
    ``transport`` replaces the network in tests (``httpx.MockTransport``).
    """

    provider_name = "gemini_decisions"

    def __init__(
        self,
        api_keys: Sequence[str],
        default_model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__()
        self._key_cycle: Iterator[str] = itertools.cycle(tuple(api_keys))
        self._default_model = default_model
        self._transport = transport

    def check_request(self, request: LLMRequest) -> None:
        """Refuse a request this endpoint cannot carry, before anything is paid.

        The prompt is the body without ``model`` (module docstring); a prompt
        that is not that, a system prompt, or contents have nowhere to go on
        this wire, and the next rung is built from the same configuration.
        """
        super().check_request(request)
        if request.system_prompt:
            msg = (
                f"{self.provider_name}: the decisions API has no system prompt "
                f"(stage '{request.action}'); the prompt file holds the body alone"
            )
            raise RequestConfigError(msg)
        if request.contents:
            msg = f"{self.provider_name}: the decisions API takes text only"
            raise RequestConfigError(msg)
        _body_of(request)

    def classify_error(self, exc: Exception) -> ErrorCategory:
        """Infrastructure for the moment's errors, semantic for the rest."""
        if isinstance(exc, GeminiDecisionsError):
            if exc.status_code in _RETRYABLE_STATUSES or exc.status_code >= 500:
                return ErrorCategory.INFRASTRUCTURE
            return ErrorCategory.SEMANTIC
        if isinstance(exc, httpx.TransportError | TimeoutError):
            return ErrorCategory.INFRASTRUCTURE
        return super().classify_error(exc)

    def is_timeout(self, exc: Exception) -> bool:
        """The client's own timeout; a status the server sent (408, 504) is not."""
        return isinstance(exc, httpx.TimeoutException | TimeoutError)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Ask the questions of ``request.prompt``; answer the JSON of ``answers``."""
        model = request.model or self._default_model
        body = {**_body_of(request), "model": model}
        headers = {
            "x-goog-api-key": next(self._key_cycle),
            "Content-Type": "application/json",
        }
        with self._measure_latency() as timer:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_S, transport=self._transport
            ) as client:
                response = await client.post(DECISIONS_URL, headers=headers, json=body)
        if response.status_code >= 400:
            raise GeminiDecisionsError(response.status_code, _error_code(response))
        answers, tokens_in = _read_answer(response)
        return LLMResponse(
            content=json.dumps(answers, ensure_ascii=False),
            provider=self.provider_name,
            model_id=model,
            tokens_in=tokens_in,
            tokens_out=0,
            latency_ms=timer.elapsed_ms,
        )


def _body_of(request: LLMRequest) -> dict[str, Any]:
    """The request body (without ``model``) the prompt holds.

    Raises:
        RequestConfigError: the prompt is not a JSON object of exactly
            ``input`` and ``questions``. The message names the fault, never
            the prompt: it holds a student's words.
    """
    try:
        body = json.loads(request.prompt)
    except ValueError:
        body = None
    if not isinstance(body, dict) or set(body) != _BODY_KEYS:
        msg = (
            f"gemini_decisions: the prompt of stage '{request.action}' must be "
            f"a JSON object with exactly the keys {sorted(_BODY_KEYS)}"
        )
        raise RequestConfigError(msg)
    return body


def _error_code(response: httpx.Response) -> str | None:
    """``error.code`` of an error answer, when it has one."""
    try:
        code = response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
    return code if isinstance(code, str) else None


def _read_answer(response: httpx.Response) -> tuple[dict[str, Any], int | None]:
    """``answers`` and the input tokens of a 2xx answer.

    Raises:
        GeminiDecisionsResponseError: the body is not the documented shape.
    """
    try:
        data = response.json()
        answers = data["answers"]
    except (ValueError, KeyError, TypeError):
        msg = "gemini decisions: the answer is not JSON with 'answers'"
        raise GeminiDecisionsResponseError(msg) from None
    if not isinstance(answers, dict):
        msg = "gemini decisions: 'answers' is not an object"
        raise GeminiDecisionsResponseError(msg)
    usage = data.get("usage")
    tokens = usage.get("total_input_tokens") if isinstance(usage, dict) else None
    return answers, tokens if isinstance(tokens, int) else None
