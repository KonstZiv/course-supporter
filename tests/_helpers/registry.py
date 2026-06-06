"""Shared registry fixtures for StageRouter tests (TASK-2.4.22).

``StageRouter.__init__`` requires a ``ModelRegistryConfig`` (keyword-only,
no default) to compute ESC ``cost_usd`` from per-model pricing and to
fall back to the registry's ``max_output_tokens`` cap when a ladder rung
is unpinned. Tests that exercise the router's control flow (ladder
fallback, ESC plumbing, structural retry) don't care about pricing —
they pass an empty registry through :func:`empty_registry`.

Tests that exercise the cost / max_tokens fallback themselves build a
populated registry inline (see ``TestRegistryAwareCost`` /
``TestRegistryAwareMaxTokens`` in ``test_stage_router.py``).
"""

from __future__ import annotations

from course_supporter.llm.registry import (
    ModelRegistryConfig,
    ProviderConfig,
    ProviderModelConfig,
)


def empty_registry() -> ModelRegistryConfig:
    """Return a registry with no providers / actions.

    Validates cleanly: ``validate_and_flatten`` iterates ``providers``
    (empty) and ``actions`` (empty) and raises nothing. Use this fixture
    in tests whose subject is not the cost / max_tokens path.
    """
    return ModelRegistryConfig(providers={}, actions={})


def registry_with(
    *,
    model_id: str = "claude-x",
    provider: str = "anthropic",
    cost_per_1k_in: float = 0.0,
    cost_per_1k_out: float = 0.0,
    max_output_tokens: int | None = None,
) -> ModelRegistryConfig:
    """Build a registry containing exactly one model entry.

    Use this in tests that exercise the F(a) cost-from-registry path or
    the H max_tokens registry fallback — both behaviours look up the
    request's ``model_id`` in ``registry.models``.
    """
    return ModelRegistryConfig(
        providers={
            provider: ProviderConfig(
                type="llm",
                models=[
                    ProviderModelConfig(
                        id=model_id,
                        cost_per_1k_in=cost_per_1k_in,
                        cost_per_1k_out=cost_per_1k_out,
                        max_output_tokens=max_output_tokens,
                    )
                ],
            )
        },
        actions={},
    )
