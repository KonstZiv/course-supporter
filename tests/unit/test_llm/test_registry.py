"""Tests for unified external service registry."""

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
    """Minimal valid registry config in new format."""
    return {
        "providers": {
            "gemini": {
                "type": "llm",
                "models": [
                    {
                        "id": "model-a",
                        "capabilities": ["vision", "structured_output"],
                        "max_context": 100000,
                        "unit_type": "tokens",
                        "cost_per_1k_in": 0.001,
                        "cost_per_1k_out": 0.002,
                    },
                ],
            },
            "deepseek": {
                "type": "llm",
                "models": [
                    {
                        "id": "model-b",
                        "capabilities": ["structured_output"],
                        "max_context": 65000,
                        "unit_type": "tokens",
                        "cost_per_1k_in": 0.0001,
                        "cost_per_1k_out": 0.0002,
                    },
                ],
            },
        },
        "strategies": {
            "default": {"providers": ["gemini", "deepseek"], "fallback": True},
        },
        "actions": {
            "analyze": {
                "strategy": "default",
                "requires": ["structured_output"],
                "chain": {
                    "default": ["model-a", "model-b"],
                    "budget": ["model-b"],
                },
            },
            "see": {
                "strategy": "default",
                "requires": ["vision", "structured_output"],
                "chain": {
                    "default": ["model-a"],
                },
            },
        },
    }


class TestModelConfig:
    def test_estimate_cost(self) -> None:
        m = ModelConfig(
            provider="test",
            capabilities=["structured_output"],
            max_context=100000,
            cost_per_1k={"input": 0.001, "output": 0.002},  # type: ignore[arg-type]
        )
        cost = m.estimate_cost(tokens_in=1000, tokens_out=500)
        assert cost == pytest.approx(0.002)

    def test_estimate_cost_zero_tokens(self) -> None:
        m = ModelConfig(
            provider="test",
            capabilities=["structured_output"],
            max_context=100000,
            cost_per_1k={"input": 0.001, "output": 0.002},  # type: ignore[arg-type]
        )
        assert m.estimate_cost(0, 0) == 0.0


class TestRegistryValidation:
    def test_valid_config_loads(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        assert len(cfg.models) == 2
        assert len(cfg.actions) == 2
        assert len(cfg.providers) == 2

    def test_model_id_populated(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        assert cfg.models["model-a"].model_id == "model-a"
        assert cfg.models["model-b"].model_id == "model-b"

    def test_provider_populated(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        assert cfg.models["model-a"].provider == "gemini"
        assert cfg.models["model-b"].provider == "deepseek"

    def test_unit_type_populated(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        assert cfg.models["model-a"].unit_type == "tokens"

    def test_cost_flattened(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        m = cfg.models["model-a"]
        assert m.cost_per_1k.input == pytest.approx(0.001)
        assert m.cost_per_1k.output == pytest.approx(0.002)

    def test_unknown_model_in_chain(self, valid_config: dict) -> None:
        valid_config["actions"]["analyze"]["chain"]["default"] = ["nonexistent"]
        with pytest.raises(ValueError, match="unknown model"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_missing_default_chain(self, valid_config: dict) -> None:
        valid_config["actions"]["analyze"]["chain"] = {"quality": ["model-a"]}
        with pytest.raises(ValueError, match="'default' entry"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_capability_mismatch(self, valid_config: dict) -> None:
        # model-b lacks vision, but "see" requires it
        valid_config["actions"]["see"]["chain"]["default"] = ["model-b"]
        with pytest.raises(ValueError, match="lacks required capabilities"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_empty_chain(self, valid_config: dict) -> None:
        valid_config["actions"]["analyze"]["chain"]["default"] = []
        with pytest.raises(ValueError, match="empty model chain"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_no_chain_defined(self, valid_config: dict) -> None:
        valid_config["actions"]["analyze"]["chain"] = {}
        with pytest.raises(ValueError, match="has no chain"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_multiple_errors_collected(self, valid_config: dict) -> None:
        valid_config["actions"]["analyze"]["chain"]["default"] = ["nonexistent"]
        valid_config["actions"]["see"]["chain"]["default"] = ["model-b"]
        with pytest.raises(ValueError) as exc_info:
            ModelRegistryConfig.model_validate(valid_config)
        msg = str(exc_info.value)
        assert "unknown model" in msg
        assert "lacks required capabilities" in msg


class TestGetChain:
    def test_default_strategy(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        chain = cfg.get_chain("analyze")
        assert [m.model_id for m in chain] == ["model-a", "model-b"]

    def test_named_strategy(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        chain = cfg.get_chain("analyze", strategy="budget")
        assert [m.model_id for m in chain] == ["model-b"]

    def test_unknown_strategy_falls_back_to_default(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        chain = cfg.get_chain("analyze", strategy="nonexistent")
        assert [m.model_id for m in chain] == ["model-a", "model-b"]

    def test_unknown_action_raises(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        with pytest.raises(KeyError, match="Unknown action"):
            cfg.get_chain("nonexistent")

    def test_chain_items_have_provider(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        chain = cfg.get_chain("analyze")
        assert chain[0].provider == "gemini"
        assert chain[1].provider == "deepseek"

    def test_chain_items_have_estimate_cost(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        chain = cfg.get_chain("analyze")
        cost = chain[0].estimate_cost(1000, 500)
        assert cost > 0


class TestGetAvailableStrategies:
    def test_list_strategies(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        strategies = cfg.get_available_strategies("analyze")
        assert "default" in strategies
        assert "budget" in strategies

    def test_unknown_action_raises(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        with pytest.raises(KeyError, match="Unknown action"):
            cfg.get_available_strategies("nonexistent")


class TestLoadRegistry:
    def test_load_from_file(self, valid_config: dict, tmp_path: Path) -> None:
        p = tmp_path / "services.yaml"
        p.write_text(yaml.dump(valid_config), encoding="utf-8")
        cfg = load_registry(p)
        assert len(cfg.models) == 2

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_registry(tmp_path / "nope.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.yaml"
        p.write_text("providers:\n  - :\n  bad: [unclosed", encoding="utf-8")
        with pytest.raises(ValueError, match="Failed to parse"):
            load_registry(p)

    def test_real_config_file(self) -> None:
        """Validate the actual config/external_services.yaml."""
        cfg_path = Path("config/external_services.yaml")
        if cfg_path.exists():
            cfg = load_registry(cfg_path)
            assert len(cfg.models) > 0
            assert len(cfg.actions) > 0
