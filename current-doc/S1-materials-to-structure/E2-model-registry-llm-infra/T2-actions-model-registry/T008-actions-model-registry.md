# 📋 S1-008: Actions & Model Registry

## Мета

YAML-конфігурація з трьома секціями: **models** (доступні моделі з capabilities та вартістю), **actions** (перелік завдань з вимогами), **routing** (action → strategies → ordered model chains). Валідація при старті: модель у routing повинна мати capabilities, які вимагає action.

## Контекст

Залежить від S1-007 (providers). Використовується ModelRouter-ом (S1-009) для вибору моделей. Розширення: новий action = рядок у YAML, нова модель = рядок у YAML, нова strategy = рядок у YAML.

---

## Acceptance Criteria

- [ ] `config/models.yaml` містить секції: `models`, `actions`, `routing`
- [ ] Pydantic-схеми валідують YAML при завантаженні
- [ ] `actions` декларують вимоги (`requires: [vision, structured_output]`)
- [ ] `routing` підтримує named strategies: `default`, `quality`, `budget` та довільні
- [ ] Валідація: модель у routing chain має capabilities з `requires` відповідного action
- [ ] `get_chain("video_analysis")` → default chain як `list[ModelConfig]`
- [ ] `get_chain("video_analysis", strategy="quality")` → quality chain
- [ ] `ModelConfig.estimate_cost(tokens_in, tokens_out)` → USD
- [ ] `ModelConfig` має `.model_id`, `.provider`, `.estimate_cost()` — інтерфейс для ModelRouter (S1-009)
- [ ] Невалідний YAML → зрозуміла `ValidationError` при старті
- [ ] Додати новий action/model/strategy = зміна YAML, без зміни коду

---

## Конфігурація

### config/models.yaml

```yaml
# ============================================================
# Models: available LLM models with capabilities and pricing
# ============================================================
models:
  gemini-2.5-flash:
    provider: gemini
    capabilities: [vision, structured_output, long_context]
    max_context: 1048576
    cost_per_1k:
      input: 0.00015
      output: 0.0006

  gemini-2.5-pro:
    provider: gemini
    capabilities: [vision, structured_output, long_context]
    max_context: 1048576
    cost_per_1k:
      input: 0.00125
      output: 0.005

  claude-sonnet:
    provider: anthropic
    capabilities: [structured_output, long_context]
    max_context: 200000
    cost_per_1k:
      input: 0.003
      output: 0.015

  deepseek-chat:
    provider: deepseek
    capabilities: [structured_output]
    max_context: 65536
    cost_per_1k:
      input: 0.00014
      output: 0.00028

  gpt-4o-mini:
    provider: openai
    capabilities: [vision, structured_output]
    max_context: 128000
    cost_per_1k:
      input: 0.00015
      output: 0.0006

# ============================================================
# Actions: task types with capability requirements
# ============================================================
actions:
  video_analysis:
    description: "Transcribe and analyze video content"
    requires: [vision, long_context]

  presentation_analysis:
    description: "Extract and analyze presentation content"
    requires: [vision, structured_output]

  course_structuring:
    description: "Generate course structure from materials"
    requires: [structured_output]

  text_processing:
    description: "Process and transform text content"
    requires: [structured_output]

# ============================================================
# Routing: action → strategies → ordered model chains
# ============================================================
routing:
  video_analysis:
    default:
      - gemini-2.5-flash
      - gemini-2.5-pro
    quality:
      - gemini-2.5-pro
      - gemini-2.5-flash
    budget:
      - gemini-2.5-flash

  presentation_analysis:
    default:
      - gemini-2.5-flash
      - gpt-4o-mini
    quality:
      - gemini-2.5-pro
      - gemini-2.5-flash

  course_structuring:
    default:
      - gemini-2.5-flash
      - deepseek-chat
    quality:
      - claude-sonnet
      - gemini-2.5-pro
    budget:
      - deepseek-chat

  text_processing:
    default:
      - deepseek-chat
      - gemini-2.5-flash
    quality:
      - claude-sonnet
      - gemini-2.5-flash
    budget:
      - deepseek-chat
```

---

## Pydantic-схеми

### src/course_supporter/llm/registry.py

