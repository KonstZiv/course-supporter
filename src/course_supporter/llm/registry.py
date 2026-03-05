"""Unified external service registry: providers, models, actions, routing.

Loaded from config/external_services.yaml at startup, validated by Pydantic.
Extensible: new action/model/strategy = YAML edit, no code changes.

ModelRouter uses get_chain() to obtain ordered list of
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


class ProviderModelConfig(BaseModel):
    """Single model within a provider definition."""

    id: str
    capabilities: list[Capability] = []
    max_context: int = 0
    unit_type: str = "tokens"
    cost_per_1k_in: float = 0.0
    cost_per_1k_out: float = 0.0
    local: bool = False


class ProviderConfig(BaseModel):
    """Provider with its models."""

    type: str  # llm, transcription, scraping
    models: list[ProviderModelConfig]


class StrategyConfig(BaseModel):
    """Named strategy with provider preference."""

    providers: list[str]
    fallback: bool = True


class ActionConfig(BaseModel):
    """Action with strategy reference and model chains."""

    strategy: str = "default"
    requires: list[Capability] = []
    chain: dict[str, list[str]] = {}


# ── Flattened model config for ModelRouter compatibility ──


class CostPer1K(BaseModel):
    """Cost per 1000 tokens in USD."""

    input: float
    output: float


class ModelConfig(BaseModel):
    """Flattened model configuration used by ModelRouter.

    Built from provider + model data during registry validation.
    Provides .model_id, .provider, .unit_type, and .estimate_cost().
    """

    model_id: str = ""
    provider: str = ""
    capabilities: list[Capability] = []
    max_context: int = 0
    unit_type: str = "tokens"
    cost_per_1k: CostPer1K = CostPer1K(input=0.0, output=0.0)
    local: bool = False

    def estimate_cost(self, tokens_in: int, tokens_out: int) -> float:
        """Calculate cost in USD for given token/unit counts."""
        return (
            tokens_in * self.cost_per_1k.input / 1000
            + tokens_out * self.cost_per_1k.output / 1000
        )


# ── Top-level registry ──


class ModelRegistryConfig(BaseModel):
    """Unified external service registry.

    Parses the providers/strategies/actions YAML structure and
    flattens provider models into a dict[model_id, ModelConfig]
    for backward compatibility with ModelRouter.

    Validates that:
    - All models referenced in action chains exist
    - All models in chains have required capabilities
    - Every action chain has at least a 'default' entry
    """

    providers: dict[str, ProviderConfig]
    strategies: dict[str, StrategyConfig] = {}
    actions: dict[str, ActionConfig]

    # Flattened lookup (populated in validator)
    models: dict[str, ModelConfig] = {}
    # Routing (populated in validator for get_chain compat)
    routing: dict[str, dict[str, list[str]]] = {}

    @model_validator(mode="after")
    def validate_and_flatten(self) -> "ModelRegistryConfig":
        """Flatten provider models and validate action chains."""
        # 1. Flatten providers → models dict
        for provider_name, provider in self.providers.items():
            for pm in provider.models:
                self.models[pm.id] = ModelConfig(
                    model_id=pm.id,
                    provider=provider_name,
                    capabilities=pm.capabilities,
                    max_context=pm.max_context,
                    unit_type=pm.unit_type,
                    cost_per_1k=CostPer1K(
                        input=pm.cost_per_1k_in,
                        output=pm.cost_per_1k_out,
                    ),
                    local=pm.local,
                )

        # 2. Build routing from action chains + validate
        errors: list[str] = []

        for action_name, action in self.actions.items():
            if not action.chain:
                errors.append(f"Action '{action_name}' has no chain defined")
                continue

            if "default" not in action.chain:
                errors.append(f"Action '{action_name}' chain must have 'default' entry")

            self.routing[action_name] = action.chain

            for strategy_name, model_chain in action.chain.items():
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
                "Registry validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
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
    """Load and validate service registry from YAML.

    Args:
        config_path: Path to external_services.yaml.

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
