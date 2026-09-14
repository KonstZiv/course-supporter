"""Canonical attempt input and its hash (mentor-rebuild task 01)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from course_supporter.llm.input_hash import (
    canonical_attempt_input,
    hash_attempt_input,
)
from course_supporter.llm.schemas import LLMRequest

_BASE: dict[str, Any] = {
    "prompt": "user text",
    "system_prompt": "system text",
    "model": "deepseek-v4-pro",
    "temperature": 0.0,
    "max_tokens": 8192,
    "reasoning": None,
    "expects_json": True,
    "contents": [b"\x89PNG-one", b"\x89PNG-two"],
}


def _hash(provider: str = "deepseek_thinking", **overrides: Any) -> str:
    return hash_attempt_input(LLMRequest(**{**_BASE, **overrides}), provider=provider)


class TestHashAttemptInput:
    def test_same_input_twice_gives_the_same_hash(self) -> None:
        assert _hash() == _hash()

    def test_non_input_fields_do_not_change_the_hash(self) -> None:
        """``action`` / ``strategy`` label the call; they are not sent content."""
        assert _hash() == _hash(action="criteria_decomposition", strategy="quality")

    @pytest.mark.parametrize(
        ("provider", "overrides"),
        [
            ("deepseek", {}),  # same model name, thinking off instead of on
            ("deepseek_thinking", {"model": "deepseek-v4-flash"}),
            ("deepseek_thinking", {"temperature": 0.7}),
            ("deepseek_thinking", {"max_tokens": 32768}),
            ("deepseek_thinking", {"reasoning": {"exclude": True}}),
            ("deepseek_thinking", {"expects_json": False}),
            ("deepseek_thinking", {"system_prompt": "other system"}),
            ("deepseek_thinking", {"prompt": "user text\n\n[feedback]"}),
            ("deepseek_thinking", {"contents": [b"\x89PNG-one"]}),
            ("deepseek_thinking", {"contents": [b"\x89PNG-two", b"\x89PNG-one"]}),
        ],
        ids=[
            "provider",
            "model",
            "temperature",
            "max-tokens",
            "reasoning",
            "expects-json",
            "system",
            "user",
            "attachment-set",
            "attachment-order",
        ],
    )
    def test_every_output_affecting_part_changes_the_hash(
        self, provider: str, overrides: dict[str, Any]
    ) -> None:
        assert _hash(provider, **overrides) != _hash()

    def test_hash_is_sha256_of_the_canonical_text(self) -> None:
        request = LLMRequest(**_BASE)
        canonical = canonical_attempt_input(request, provider="p")
        assert hash_attempt_input(request, provider="p") == (
            hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        )

    def test_canonical_text_keeps_digests_not_attachment_bytes(self) -> None:
        document = json.loads(
            canonical_attempt_input(LLMRequest(**_BASE), provider="p")
        )
        assert document["attachments"] == [
            hashlib.sha256(b"\x89PNG-one").hexdigest(),
            hashlib.sha256(b"\x89PNG-two").hexdigest(),
        ]

    def test_non_ascii_text_is_kept_verbatim(self) -> None:
        canonical = canonical_attempt_input(
            LLMRequest(prompt="Розв'язок студента"), provider="p"
        )
        assert "Розв'язок студента" in canonical
