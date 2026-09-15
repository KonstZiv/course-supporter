"""The rung admissibility rule shared by ladders and submission paths."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from course_supporter.llm.registry import ModelConfig, ModelRegistryConfig
from course_supporter.llm.rung_registry_check import rung_registry_errors

_PRICE_MESSAGE = (
    "Stage 's' rung 0 model 'unpriced' has no named price in the registry "
    "(cost_per_1k_in and cost_per_1k_out are both required; 0.0 is a valid price)"
)


@dataclass(frozen=True)
class _Rung:
    """A rung that is not a ``LadderEntry``: the rule reads shape, not type."""

    provider: str
    model: str
    reasoning: dict[str, Any] | None = None


def _registry() -> ModelRegistryConfig:
    return ModelRegistryConfig.model_validate(
        {
            "providers": {
                "anthropic": {
                    "type": "llm",
                    "models": [
                        {"id": "priced", "cost_per_1k_in": 0.0, "cost_per_1k_out": 0.0},
                        {"id": "unpriced"},
                    ],
                }
            },
            "actions": {},
        }
    )


class TestRungRegistryErrors:
    def test_admissible_rung_of_another_shape_passes(self) -> None:
        assert (
            rung_registry_errors("s", 0, _Rung("anthropic", "priced"), _registry())
            == []
        )

    def test_unknown_model_stops_before_model_checks_and_price(self) -> None:
        seen: list[ModelConfig] = []

        def model_checks(model: ModelConfig) -> list[str]:
            seen.append(model)
            return ["must not appear"]

        errors = rung_registry_errors(
            "s", 3, _Rung("anthropic", "typo"), _registry(), model_checks=model_checks
        )

        assert errors == ["Stage 's' rung 3 references unknown model: 'typo'"]
        assert seen == []

    def test_reasoning_fault_is_reported_before_unknown_model(self) -> None:
        errors = rung_registry_errors(
            "s", 0, _Rung("gemini", "typo", {"exclude": True}), _registry()
        )

        assert errors == [
            "Stage 's' rung 0 provider 'gemini' model 'typo' declares a reasoning "
            "form its connector cannot translate: {'exclude': True}",
            "Stage 's' rung 0 references unknown model: 'typo'",
        ]

    def test_model_checks_run_between_membership_and_price(self) -> None:
        registry = _registry()
        seen: list[ModelConfig] = []

        def model_checks(model: ModelConfig) -> list[str]:
            seen.append(model)
            return ["stage check"]

        errors = rung_registry_errors(
            "s", 0, _Rung("anthropic", "unpriced"), registry, model_checks=model_checks
        )

        assert errors == ["stage check", _PRICE_MESSAGE]
        assert seen == [registry.models["unpriced"]]

    def test_unnamed_price_is_the_only_fault_without_model_checks(self) -> None:
        errors = rung_registry_errors(
            "s", 0, _Rung("anthropic", "unpriced"), _registry()
        )

        assert errors == [_PRICE_MESSAGE]
