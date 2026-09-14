"""Canonical input of one LLM attempt, and its hash (mentor-rebuild task 01).

Purpose:
    Reproducibility: the same input sent twice must be recognisable as the
    same input, so a score that changed can be told apart from an input that
    changed. The call register stores :func:`hash_attempt_input` on every
    attempt row and, only when the full-input setting is raised, the
    canonical text itself (:func:`canonical_attempt_input`) — whose SHA-256
    is exactly the stored hash, so a recorded input can be verified against
    its row.

Interface:
    Input: the :class:`~course_supporter.llm.schemas.LLMRequest` actually
    sent, plus the ladder ``provider`` name it was sent through.
    Output: a compact, key-sorted JSON string (UTF-8) / its SHA-256 hex.

    What the document holds — everything that can change the output:
    the rendered system and user prompts; a SHA-256 digest per attachment
    (the bytes themselves are never serialised); the model; temperature;
    the output ceiling; the reasoning form; whether JSON was requested; and
    the provider name. The provider is part of the input because two ladder
    providers can send the same model name with different behaviour —
    ``deepseek`` and ``deepseek_thinking`` differ exactly in thinking off/on.

    The level is one attempt, not one stage call: a structural retry appends
    the validator's feedback to the prompt, so it hashes differently.

Replacing / extending:
    A new output-affecting request field must be added to
    :func:`attempt_input_document`; adding it changes every hash from then on,
    which is correct — inputs that differ in it were never the same input.
    Swapping the digest is a change here only; the register stores the hex.

>>> from course_supporter.llm.schemas import LLMRequest
>>> request = LLMRequest(prompt="hi", system_prompt="sys", model="m")
>>> hash_attempt_input(request, provider="p") == hash_attempt_input(
...     request.model_copy(), provider="p"
... )
True
>>> hash_attempt_input(request, provider="p") == hash_attempt_input(
...     request, provider="p_thinking"
... )
False
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from course_supporter.llm.schemas import LLMRequest


def _attachment_digest(item: object) -> str:
    if isinstance(item, (bytes, bytearray)):
        return hashlib.sha256(item).hexdigest()
    # The router only ever forwards raw bytes. Any other element (a caller
    # passing SDK-shaped parts straight to a provider) still hashes, via its
    # repr — stable for plain values, not guaranteed for arbitrary objects.
    return hashlib.sha256(repr(item).encode("utf-8")).hexdigest()


def attempt_input_document(request: LLMRequest, *, provider: str) -> dict[str, object]:
    """The output-affecting parts of one attempt, as a JSON-ready mapping."""
    return {
        "provider": provider,
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "reasoning": request.reasoning,
        "expects_json": request.expects_json,
        "system": request.system_prompt,
        "user": request.prompt,
        "attachments": [_attachment_digest(item) for item in request.contents or []],
    }


def canonical_attempt_input(request: LLMRequest, *, provider: str) -> str:
    """Canonical serialisation: sorted keys, no whitespace, UTF-8 kept as is."""
    return json.dumps(
        attempt_input_document(request, provider=provider),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def hash_attempt_input(request: LLMRequest, *, provider: str) -> str:
    """SHA-256 hex of :func:`canonical_attempt_input`."""
    canonical = canonical_attempt_input(request, provider=provider)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
