"""Regression guards for ``config/external_services.yaml``.

The legacy LLM action chains (course_structuring, methodist,
text_processing, …) and their Sonnet-rescue regression guards were removed
with the DD-20-A dismantle; the live LLM routing surface is StageRouter over
``config/ladders_*.yaml``. What remains in this registry is the model catalog
plus the STT ``transcribe`` action consumed by STTRouter — pinned here so a
bad edit is caught at load time.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

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
    ModelConfig,
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


# ── Lock: a called model's price fields are filled or marked (BIL-PRICES) ──
#
# An empty price field of a model a ladder, a submission path or the
# ``transcribe`` chain actually calls is allowed only when it is listed below
# with its reason: either the provider has no such rule for the model (not
# applicable), or the official page did not state the number at the last
# revision (not established, 2026-10-30). A mark on a field that is filled, or
# on a model nothing calls, fails too, so the lists cannot outlive the gap.

_CONFIG_DIR = _REGISTRY_PATH.parent

# Read from the raw YAML, so a ladder file no loader knows yet is covered too.
_ROUTING_FILES = sorted(
    [*_CONFIG_DIR.glob("ladders_*.yaml"), _CONFIG_DIR / "submission_paths.yaml"]
)

_TOKEN_PRICE_FIELDS = (
    "cost_per_1k_in",
    "cost_per_1k_out",
    "cost_per_1k_in_cached",
    "price_effective_from",
    "price_holds_to_input_tokens",
)
_MINUTE_PRICE_FIELDS = (
    "cost_per_minute",
    "billing_granularity_sec",
    "price_effective_from",
)

_NO_TIERS_DEEPSEEK = (
    "no prompt-size tiers on api-docs.deepseek.com/quick_start/pricing/"
)
_NO_TIERS_GEMINI = (
    "one price for every prompt size on ai.google.dev/gemini-api/docs/pricing"
)
_NO_TIERS_MISTRAL = "one price for every prompt size on the docs.mistral.ai model card"

_NOT_APPLICABLE: dict[tuple[str, str], str] = {
    ("deepseek-flash", "price_holds_to_input_tokens"): _NO_TIERS_DEEPSEEK,
    ("deepseek-v4-pro", "price_holds_to_input_tokens"): _NO_TIERS_DEEPSEEK,
    ("gemini-3-flash-preview", "price_holds_to_input_tokens"): _NO_TIERS_GEMINI,
    ("gemini-3.1-flash-lite", "price_holds_to_input_tokens"): _NO_TIERS_GEMINI,
    ("gemini-3.5-flash-lite", "price_holds_to_input_tokens"): _NO_TIERS_GEMINI,
    ("gemini-3.8-flash", "price_holds_to_input_tokens"): _NO_TIERS_GEMINI,
    ("mistral-large-2512", "price_holds_to_input_tokens"): _NO_TIERS_MISTRAL,
    ("mistral-small-latest", "price_holds_to_input_tokens"): _NO_TIERS_MISTRAL,
    ("qwen3.5-flash", "cost_per_1k_in_cached"): (
        "Frankfurt / Global scope has the explicit cache only for this model "
        "(model-studio/context-cache); our connector never creates one"
    ),
    ("gpt-4o-mini-transcribe", "billing_granularity_sec"): (
        "billed by audio tokens; the pricing page calls $0.003/minute an "
        "'Estimated cost', so there is no rounding step in seconds"
    ),
}

_NO_DATE = "the official page gives no date the price is in force from"

_NOT_ESTABLISHED: dict[tuple[str, str], str] = {
    ("gemini-3-flash-preview", "price_effective_from"): _NO_DATE,
    ("gemini-3.1-flash-lite", "price_effective_from"): _NO_DATE,
    ("gemini-3.5-flash-lite", "price_effective_from"): _NO_DATE,
    ("mistral-large-2512", "price_effective_from"): _NO_DATE,
    ("mistral-small-latest", "price_effective_from"): _NO_DATE,
    ("qwen3.5-flash", "price_effective_from"): _NO_DATE,
    ("qwen3.7-max", "price_effective_from"): _NO_DATE,
    ("gpt-4o-mini-transcribe", "price_effective_from"): _NO_DATE,
    ("nova-3", "price_effective_from"): _NO_DATE,
    ("scribe_v1", "price_effective_from"): _NO_DATE,
    ("mistral-large-2512", "cost_per_1k_in_cached"): (
        "mistral.ai/pricing only says cached input tokens 'reduce input cost by "
        "up to 90%'; no per-model price"
    ),
    ("mistral-small-latest", "cost_per_1k_in_cached"): (
        "mistral.ai/pricing only says cached input tokens 'reduce input cost by "
        "up to 90%'; no per-model price"
    ),
    ("scribe_v1", "billing_granularity_sec"): (
        "elevenlabs.io/pricing/api: 'Speech to Text is billed per audio minute' "
        "names the unit, not a rounding step"
    ),
}


def _rung_models(node: Any) -> Iterator[str]:
    """The model of every mapping in a YAML tree that names a provider and a model."""
    if isinstance(node, dict):
        if "provider" in node and "model" in node:
            yield node["model"]
        for value in node.values():
            yield from _rung_models(value)
    elif isinstance(node, list):
        for item in node:
            yield from _rung_models(item)


def _called_models(registry: ModelRegistryConfig) -> set[str]:
    called = {
        model
        for path in _ROUTING_FILES
        for model in _rung_models(yaml.safe_load(path.read_text(encoding="utf-8")))
    }
    for chain in registry.actions["transcribe"].chain.values():
        called.update(chain)
    return called


def _price_field(model: ModelConfig, name: str) -> object:
    return {
        "cost_per_1k_in": model.prices.in_per_1k,
        "cost_per_1k_out": model.prices.out_per_1k,
        "cost_per_1k_in_cached": model.prices.in_cached_per_1k,
        "cost_per_minute": model.prices.per_minute,
        "price_effective_from": model.price_effective_from,
        "price_holds_to_input_tokens": model.price_holds_to_input_tokens,
        "billing_granularity_sec": model.billing_granularity_sec,
    }[name]


def _empty_price_fields(registry: ModelRegistryConfig) -> set[tuple[str, str]]:
    empty = set()
    for model_id in _called_models(registry):
        model = registry.models[model_id]
        by_minute = model.unit_type == "minutes"
        fields = _MINUTE_PRICE_FIELDS if by_minute else _TOKEN_PRICE_FIELDS
        empty.update(
            (model_id, name) for name in fields if _price_field(model, name) is None
        )
    return empty


class TestCalledModelPricesAreFilledOrMarked:
    """BIL-PRICES lock on the price fields of the models we actually call."""

    def test_called_models_are_found_in_the_registry(
        self, registry: ModelRegistryConfig
    ) -> None:
        called = _called_models(registry)
        # Guards the discovery: an empty set would pass the lock vacuously.
        assert len(_ROUTING_FILES) >= 9
        assert {"deepseek-flash", "scribe_v1", "nova-3"} <= called
        assert called <= set(registry.models)

    def test_every_empty_price_field_is_marked(
        self, registry: ModelRegistryConfig
    ) -> None:
        marked = _NOT_APPLICABLE.keys() | _NOT_ESTABLISHED.keys()
        assert sorted(_empty_price_fields(registry) - marked) == []

    def test_every_mark_names_an_empty_field_of_a_called_model(
        self, registry: ModelRegistryConfig
    ) -> None:
        marked = _NOT_APPLICABLE.keys() | _NOT_ESTABLISHED.keys()
        assert sorted(marked - _empty_price_fields(registry)) == []

    def test_a_field_is_marked_once_and_with_a_reason(self) -> None:
        assert _NOT_APPLICABLE.keys() & _NOT_ESTABLISHED.keys() == set()
        reasons = [*_NOT_APPLICABLE.values(), *_NOT_ESTABLISHED.values()]
        assert all(reason.strip() for reason in reasons)
