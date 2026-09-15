"""Whether one ladder rung is admissible against the model registry.

Purpose:
    The rule "which rung may stand in a ladder" has three registry-facing
    parts, all enforced fail-fast at startup: the rung's reasoning form is one
    its provider's connector can translate (P6), its model is in the registry
    (K), and that model names both token prices (mentor-rebuild task 01). The
    ladder stages apply it through
    :func:`course_supporter.llm.ladder_config.validate_ladders_against_registry`;
    the submission-path configuration (mentor-rebuild task 02) applies the same
    rule. It lives here, apart from the ladder loader, so a second reader of the
    rule does not have to depend on the whole loader to reach three checks.

Interface:
    Input: the rung's location — stage name and rung index, which open every
    message — anything shaped like a rung (:class:`RungLike`: ``provider``,
    ``model``, ``reasoning``), and the registry. Optionally ``model_checks``:
    the caller's own checks against the rung's resolved registry model.

    Output: the error messages in the order the checks run; an empty list
    means the rung is admissible. Nothing is raised — callers aggregate every
    fault of every rung into one startup error, so a config with several typos
    reports all of them at once.

    Worked cases, executed: :mod:`tests.unit.test_llm.test_rung_registry_check`.

Extending:
    A new registry-facing rule is a new check inside
    :func:`rung_registry_errors`, and every caller applies it from then on.
    A check that depends on the caller's own description rather than on the
    registry alone (a ladder stage's required capabilities or input budget)
    stays with that caller and runs through ``model_checks``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from course_supporter.llm.registry import ModelConfig, ModelRegistryConfig


class RungLike(Protocol):
    """What the rule reads from a rung, whichever model describes the rung."""

    @property
    def provider(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def reasoning(self) -> dict[str, Any] | None: ...


def rung_registry_errors(
    stage_name: str,
    index: int,
    rung: RungLike,
    registry: ModelRegistryConfig,
    *,
    model_checks: Callable[[ModelConfig], list[str]] | None = None,
) -> list[str]:
    """Return every registry fault of one rung, in the order the checks run.

    1. Reasoning form (P6) — independent of membership, so a rung with an
       untranslatable form and an unknown model reports both.
    2. Membership (K) — an unknown model ends the rung's checks: nothing else
       can be read without its registry entry.
    3. ``model_checks`` — the caller's checks against the resolved model. They
       run before the price check because that is the order the ladder
       validator has always reported in; moving them would reorder its
       messages.
    4. Named price — an explicit ``0.0`` is a named price (free tier, local
       model); an absent one is not.
    """
    # Deferred import keeps this module, and the ladder loader that imports it,
    # free of the provider SDK graph at module load (importing the providers
    # pulls in every vendor SDK); by validation time (app / worker startup) the
    # providers are already imported. ``PROVIDER_REGISTRY`` maps a provider
    # name → its class, so the reasoning check stays provider-agnostic and the
    # dialect knowledge lives only in the provider modules (P6).
    from course_supporter.llm.providers import PROVIDER_REGISTRY

    errors: list[str] = []

    # A form the connector cannot translate would be silently ignored on the
    # wire; refusing it at boot turns a deploy-time typo into a startup error.
    if rung.reasoning is not None:
        provider_cls = PROVIDER_REGISTRY.get(rung.provider)
        if provider_cls is None or not provider_cls.supports_reasoning(rung.reasoning):
            errors.append(
                f"Stage '{stage_name}' rung {index} provider "
                f"'{rung.provider}' model '{rung.model}' declares a "
                f"reasoning form its connector cannot translate: "
                f"{rung.reasoning!r}"
            )

    if rung.model not in registry.models:
        errors.append(
            f"Stage '{stage_name}' rung {index} "
            f"references unknown model: '{rung.model}'"
        )
        return errors

    model = registry.models[rung.model]
    if model_checks is not None:
        errors.extend(model_checks(model))

    # A rung without a price would write ``cost_usd = NULL`` for every call it
    # makes — the register would stop saying what was paid for, and nothing
    # would say so.
    if model.cost_per_1k is None:
        errors.append(
            f"Stage '{stage_name}' rung {index} model '{rung.model}' "
            f"has no named price in the registry "
            f"(cost_per_1k_in and cost_per_1k_out are both required; "
            f"0.0 is a valid price)"
        )

    return errors