```python
"""Model registry: models, actions, routing with strategy support.

Loaded from config/models.yaml at startup, validated by Pydantic.
Extensible: new action/model/strategy = YAML edit, no code changes.
"""

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator


class ModelCapability(StrEnum):
    """Capabilities a model can have."""

    VISION = "vision"
    STRUCTURED_OUTPUT = "structured_output"
    LONG_CONTEXT = "long_context"


class CostPer1K(BaseModel):
    """Cost per 1000 tokens in USD."""

    input: float
    output: float


class ModelConfig(BaseModel):
    """Single model configuration."""

    provider: str
    capabilities: list[ModelCapability]
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
    requires: list[ModelCapability] = []


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
        errors: list[str] = []

        for action_name, strategies in self.routing.items():
            # Action must exist
            if action_name not in self.actions:
                errors.append(
                    f"Routing references unknown action: '{action_name}'"
                )
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
                        f"Action '{action_name}' strategy '{strategy_name}' "
                        f"has empty model chain"
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

    def get_models_for_action(
        self,
        action: str,
        strategy: str = "default",
    ) -> list[tuple[str, ModelConfig]]:
        """Get ordered model chain for action + strategy.

        Falls back to 'default' strategy if requested strategy not found.

        Raises:
            KeyError: if action not found in routing.
        """
        if action not in self.routing:
            raise KeyError(f"Unknown action: '{action}'")

        strategies = self.routing[action]
        chain = strategies.get(strategy) or strategies["default"]
        return [(mid, self.models[mid]) for mid in chain]

    def get_available_strategies(self, action: str) -> list[str]:
        """List available strategies for an action."""
        if action not in self.routing:
            raise KeyError(f"Unknown action: '{action}'")
        return list(self.routing[action].keys())

    def estimate_cost(
        self,
        model_id: str,
        tokens_in: int,
        tokens_out: int,
    ) -> float:
        """Estimate cost in USD for a specific model."""
        return self.models[model_id].estimate_cost(tokens_in, tokens_out)


def load_registry(config_path: Path | None = None) -> ModelRegistryConfig:
    """Load and validate model registry from YAML.

    Raises:
        FileNotFoundError: if YAML file doesn't exist.
        ValueError: if validation fails.
    """
    if config_path is None:
        config_path = Path("config/models.yaml")

    if not config_path.exists():
        raise FileNotFoundError(f"Registry config not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return ModelRegistryConfig.model_validate(raw)
```

---

## Тести

### tests/unit/test_llm/test_registry.py

