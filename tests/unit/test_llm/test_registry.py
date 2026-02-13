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
        assert len(cfg.routing) == 2

    def test_model_id_populated_from_key(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        assert cfg.models["model-a"].model_id == "model-a"
        assert cfg.models["model-b"].model_id == "model-b"

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
        with pytest.raises(ValueError, match="'default' strategy"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_capability_mismatch(self, valid_config: dict) -> None:
        # model-b lacks vision, but "see" requires it
        valid_config["routing"]["see"]["default"] = ["model-b"]
        with pytest.raises(ValueError, match="lacks required capabilities"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_empty_chain(self, valid_config: dict) -> None:
        valid_config["routing"]["analyze"]["default"] = []
        with pytest.raises(ValueError, match="empty model chain"):
            ModelRegistryConfig.model_validate(valid_config)

    def test_multiple_errors_collected(self, valid_config: dict) -> None:
        valid_config["routing"]["analyze"]["default"] = ["nonexistent"]
        valid_config["routing"]["see"]["default"] = ["model-b"]
        with pytest.raises(ValueError) as exc_info:
            ModelRegistryConfig.model_validate(valid_config)
        # Both errors should be in the message
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


class TestEstimateCost:
    def test_estimate_by_model_id(self, valid_config: dict) -> None:
        cfg = ModelRegistryConfig.model_validate(valid_config)
        cost = cfg.estimate_cost("model-a", tokens_in=1000, tokens_out=500)
        assert cost == pytest.approx(0.002)


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
