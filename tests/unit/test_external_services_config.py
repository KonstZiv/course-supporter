"""Regression guards for ``config/external_services.yaml`` routing chains.

These tests load the real registry file and pin invariants that must hold
so that specific production failure modes cannot recur unnoticed after
future config edits.

Covered incidents
-----------------
- 2026-04-14: a 4-material architect input hit DeepSeek's 8192
  ``max_output_tokens`` cap with ``finish_reason="length"`` and
  ``course_structuring.default = [gemini-2.5-flash, deepseek-chat]``
  had no further fallback, cascading into full subtree generation loss.
  Sonnet (16384 output) is now required as the rescue step for any
  ``structured_output`` action whose chain includes a cheap model with
  ``max_output_tokens <= 8192``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from course_supporter.llm.registry import ModelRegistryConfig, load_registry

# Path to the real production registry file.
_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "external_services.yaml"
)

# The rescue model every structured-output chain must fall back to when
# cheaper primaries truncate at 8192 tokens.
_RESCUE_MODEL_ID = "claude-sonnet-4-20250514"

# DeepSeek's hard-capped max_output_tokens (see deepseek-chat comment in
# external_services.yaml). Primaries at or below this cap need a higher-
# capacity follower to recover from ``finish_reason="length"``.
_SMALL_OUTPUT_CAP = 8192

# Actions that produce long structured output (full course subtrees,
# per-node methodological documents) and therefore must have the rescue
# model as a terminal fallback in the ``default`` strategy.
_LONG_STRUCTURED_ACTIONS: tuple[str, ...] = (
    "course_structuring",
    "methodist",
)


@pytest.fixture(scope="module")
def registry() -> ModelRegistryConfig:
    """Load the real external_services.yaml once per module."""
    return load_registry(_REGISTRY_PATH)


class TestRescueFallback:
    """Invariants around the Sonnet rescue step."""

    @pytest.mark.parametrize("action", _LONG_STRUCTURED_ACTIONS)
    def test_rescue_model_is_present_in_default_chain(
        self,
        registry: ModelRegistryConfig,
        action: str,
    ) -> None:
        chain = [m.model_id for m in registry.get_chain(action, strategy="default")]
        assert _RESCUE_MODEL_ID in chain, (
            f"{action}.default must include {_RESCUE_MODEL_ID} as the "
            f"rescue step — see 2026-04-14 incident. Current chain: {chain}"
        )

    @pytest.mark.parametrize("action", _LONG_STRUCTURED_ACTIONS)
    def test_rescue_model_is_not_first(
        self,
        registry: ModelRegistryConfig,
        action: str,
    ) -> None:
        """Sonnet is the rescue, not the primary — it must run after cheaper
        models so normal traffic does not pay Sonnet pricing.
        """
        chain = [m.model_id for m in registry.get_chain(action, strategy="default")]
        assert chain.index(_RESCUE_MODEL_ID) > 0, (
            f"{action}.default puts {_RESCUE_MODEL_ID} first — "
            f"cheaper primaries should be tried before the rescue. "
            f"Current chain: {chain}"
        )

    @pytest.mark.parametrize("action", _LONG_STRUCTURED_ACTIONS)
    def test_chain_covers_any_small_output_primary(
        self,
        registry: ModelRegistryConfig,
        action: str,
    ) -> None:
        """If any primary in the chain has ``max_output_tokens <= 8192``
        (the DeepSeek cap that triggered the 2026-04-14 incident), there
        must be a later model with strictly more output capacity. The
        rescue step is what prevents ``finish_reason="length"`` from
        terminating the whole chain.
        """
        chain = registry.get_chain(action, strategy="default")

        for i, m in enumerate(chain):
            if m.max_output_tokens is None:
                continue
            if m.max_output_tokens > _SMALL_OUTPUT_CAP:
                continue
            # Found a small-output primary — need a bigger follower.
            bigger_follows = any(
                (later.max_output_tokens or 0) > m.max_output_tokens
                for later in chain[i + 1 :]
            )
            assert bigger_follows, (
                f"{action}.default has '{m.model_id}' with "
                f"max_output_tokens={m.max_output_tokens} and no later "
                f"model with a larger cap. A 'finish_reason=length' "
                f"failure would terminate the chain with no recovery."
            )


class TestCourseStructuringChainShape:
    """Shape of the course_structuring.default chain.

    Pinned explicitly because this chain was the site of the 2026-04-14
    cascading failure. Any future change must be deliberate.
    """

    def test_default_chain_ends_with_rescue(
        self,
        registry: ModelRegistryConfig,
    ) -> None:
        chain = [
            m.model_id
            for m in registry.get_chain("course_structuring", strategy="default")
        ]
        assert chain[-1] == _RESCUE_MODEL_ID, (
            f"course_structuring.default must end with {_RESCUE_MODEL_ID}; got {chain}"
        )

    def test_default_chain_includes_cheap_primary(
        self,
        registry: ModelRegistryConfig,
    ) -> None:
        """At least one primary (non-rescue) model must be cheaper than
        Sonnet so the default strategy actually saves money on normal
        traffic.
        """
        chain = registry.get_chain("course_structuring", strategy="default")
        rescue = next(m for m in chain if m.model_id == _RESCUE_MODEL_ID)
        primaries = [m for m in chain if m.model_id != _RESCUE_MODEL_ID]
        assert primaries, "course_structuring.default has no primaries"
        cheaper = [
            m
            for m in primaries
            if m.cost_per_1k.input < rescue.cost_per_1k.input
            and m.cost_per_1k.output < rescue.cost_per_1k.output
        ]
        assert cheaper, (
            "course_structuring.default has no primary cheaper than "
            "the rescue — the chain would always pay rescue pricing."
        )