```python
"""Tests for model registry configuration."""

from pathlib import Path

import pytest
import yaml

from course_supporter.llm.registry import (
    ModelConfig,
    ModelRegistryConfig,
    load_registry,
)


@pytest.fixture
def valid_config() -> dict:
    """Minimal valid registry config."""
    return {
        "models": {
            "model-a": {
                "provider": "gemini",
                "capabilities": ["vision", "structured_output"],
                "max_context": 100000,
                "cost_per_1k": {"input": 0.001, "output": 0.002},
            },
            "model-b": {
                "provider": "deepseek",
                "capabilities": ["structured_output"],
                "max_context": 65000,
                "cost_per_1k": {"input": 0.0001, "output": 0.0002},
            },
        },
        "actions": {
            "analyze": {
                "description": "Analyze content",
                "requires": ["structured_output"],
            },
            "see": {
                "description": "Vision task",
                "requires": ["vision", "structured_output"],
            },
        },
        "routing": {
            "analyze": {
                "default": ["model-a", "model-b"],
                "budget": ["model-b"],
            },
            "see": {
                "default": ["model-a"],
            },
        },
    }


class TestModelConfig:
    def test_estimate_cost(self) -> None:
        m = ModelConfig(
            provider="test",
            capabilities=["structured_output"],
            max_context=100000,
            cost_per_1k={"input": 0.001, "output": 0.002},
        )
        cost = m.estimate_cost(tokens_in=1000, tokens_out=500)
        assert cost == pytest.approx(0.002)


class TestRegistryValidation:
    def test_valid_config_loads(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        assert len(cfg.models) == 2
        assert len(cfg.actions) == 2
        assert len(cfg.routing) == 2

    def test_unknown_model_in_routing(self, valid_config: dict) -> None:
        valid_config["routing"]["analyze"]["default"] = ["nonexistent"]
        with pytest.raises(ValueError, match="unknown model"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_unknown_action_in_routing(self, valid_config: dict) -> None:
        valid_config["routing"]["unknown_action"] = {"default": ["model-a"]}
        with pytest.raises(ValueError, match="unknown action"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_missing_default_strategy(self, valid_config: dict) -> None:
        valid_config["routing"]["analyze"] = {"quality": ["model-a"]}
        with pytest.raises(ValueError, match="must have 'default' strategy"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_capability_mismatch(self, valid_config: dict) -> None:
        valid_config["routing"]["see"]["default"] = ["model-b"]
        with pytest.raises(ValueError, match="lacks required capabilities"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_empty_chain(self, valid_config: dict) -> None:
        valid_config["routing"]["analyze"]["default"] = []
        with pytest.raises(ValueError, match="empty model chain"):
            ModelRegistryConfig.model_validate(valid_config)


class TestGetModelsForAction:
    def test_default_strategy(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        chain = cfg.get_models_for_action("analyze")
        assert [mid for mid, _ in chain] == ["model-a", "model-b"]

    def test_named_strategy(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        chain = cfg.get_models_for_action("analyze", strategy="budget")
        assert [mid for mid, _ in chain] == ["model-b"]

    def test_unknown_strategy_falls_back(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        chain = cfg.get_models_for_action("analyze", strategy="nonexistent")
        assert [mid for mid, _ in chain] == ["model-a", "model-b"]

    def test_unknown_action_raises(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        with pytest.raises(KeyError, match="Unknown action"):
            cfg.get_models_for_action("nonexistent")


class TestGetAvailableStrategies:
    def test_list_strategies(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        strategies = cfg.get_available_strategies("analyze")
        assert "default" in strategies
        assert "budget" in strategies


class TestLoadRegistry:
    def test_load_from_file(self, valid_config: dict, tmp_path: Path) -> None:
        p = tmp_path / "models.yaml"
        p.write_text(yaml.dump(valid_config), encoding="utf-8")
        cfg = load_registry(p)
        assert len(cfg.models) == 2

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_registry(tmp_path / "nope.yaml")

    def test_real_config_file(self) -> None:
        """Validate the actual config/models.yaml."""
        cfg_path = Path("config/models.yaml")
        if cfg_path.exists():
            cfg = load_registry(cfg_path)
            assert len(cfg.models) > 0
            assert len(cfg.actions) > 0
```

---

## Структура файлів

```
src/course_supporter/llm/
    registry.py               # ModelRegistryConfig, load_registry()
config/
    models.yaml               # models + actions + routing
tests/unit/test_llm/
    test_registry.py
```

---

## Кроки виконання

1. Створити `config/models.yaml`
2. Створити `llm/registry.py`
3. Створити `tests/unit/test_llm/test_registry.py`
4. `make check`

---

## Примітки

- **Стратегії** — довільні імена, не enum. `default` обов'язкова, решта — конвенція.
- **Fallback між strategies** — не тут, а в ModelRouter (S1-009). Registry тільки дає chain для конкретної strategy.
- **Невідома strategy** → fallback на default chain, не помилка. Forward compatibility.
- **Валідація capabilities** — при завантаженні YAML, не в runtime. Невалідний конфіг = додаток не стартує.

---

## Адаптація під S1-009 (ModelRouter)

Зміни відносно оригінальної специфікації для сумісності з S1-009:

1. **`ModelCapability` StrEnum → `Capability`** — звільняє ім'я `ModelCapability` (S1-009 використовує його інакше). Фактично S1-009 працює з `ModelConfig` напряму.
2. **`get_models_for_action()` → `get_chain()`** — метод, який S1-009 викликає. Повертає `list[ModelConfig]` (не tuples).
3. **`ModelConfig.model_id`** — заповнюється з dict key при валідації. Router потребує `.model_id`, `.provider`, `.estimate_cost()` на кожному елементі chain.
4. **S1-009 буде імпортувати** `ModelConfig` та `ModelRegistryConfig` з `registry.py` (не `ModelCapability`).
