"""Regression guards for ``config/external_services.yaml``.

The legacy LLM action chains (course_structuring, methodist,
text_processing, …) and their Sonnet-rescue regression guards were removed
with the DD-20-A dismantle; the live LLM routing surface is StageRouter over
``config/ladders_*.yaml``. What remains in this registry is the model catalog
plus the STT ``transcribe`` action consumed by STTRouter — pinned here so a
bad edit is caught at load time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from course_supporter.homework.path_config import (
    load_path_config,
    validate_path_config,
)
from course_supporter.llm.ladder_config import (
    load_ladder_config,
    validate_ladders_against_registry,
)
from course_supporter.llm.registry import (
    Capability,
    ModelRegistryConfig,
    load_registry,
)

# Path to the real production registry file.
_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "external_services.yaml"
)


@pytest.fixture(scope="module")
def registry() -> ModelRegistryConfig:
    """Load the real external_services.yaml once per module."""
    return load_registry(_REGISTRY_PATH)


class TestRegistryLoads:
    """The production registry file parses and validates at import time."""

    def test_registry_has_models_and_actions(
        self,
        registry: ModelRegistryConfig,
    ) -> None:
        assert registry.models
        assert registry.actions


class TestTranscribeAction:
    """The STT ``transcribe`` action survives the dismantle intact."""

    def test_transcribe_action_present(
        self,
        registry: ModelRegistryConfig,
    ) -> None:
        assert "transcribe" in registry.actions

    def test_transcribe_default_chain_resolves(
        self,
        registry: ModelRegistryConfig,
    ) -> None:
        chain = [
            m.model_id for m in registry.get_chain("transcribe", strategy="default")
        ]
        assert chain == ["scribe_v1", "gpt-4o-mini-transcribe", "nova-3"]

    def test_transcribe_chain_models_are_minute_billed_stt(
        self,
        registry: ModelRegistryConfig,
    ) -> None:
        """Every model in the transcribe chain is a minutes-billed STT model."""
        for m in registry.get_chain("transcribe", strategy="default"):
            assert m.unit_type == "minutes"


class TestOutputCapabilities:
    """Task 09a: ``structured_output`` split into ``json_mode`` + ``schema_strict``.

    ``schema_strict`` is a measured fact about a provider holding a strict
    schema, so it stays on exactly the models measured live; every real
    ladder and submission-path stage still passes the startup check.
    """

    _MEASURED_STRICT = frozenset(
        {"gemini-3.5-flash-lite", "gemini-3.8-flash", "qwen3.7-max"}
    )

    def test_schema_strict_only_on_measured_models(
        self, registry: ModelRegistryConfig
    ) -> None:
        strict = {
            model_id
            for model_id, model in registry.models.items()
            if Capability.SCHEMA_STRICT in model.capabilities
        }
        assert strict == self._MEASURED_STRICT

    def test_schema_strict_models_also_declare_json_mode(
        self, registry: ModelRegistryConfig
    ) -> None:
        for model_id in self._MEASURED_STRICT:
            assert Capability.JSON_MODE in registry.models[model_id].capabilities

    def test_anthropic_models_have_no_json_mode(
        self, registry: ModelRegistryConfig
    ) -> None:
        claude = [m for m in registry.models.values() if m.provider == "anthropic"]
        assert claude
        assert all(Capability.JSON_MODE not in m.capabilities for m in claude)

    def test_every_real_ladder_and_path_stage_passes_the_startup_check(
        self, registry: ModelRegistryConfig
    ) -> None:
        config_dir = _REGISTRY_PATH.parent
        ladders = load_ladder_config(config_dir)
        validate_ladders_against_registry(ladders, registry)
        validate_path_config(
            load_path_config(config_dir / "submission_paths.yaml"),
            registry,
            ladder_stage_names=ladders.stages.keys(),
            prompt_base_path=config_dir.parent,
        )
