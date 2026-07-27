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

from course_supporter.llm.registry import ModelRegistryConfig, load_registry

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
