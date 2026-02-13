"""Model registry: models, actions, routing with strategy support.

Loaded from config/models.yaml at startup, validated by Pydantic.
Extensible: new action/model/strategy = YAML edit, no code changes.

ModelRouter (S1-009) uses get_chain() to obtain ordered list of
ModelConfig objects with .provider, .model_id, .estimate_cost().
"""

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator


class Capability(StrEnum):
    """Capabilities a model can have."""

    VISION = "vision"
    STRUCTURED_OUTPUT = "structured_output"
    LONG_CONTEXT = "long_context"


class CostPer1K(BaseModel):
    """Cost per 1000 tokens in USD."""

    input: float
    output: float


class ModelConfig(BaseModel):
    """Single model configuration.

    Used directly by ModelRouter as chain item — provides
    .model_id, .provider, and .estimate_cost() interface.
    """

    model_id: str = ""  # populated from dict key during validation
    provider: str
    capabilities: list[Capability]
    max_context: int
    cost_per_1k: CostPer1K

    def estimate_cost(self, tokens_in: int, tokens_out: int) -> float:
        """Calculate cost in USD for given token counts."""
        return (
            tokens_in * self.cost_per_1k.input / 1000
            + tokens_out * self.cost_per_1k.output / 1000
        )


class ActionConfig(BaseModel):
    """Action (task type) with capability requirements."""

    description: str = ""
    requires: list[Capability] = []


class ModelRegistryConfig(BaseModel):
    """Top-level registry: models + actions + routing.

    Validates that:
    - All models referenced in routing exist in models section
    - All models in routing have capabilities required by the action
    - All actions in routing exist in actions section
    - Every routing entry has at least a 'default' strategy
    """

    models: dict[str, ModelConfig]
    actions: dict[str, ActionConfig]
    routing: dict[str, dict[str, list[str]]]

    @model_validator(mode="after")
    def validate_routing(self) -> "ModelRegistryConfig":
        """Populate model_id fields and validate routing consistency."""
        # Populate model_id from dict keys
        for model_id, model in self.models.items():
            model.model_id = model_id

        errors: list[str] = []

        for action_name, strategies in self.routing.items():
            # Action must exist
            if action_name not in self.actions:
                errors.append(f"Routing references unknown action: '{action_name}'")
                continue

            # Must have default strategy
            if "default" not in strategies:
                errors.append(
                    f"Action '{action_name}' routing must have 'default' strategy"
                )

            action = self.actions[action_name]

            for strategy_name, model_chain in strategies.items():
                if not model_chain:
                    errors.append(
                        f"Action '{action_name}' strategy "
                        f"'{strategy_name}' has empty model chain"
                    )
                    continue

                for model_id in model_chain:
                    if model_id not in self.models:
                        errors.append(
                            f"Routing '{action_name}.{strategy_name}' "
                            f"references unknown model: '{model_id}'"
                        )
                        continue

                    model = self.models[model_id]
                    missing = set(action.requires) - set(model.capabilities)
                    if missing:
                        errors.append(
                            f"Model '{model_id}' in "
                            f"'{action_name}.{strategy_name}' "
                            f"lacks required capabilities: {missing}"
                        )

        if errors:
            raise ValueError(
                "Model registry validation failed:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )
        return self

    def get_chain(
        self,
        action: str,
        strategy: str = "default",
    ) -> list[ModelConfig]:
        """Get ordered model chain for action + strategy.

        Falls back to 'default' strategy if requested strategy not found.

        Raises:
            KeyError: if action not found in routing.
        """
        if action not in self.routing:
            raise KeyError(f"Unknown action: '{action}'")

        strategies = self.routing[action]
        chain = strategies.get(strategy) or strategies["default"]
        return [self.models[mid] for mid in chain]

    def get_available_strategies(self, action: str) -> list[str]:
        """List available strategies for an action."""
        if action not in self.routing:
            raise KeyError(f"Unknown action: '{action}'")
        return list(self.routing[action].keys())


def load_registry(config_path: Path) -> ModelRegistryConfig:
    """Load and validate model registry from YAML.

    Args:
        config_path: Path to models.yaml. Typically comes from
            Settings.model_registry_path.

    Raises:
        FileNotFoundError: if YAML file doesn't exist.
        ValueError: if YAML parsing or validation fails.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Registry config not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse registry config '{config_path}': {e}") from e
    return ModelRegistryConfig.model_validate(raw)
