"""Unit tests for :mod:`course_supporter.llm.token_budget`.

Pins the chars-per-token ratio (~3.5) so a future tweak surfaces as
a test diff rather than a silent change in router skip thresholds.
"""

from __future__ import annotations

from course_supporter.llm.token_budget import estimate_tokens


class TestEstimateTokens:
    """``estimate_tokens`` is a deterministic chars/3.5 estimate."""

    def test_empty_prompt_returns_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_estimate_uses_chars_per_token_ratio(self) -> None:
        # 35 chars / 3.5 = 10 tokens (exact integer at the boundary).
        assert estimate_tokens("a" * 35) == 10

    def test_system_prompt_is_added_to_total(self) -> None:
        prompt = "a" * 14  # 4 tokens at 3.5 chars/token
        system_prompt = "b" * 21  # 6 tokens
        # 14 + 21 = 35 chars / 3.5 = 10 tokens.
        assert estimate_tokens(prompt, system_prompt) == 10

    def test_system_prompt_none_treated_as_absent(self) -> None:
        assert estimate_tokens("a" * 35, None) == 10

    def test_truncates_to_int(self) -> None:
        # 5 chars / 3.5 = 1.428... → floor via int() → 1.
        assert estimate_tokens("hello") == 1
