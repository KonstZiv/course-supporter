"""Tests for the KD16 ladder config loader."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from course_supporter.llm.ladder_config import (
    LadderConfig,
    LadderEntry,
    LadderFile,
    StageConfig,
    load_ladder_config,
    validate_ladders_against_registry,
)
from course_supporter.llm.registry import (
    Capability,
    ModelRegistryConfig,
    ProviderConfig,
    ProviderModelConfig,
    load_registry,
)

# ── Helpers ────────────────────────────────────────────────────────


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


_PIPELINE_YAML = """\
stages:
  pipeline_demo:
    prompt_ref: "prompts/example/v1.md"
    ladder:
      - provider: anthropic
        model: claude-sonnet-4-5-20250929
      - provider: gemini
        model: gemini-2.5-flash
"""

_METHODIST_YAML = """\
stages:
  methodist_demo:
    prompt_ref: "prompts/methodist_demo/v1.md"
    ladder:
      - provider: anthropic
        model: claude-sonnet-4-5-20250929
"""

_MENTOR_YAML = """\
stages:
  mentor_demo:
    prompt_ref: "prompts/mentor_demo/v1.md"
    ladder:
      - provider: deepseek
        model: deepseek-v4-flash
"""


def _three_file_directory(tmp_path: Path) -> Path:
    _write(tmp_path / "ladders_pipeline.yaml", _PIPELINE_YAML)
    _write(tmp_path / "ladders_methodist.yaml", _METHODIST_YAML)
    _write(tmp_path / "ladders_mentor.yaml", _MENTOR_YAML)
    return tmp_path


# ── Happy-path loading ─────────────────────────────────────────────


class TestLoadLadderConfig:
    def test_loads_three_files_and_merges(self, tmp_path: Path) -> None:
        config = load_ladder_config(_three_file_directory(tmp_path))

        assert isinstance(config, LadderConfig)
        assert set(config.stages) == {
            "pipeline_demo",
            "methodist_demo",
            "mentor_demo",
        }

    def test_get_stage_returns_validated_config(self, tmp_path: Path) -> None:
        config = load_ladder_config(_three_file_directory(tmp_path))

        stage = config.get_stage("pipeline_demo")
        assert stage.prompt_ref == "prompts/example/v1.md"
        assert len(stage.ladder) == 2
        assert stage.ladder[0] == LadderEntry(
            provider="anthropic", model="claude-sonnet-4-5-20250929"
        )

    def test_get_stage_unknown_raises_keyerror(self, tmp_path: Path) -> None:
        config = load_ladder_config(_three_file_directory(tmp_path))

        with pytest.raises(KeyError, match="Unknown stage"):
            config.get_stage("does_not_exist")

    def test_empty_directory_returns_empty_config(self, tmp_path: Path) -> None:
        config = load_ladder_config(tmp_path)

        assert config.stages == {}
        with pytest.raises(KeyError):
            config.get_stage("anything")

    def test_only_glob_match_is_loaded(self, tmp_path: Path) -> None:
        _write(tmp_path / "ladders_pipeline.yaml", _PIPELINE_YAML)
        _write(tmp_path / "unrelated.yaml", "stages: {ignored: {}}")
        _write(tmp_path / "external_services.yaml", "ignored: true")

        config = load_ladder_config(tmp_path)
        assert set(config.stages) == {"pipeline_demo"}

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_ladder_config(tmp_path / "nope")

    def test_empty_yaml_file_treated_as_empty_stages(self, tmp_path: Path) -> None:
        _write(tmp_path / "ladders_empty.yaml", "")
        _write(tmp_path / "ladders_pipeline.yaml", _PIPELINE_YAML)

        config = load_ladder_config(tmp_path)
        assert set(config.stages) == {"pipeline_demo"}


# ── Cross-file uniqueness ──────────────────────────────────────────


class TestUniqueness:
    def test_duplicate_stage_across_files_raises(self, tmp_path: Path) -> None:
        _write(tmp_path / "ladders_a.yaml", _PIPELINE_YAML)
        # Same stage name in second file with different prompt_ref
        _write(
            tmp_path / "ladders_b.yaml",
            _PIPELINE_YAML.replace("prompts/example/v1.md", "prompts/example/v2.md"),
        )

        with pytest.raises(ValueError) as exc_info:
            load_ladder_config(tmp_path)

        msg = str(exc_info.value)
        assert "pipeline_demo" in msg
        assert "ladders_a.yaml" in msg
        assert "ladders_b.yaml" in msg


# ── Schema strictness (extra="forbid") ─────────────────────────────


class TestExtraForbid:
    def test_typo_in_top_level_key_rejected(self, tmp_path: Path) -> None:
        # "stagez" instead of "stages"
        _write(
            tmp_path / "ladders_typo.yaml",
            "stagez:\n  pipeline_demo:\n    prompt_ref: x\n    ladder:\n"
            "      - provider: a\n        model: b\n",
        )

        with pytest.raises(ValueError, match="Invalid ladder config"):
            load_ladder_config(tmp_path)

    def test_typo_in_stage_field_rejected(self, tmp_path: Path) -> None:
        # "leader" instead of "ladder"
        _write(
            tmp_path / "ladders_typo.yaml",
            "stages:\n  pipeline_demo:\n    prompt_ref: x\n    leader:\n"
            "      - provider: a\n        model: b\n",
        )

        with pytest.raises(ValueError, match="Invalid ladder config"):
            load_ladder_config(tmp_path)

    def test_typo_in_ladder_entry_rejected(self, tmp_path: Path) -> None:
        # "providr" instead of "provider"
        _write(
            tmp_path / "ladders_typo.yaml",
            "stages:\n  pipeline_demo:\n    prompt_ref: x\n    ladder:\n"
            "      - providr: a\n        model: b\n",
        )

        with pytest.raises(ValueError, match="Invalid ladder config"):
            load_ladder_config(tmp_path)


# ── Required-field / min-length validation ─────────────────────────


class TestSchemaValidation:
    def test_empty_ladder_rejected(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "ladders_empty_chain.yaml",
            "stages:\n  pipeline_demo:\n    prompt_ref: x\n    ladder: []\n",
        )

        with pytest.raises(ValueError, match="Invalid ladder config"):
            load_ladder_config(tmp_path)

    def test_missing_prompt_ref_rejected(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "ladders_missing.yaml",
            "stages:\n  pipeline_demo:\n    ladder:\n"
            "      - provider: a\n        model: b\n",
        )

        with pytest.raises(ValueError, match="Invalid ladder config"):
            load_ladder_config(tmp_path)

    def test_malformed_yaml_rejected(self, tmp_path: Path) -> None:
        _write(tmp_path / "ladders_broken.yaml", "stages:\n  : : :\n")

        with pytest.raises(ValueError, match="Failed to parse"):
            load_ladder_config(tmp_path)


# ── Direct model coverage (extra="forbid" at the type level) ───────


class TestModelDirect:
    def test_ladder_entry_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            LadderEntry.model_validate(
                {"provider": "anthropic", "model": "x", "extra": True}
            )

    def test_ladder_entry_defaults_overrides_to_none(self) -> None:
        # Backward compatibility: YAML without overrides parses cleanly
        # and both override fields default to ``None`` so providers fall
        # back to their own class-level defaults.
        entry = LadderEntry.model_validate({"provider": "p", "model": "m"})
        assert entry.reasoning is None
        assert entry.max_output_tokens is None

    def test_ladder_entry_accepts_reasoning_override(self) -> None:
        entry = LadderEntry.model_validate(
            {
                "provider": "dashscope",
                "model": "qwen3-vl-32b-instruct",
                "reasoning": {"exclude": True},
            }
        )
        assert entry.reasoning == {"exclude": True}
        assert entry.max_output_tokens is None

    def test_ladder_entry_accepts_max_output_tokens_override(self) -> None:
        entry = LadderEntry.model_validate(
            {"provider": "gemini", "model": "gemini-2.5-pro", "max_output_tokens": 4096}
        )
        assert entry.max_output_tokens == 4096
        assert entry.reasoning is None

    def test_ladder_entry_accepts_both_overrides_together(self) -> None:
        entry = LadderEntry.model_validate(
            {
                "provider": "dashscope",
                "model": "qwen3-vl-32b-instruct",
                "reasoning": {"exclude": True},
                "max_output_tokens": 2048,
            }
        )
        assert entry.reasoning == {"exclude": True}
        assert entry.max_output_tokens == 2048

    def test_stage_config_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            StageConfig.model_validate(
                {
                    "prompt_ref": "p.md",
                    "ladder": [{"provider": "a", "model": "b"}],
                    "extra": True,
                }
            )

    def test_ladder_file_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            LadderFile.model_validate({"stages": {}, "extra": True})


# ── Real configs smoke test ────────────────────────────────────────


class TestRealConfigs:
    """Verify the checked-in ladders_*.yaml files load cleanly."""

    def test_repo_configs_load(self) -> None:
        # Resolve relative to backend repo root; tests run from there.
        config = load_ladder_config(Path("config"))

        # All checked-in files are picked up; all stage names are unique
        # across them (covers the pipeline pass_2a_mapping stage).
        assert "pass_2a_mapping" in config.stages
        # Methodist stages: methodist_bottomup (Phase 3.2.3a Pass 1) and
        # methodist_topdown (Phase 3.2.3b Pass 2).
        assert "methodist_bottomup" in config.stages
        assert "methodist_topdown" in config.stages

        for stage in config.stages.values():
            assert stage.prompt_ref
            assert stage.ladder  # min_length=1 enforced

    # ── Phase 2.3 sub-area #3 — presentation ladder coverage ──
    # Verifies ``config/ladders_presentation.yaml`` parses cleanly,
    # the two stages register with the expected rungs, and the
    # KD-2.3-S per-rung overrides (reasoning + max_output_tokens)
    # land on the LadderEntry objects in their canonical shape.

    def test_presentation_ladder_loads_via_load_ladder_config(self) -> None:
        config = load_ladder_config(Path("config"))

        assert "presentation_pass_1_vision" in config.stages
        assert "presentation_pass_2a_mapping" in config.stages

    def test_presentation_pass_1_vision_ladder_has_3_rungs(self) -> None:
        config = load_ladder_config(Path("config"))
        stage = config.get_stage("presentation_pass_1_vision")

        assert stage.prompt_ref == "prompts/presentation_pass_1_vision/v1.md"
        rungs = [(e.provider, e.model) for e in stage.ladder]
        assert rungs == [
            ("dashscope", "qwen3.5-flash"),
            ("gemini", "gemini-3-flash-preview"),
            ("gemini", "gemini-2.5-flash"),
        ]

    def test_presentation_pass_2a_mapping_ladder_has_2_rungs(self) -> None:
        config = load_ladder_config(Path("config"))
        stage = config.get_stage("presentation_pass_2a_mapping")

        assert stage.prompt_ref == "prompts/presentation_pass_2a_mapping/v1.md"
        rungs = [(e.provider, e.model) for e in stage.ladder]
        assert rungs == [
            ("mistral", "mistral-large-2512"),
            ("gemini", "gemini-2.5-pro"),
        ]

    def test_pass_1_primary_rung_carries_reasoning_exclude_override(self) -> None:
        # KD-2.3-S end-to-end propagation gate: YAML
        # ``reasoning: {exclude: true}`` lands on
        # ``LadderEntry.reasoning`` as ``{"exclude": True}`` so the
        # DashScope kwarg propagation shipped in sub-area #0
        # (f89ce84) reaches the SDK boundary. For qwen3.5-flash the
        # binding is ACTIVE suppression (thinking-capable model; native
        # ``enable_thinking=False`` translation per TASK-3).
        config = load_ladder_config(Path("config"))
        primary = config.get_stage("presentation_pass_1_vision").ladder[0]

        assert primary.provider == "dashscope"
        assert primary.model == "qwen3.5-flash"
        assert primary.reasoning == {"exclude": True}
        assert primary.max_output_tokens is None

    def test_video_pass_1_primary_rung_carries_suppression_and_pin(self) -> None:
        # Mirror of the presentation rung-1 gate for the video ladder —
        # coverage gap found in the TASK-4 pre-plan (video rung 1 had no
        # named-model assert; a YAML swap broke zero tests). The 8192 pin
        # is the calibrated value (2026-07-29), NOT the model ceiling —
        # see ladders_video.yaml rationale.
        config = load_ladder_config(Path("config"))
        primary = config.get_stage("video_pass_1_vision").ladder[0]

        assert primary.provider == "dashscope"
        assert primary.model == "qwen3.5-flash"
        assert primary.reasoning == {"exclude": True}
        assert primary.max_output_tokens == 8192

    def test_pass_2a_fallback_rung_carries_max_output_tokens_4096_override(
        self,
    ) -> None:
        # KD-2.3-E + KD-2.3-S override gate: Gemini 2.5 Pro's
        # thinking-mode budget needs ``max_output_tokens >= 4096``
        # or the model risks being cut off mid-JSON before emitting
        # the closing brace. Per-rung override threads through
        # ``LadderEntry.max_output_tokens -> LLMRequest.max_tokens``.
        config = load_ladder_config(Path("config"))
        fallback = config.get_stage("presentation_pass_2a_mapping").ladder[1]

        assert fallback.provider == "gemini"
        assert fallback.model == "gemini-2.5-pro"
        assert fallback.max_output_tokens == 4096
        assert fallback.reasoning is None

    def test_presentation_fallback_rungs_have_no_overrides(self) -> None:
        # Negative gate: fallback rungs without explicit overrides
        # default both fields to ``None`` so providers fall back to
        # their class-level defaults (e.g. ``DashScopeProvider.
        # default_max_output_tokens``). Catches accidental override
        # leakage from one rung into another in YAML edits.
        config = load_ladder_config(Path("config"))

        pass_1_fallbacks = config.get_stage("presentation_pass_1_vision").ladder[1:]
        for rung in pass_1_fallbacks:
            assert rung.reasoning is None
            assert rung.max_output_tokens is None

        pass_2a_primary = config.get_stage(
            "presentation_pass_2a_mapping",
        ).ladder[0]
        assert pass_2a_primary.reasoning is None
        assert pass_2a_primary.max_output_tokens is None

    def test_presentation_prompt_refs_point_to_existing_files(self) -> None:
        # Cross-sub-area regression guard: sub-area #2 sealed both
        # prompt files; sub-area #3 references them by relative
        # path. A typo or rename in either layer surfaces as a
        # FileNotFoundError here instead of at production
        # ``StageRouter.execute_for_stage`` time.
        config = load_ladder_config(Path("config"))
        for stage_name in (
            "presentation_pass_1_vision",
            "presentation_pass_2a_mapping",
        ):
            stage = config.get_stage(stage_name)
            assert Path(stage.prompt_ref).is_file(), (
                f"prompt_ref for {stage_name!r} not found: {stage.prompt_ref}"
            )


# ── TASK-2.4.23: validate_ladders_against_registry ─────────────────


def _two_model_registry() -> ModelRegistryConfig:
    """Registry with one vision + one text-only model — fixture for tests."""
    return ModelRegistryConfig(
        providers={
            "anthropic": ProviderConfig(
                type="llm",
                models=[
                    ProviderModelConfig(
                        id="vl-model",
                        capabilities=[
                            Capability.VISION,
                            Capability.STRUCTURED_OUTPUT,
                        ],
                    ),
                    ProviderModelConfig(
                        id="text-model",
                        capabilities=[Capability.STRUCTURED_OUTPUT],
                    ),
                ],
            )
        },
        actions={},
    )


def _stage(
    *,
    requires: list[Capability] | None = None,
    models: tuple[str, ...] = ("vl-model",),
) -> StageConfig:
    return StageConfig(
        prompt_ref="prompts/x/v1.md",
        requires=list(requires) if requires is not None else [],
        ladder=[LadderEntry(provider="anthropic", model=m) for m in models],
    )


class TestStageConfigRequiresField:
    """StageConfig.requires accepts list[Capability] and defaults to []."""

    def test_default_is_empty_list(self) -> None:
        stage = StageConfig(
            prompt_ref="prompts/x.md",
            ladder=[LadderEntry(provider="anthropic", model="m")],
        )
        assert stage.requires == []

    def test_explicit_capability_list_round_trips(self) -> None:
        stage = StageConfig(
            prompt_ref="prompts/x.md",
            requires=[Capability.VISION],
            ladder=[LadderEntry(provider="anthropic", model="m")],
        )
        assert stage.requires == [Capability.VISION]

    def test_yaml_string_capability_coerces(self) -> None:
        """YAML emits ``requires: [vision]`` as strings — Pydantic coerces."""
        stage = StageConfig.model_validate(
            {
                "prompt_ref": "prompts/x.md",
                "requires": ["vision", "structured_output"],
                "ladder": [{"provider": "anthropic", "model": "m"}],
            }
        )
        assert stage.requires == [Capability.VISION, Capability.STRUCTURED_OUTPUT]


class TestValidateLaddersAgainstRegistry:
    """validate_ladders_against_registry — K + Q-axis1 fail-fast."""

    def test_happy_path_no_errors(self) -> None:
        cfg = LadderConfig(stages={"demo": _stage(models=("vl-model",))})
        validate_ladders_against_registry(cfg, _two_model_registry())
        # Returns None on success; absence of exception is the contract.

    def test_unknown_model_raises_with_stage_and_rung(self) -> None:
        cfg = LadderConfig(
            stages={"demo": _stage(models=("typo-model",))},
        )
        with pytest.raises(ValueError) as exc_info:
            validate_ladders_against_registry(cfg, _two_model_registry())
        msg = str(exc_info.value)
        assert "Ladder validation failed" in msg
        assert "Stage 'demo' rung 0" in msg
        assert "typo-model" in msg

    def test_capability_mismatch_raises(self) -> None:
        cfg = LadderConfig(
            stages={
                "vision_stage": _stage(
                    requires=[Capability.VISION], models=("text-model",)
                )
            },
        )
        with pytest.raises(ValueError) as exc_info:
            validate_ladders_against_registry(cfg, _two_model_registry())
        msg = str(exc_info.value)
        assert "Stage 'vision_stage' rung 0" in msg
        assert "text-model" in msg
        assert "vision" in msg
        assert "lacks required capabilities" in msg

    def test_capability_satisfied_by_vision_model(self) -> None:
        cfg = LadderConfig(
            stages={
                "vision_stage": _stage(
                    requires=[Capability.VISION], models=("vl-model",)
                )
            },
        )
        validate_ladders_against_registry(cfg, _two_model_registry())

    def test_empty_requires_means_no_capability_check(self) -> None:
        """Default empty requires admits any registered rung."""
        cfg = LadderConfig(
            stages={
                "free_stage": _stage(requires=[], models=("text-model", "vl-model"))
            },
        )
        validate_ladders_against_registry(cfg, _two_model_registry())

    def test_multiple_errors_aggregated(self) -> None:
        """A multi-typo / multi-mismatch config surfaces all problems at once."""
        cfg = LadderConfig(
            stages={
                "a": _stage(models=("typo-1",)),
                "b": _stage(
                    requires=[Capability.VISION], models=("text-model", "typo-2")
                ),
            },
        )
        with pytest.raises(ValueError) as exc_info:
            validate_ladders_against_registry(cfg, _two_model_registry())
        msg = str(exc_info.value)
        # All three errors present in one raise.
        assert msg.count("Stage 'a' rung 0") == 1
        assert "typo-1" in msg
        assert "Stage 'b' rung 0" in msg
        assert "text-model" in msg
        assert "Stage 'b' rung 1" in msg
        assert "typo-2" in msg

    def test_rung_index_reflects_position(self) -> None:
        cfg = LadderConfig(
            stages={"demo": _stage(models=("vl-model", "vl-model", "typo-model"))},
        )
        with pytest.raises(ValueError) as exc_info:
            validate_ladders_against_registry(cfg, _two_model_registry())
        assert "Stage 'demo' rung 2" in str(exc_info.value)


class TestValidateReasoningInvariant:
    """P6 — the fourth invariant: reasoning-form translatability per provider.

    ``model="vl-model"`` is registered in ``_two_model_registry`` so the
    membership / capability checks pass and the reasoning check is isolated.
    The check resolves ``entry.provider`` against ``PROVIDER_REGISTRY`` (not the
    model registry), so the connector's capability is what decides.
    """

    @staticmethod
    def _stage_with_reasoning(provider: str, form: dict[str, object]) -> StageConfig:
        return StageConfig(
            prompt_ref="prompts/x/v1.md",
            ladder=[LadderEntry(provider=provider, model="vl-model", reasoning=form)],
        )

    def test_dashscope_covered_form_passes(self) -> None:
        cfg = LadderConfig(
            stages={"s": self._stage_with_reasoning("dashscope", {"exclude": True})}
        )
        validate_ladders_against_registry(cfg, _two_model_registry())

    def test_dashscope_uncovered_form_raises_naming_stage(self) -> None:
        cfg = LadderConfig(
            stages={"s": self._stage_with_reasoning("dashscope", {"exclude": False})}
        )
        with pytest.raises(ValueError) as exc_info:
            validate_ladders_against_registry(cfg, _two_model_registry())
        msg = str(exc_info.value)
        assert "Stage 's' rung 0" in msg
        assert "dashscope" in msg
        assert "cannot translate" in msg
        assert "{'exclude': False}" in msg

    def test_non_dashscope_provider_with_any_binding_raises(self) -> None:
        cfg = LadderConfig(
            stages={"s": self._stage_with_reasoning("gemini", {"exclude": True})}
        )
        with pytest.raises(ValueError) as exc_info:
            validate_ladders_against_registry(cfg, _two_model_registry())
        msg = str(exc_info.value)
        assert "Stage 's' rung 0" in msg
        assert "gemini" in msg
        assert "cannot translate" in msg

    def test_unknown_provider_with_binding_raises(self) -> None:
        cfg = LadderConfig(
            stages={"s": self._stage_with_reasoning("nonesuch", {"exclude": True})}
        )
        with pytest.raises(ValueError) as exc_info:
            validate_ladders_against_registry(cfg, _two_model_registry())
        assert "cannot translate" in str(exc_info.value)

    def test_reasoning_and_model_faults_both_aggregated(self) -> None:
        # The reasoning check runs independently of membership, so a rung with
        # BOTH an unknown model and an uncovered form surfaces both errors.
        cfg = LadderConfig(
            stages={
                "s": StageConfig(
                    prompt_ref="prompts/x/v1.md",
                    ladder=[
                        LadderEntry(
                            provider="dashscope",
                            model="typo-model",
                            reasoning={"exclude": False},
                        )
                    ],
                )
            }
        )
        with pytest.raises(ValueError) as exc_info:
            validate_ladders_against_registry(cfg, _two_model_registry())
        msg = str(exc_info.value)
        assert "cannot translate" in msg
        assert "unknown model" in msg


class TestProductionLaddersValidate:
    """Live config vs live registry passes day-1 (TASK-2.4.23 acceptance #3)."""

    def test_real_config_passes_validation(self) -> None:
        config = load_ladder_config(Path("config"))
        registry = load_registry(Path("config/external_services.yaml"))
        # No exception = invariant holds for every annotated stage.
        validate_ladders_against_registry(config, registry)

    def test_annotated_stages_carry_expected_requires(self) -> None:
        config = load_ladder_config(Path("config"))
        # Spot-check the per-stage requires map from the TASK-23 spec.
        assert config.get_stage("video_pass_1_vision").requires == [Capability.VISION]
        assert config.get_stage("presentation_pass_1_vision").requires == [
            Capability.VISION
        ]
        assert config.get_stage("safety_check").requires == [
            Capability.STRUCTURED_OUTPUT
        ]
        # Pass 2c stages stay unconstrained.
        assert config.get_stage("video_pass_2c_denoise").requires == []
        assert config.get_stage("audio_pass_2c_denoise").requires == []


# ── Phase 3.2.3-pre: input_budget_ratio field + config-time validator ──


def _registry_with_max_context(
    *, vl_max_context: int | None = 1_000_000, text_max_context: int | None = 32_000
) -> ModelRegistryConfig:
    """Registry where models carry ``max_context`` (Phase 3.2.3-pre coverage).

    ``_two_model_registry`` above leaves ``max_context=None`` everywhere
    (capabilities-focused). The input-budget validator (and runtime
    skip block in StageRouter) need real values; this helper exposes
    a knob per model so a test can omit ``max_context`` selectively.
    """
    return ModelRegistryConfig(
        providers={
            "anthropic": ProviderConfig(
                type="llm",
                models=[
                    ProviderModelConfig(
                        id="vl-model",
                        capabilities=[
                            Capability.VISION,
                            Capability.STRUCTURED_OUTPUT,
                        ],
                        max_context=vl_max_context,
                    ),
                    ProviderModelConfig(
                        id="text-model",
                        capabilities=[Capability.STRUCTURED_OUTPUT],
                        max_context=text_max_context,
                    ),
                ],
            )
        },
        actions={},
    )


class TestStageConfigInputBudgetRatioField:
    """``input_budget_ratio`` field on :class:`StageConfig` — shape + bounds."""

    def test_default_is_none(self) -> None:
        stage = StageConfig(
            prompt_ref="prompts/x.md",
            ladder=[LadderEntry(provider="anthropic", model="m")],
        )
        assert stage.input_budget_ratio is None

    def test_accepts_valid_ratio(self) -> None:
        stage = StageConfig(
            prompt_ref="prompts/x.md",
            ladder=[LadderEntry(provider="anthropic", model="m")],
            input_budget_ratio=0.5,
        )
        assert stage.input_budget_ratio == 0.5

    def test_upper_bound_inclusive(self) -> None:
        # 1.0 is allowed (whole context window usable, no reserve).
        stage = StageConfig(
            prompt_ref="prompts/x.md",
            ladder=[LadderEntry(provider="anthropic", model="m")],
            input_budget_ratio=1.0,
        )
        assert stage.input_budget_ratio == 1.0

    def test_rejects_zero(self) -> None:
        # 0.0 is meaningless (would skip every rung) — must be > 0.
        with pytest.raises(ValueError):
            StageConfig(
                prompt_ref="prompts/x.md",
                ladder=[LadderEntry(provider="anthropic", model="m")],
                input_budget_ratio=0.0,
            )

    def test_rejects_above_one(self) -> None:
        with pytest.raises(ValueError):
            StageConfig(
                prompt_ref="prompts/x.md",
                ladder=[LadderEntry(provider="anthropic", model="m")],
                input_budget_ratio=1.5,
            )

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            StageConfig(
                prompt_ref="prompts/x.md",
                ladder=[LadderEntry(provider="anthropic", model="m")],
                input_budget_ratio=-0.1,
            )


class TestInputBudgetRatioConfigTimeValidation:
    """``validate_ladders_against_registry`` enforces max_context presence
    when a stage declares ``input_budget_ratio``.
    """

    def test_happy_path_all_rungs_have_max_context(self) -> None:
        cfg = LadderConfig(
            stages={
                "demo": StageConfig(
                    prompt_ref="prompts/x.md",
                    ladder=[LadderEntry(provider="anthropic", model="vl-model")],
                    input_budget_ratio=0.5,
                )
            }
        )
        validate_ladders_against_registry(cfg, _registry_with_max_context())

    def test_rung_without_max_context_raises(self) -> None:
        # vl-model has max_context=None in registry → invariant violated.
        registry = _registry_with_max_context(vl_max_context=None)
        cfg = LadderConfig(
            stages={
                "demo": StageConfig(
                    prompt_ref="prompts/x.md",
                    ladder=[LadderEntry(provider="anthropic", model="vl-model")],
                    input_budget_ratio=0.5,
                )
            }
        )
        with pytest.raises(ValueError) as exc_info:
            validate_ladders_against_registry(cfg, registry)
        msg = str(exc_info.value)
        assert "Stage 'demo' rung 0" in msg
        assert "vl-model" in msg
        assert "no max_context" in msg
        assert "input_budget_ratio=0.5" in msg

    def test_no_ratio_means_no_max_context_check(self) -> None:
        # Without input_budget_ratio, missing max_context is fine — this is the
        # byte-identical-pre-3.2.3-pre regression pin.
        registry = _registry_with_max_context(
            vl_max_context=None, text_max_context=None
        )
        cfg = LadderConfig(
            stages={
                "demo": StageConfig(
                    prompt_ref="prompts/x.md",
                    ladder=[LadderEntry(provider="anthropic", model="vl-model")],
                )
            }
        )
        validate_ladders_against_registry(cfg, registry)

    def test_per_rung_max_context_aggregates_errors(self) -> None:
        # ratio set + multi-rung ladder where ONLY some rungs miss max_context.
        registry = _registry_with_max_context(
            vl_max_context=1_000_000, text_max_context=None
        )
        cfg = LadderConfig(
            stages={
                "demo": StageConfig(
                    prompt_ref="prompts/x.md",
                    ladder=[
                        LadderEntry(provider="anthropic", model="vl-model"),
                        LadderEntry(provider="anthropic", model="text-model"),
                    ],
                    input_budget_ratio=0.5,
                )
            }
        )
        with pytest.raises(ValueError) as exc_info:
            validate_ladders_against_registry(cfg, registry)
        msg = str(exc_info.value)
        # Only rung 1 (text-model) is reported — vl-model has max_context.
        assert "rung 1" in msg
        assert "text-model" in msg
        assert (
            "rung 0" not in msg
            or "vl-model" not in msg.split("rung 0")[1].split("rung 1")[0]
        )


# ── TASK-4 ladder model lock ───────────────────────────────────────
# Target state of EVERY rung of EVERY stage across ALL config/ladders_*.yaml
# files, keyed by stage name, ordered as on disk. Kept next to its test on
# purpose: the append-only locality makes the lock trivial to extend.

LADDER_MODEL_TABLE: dict[str, list[tuple[str, str]]] = {
    # ladders_audio.yaml
    "audio_pass_2a_mapping": [
        ("gemini", "gemini-3.1-flash-lite"),
        ("gemini", "gemini-2.5-flash"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
    "audio_pass_2c_denoise": [
        ("gemini", "gemini-3.1-flash-lite"),
        ("gemini", "gemini-2.5-flash"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
    # ladders_code.yaml
    "code_skeleton_extraction": [
        ("gemini", "gemini-3.1-flash-lite"),
        ("deepseek", "deepseek-v4-flash"),
    ],
    "code_segment_description": [
        ("gemini", "gemini-3.1-flash-lite"),
        ("deepseek", "deepseek-v4-flash"),
    ],
    "code_summary": [
        ("deepseek_thinking", "deepseek-v4-pro"),
        ("dashscope", "qwen3.7-max"),
        ("gemini", "gemini-2.5-flash"),
    ],
    # ladders_mentor.yaml
    "safety_check": [
        ("mistral", "mistral-small-latest"),
        ("deepseek", "deepseek-v4-flash"),
        ("gemini", "gemini-2.5-flash"),
    ],
    "safety_check_authored": [
        ("mistral", "mistral-small-latest"),
        ("deepseek", "deepseek-v4-flash"),
        ("gemini", "gemini-2.5-flash"),
    ],
    "sanity_check": [
        ("mistral", "mistral-small-latest"),
        ("deepseek", "deepseek-v4-flash"),
        ("gemini", "gemini-2.5-flash"),
    ],
    "criteria_decomposition": [
        ("deepseek_thinking", "deepseek-v4-pro"),
        ("dashscope", "qwen3.7-max"),
        ("deepseek", "deepseek-v4-flash"),
    ],
    "mentor_layered_evaluation_node_course": [
        ("deepseek_thinking", "deepseek-v4-pro"),
        ("dashscope", "qwen3.7-max"),
        ("deepseek", "deepseek-v4-flash"),
    ],
    "mentor_layered_evaluation_industry": [
        ("deepseek_thinking", "deepseek-v4-pro"),
        ("dashscope", "qwen3.7-max"),
        ("deepseek", "deepseek-v4-flash"),
    ],
    "mentor_denoising": [
        ("dashscope", "qwen3.7-max"),
        ("gemini", "gemini-2.5-pro"),
        ("deepseek", "deepseek-v4-flash"),
    ],
    "mentor_synthesis": [
        ("dashscope", "qwen3.7-max"),
        ("gemini", "gemini-2.5-pro"),
        ("deepseek", "deepseek-v4-flash"),
    ],
    # ladders_methodist.yaml
    "methodist_bottomup": [
        ("deepseek_thinking", "deepseek-v4-pro"),
        ("dashscope", "qwen3.7-max"),
    ],
    "methodist_topdown": [
        ("deepseek_thinking", "deepseek-v4-pro"),
        ("dashscope", "qwen3.7-max"),
        ("deepseek", "deepseek-v4-flash"),
    ],
    # ladders_pipeline.yaml
    "pass_2a_mapping": [
        ("deepseek", "deepseek-v4-flash"),
        ("gemini", "gemini-2.5-flash"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
    # ladders_presentation.yaml
    "presentation_pass_1_vision": [
        ("dashscope", "qwen3.5-flash"),
        ("gemini", "gemini-3-flash-preview"),
        ("gemini", "gemini-2.5-flash"),
    ],
    "presentation_pass_2a_mapping": [
        ("mistral", "mistral-large-2512"),
        ("gemini", "gemini-2.5-pro"),
    ],
    # ladders_video.yaml
    "video_pass_1_vision": [
        ("dashscope", "qwen3.5-flash"),
        ("gemini", "gemini-3-flash-preview"),
        ("gemini", "gemini-2.5-flash"),
    ],
    "video_pass_2a_mapping": [
        ("deepseek_thinking", "deepseek-v4-pro"),
        ("dashscope", "qwen3.7-max"),
        ("gemini", "gemini-2.5-pro"),
    ],
    "video_pass_2c_denoise": [
        ("gemini", "gemini-3.1-flash-lite"),
        ("gemini", "gemini-2.5-flash"),
        ("anthropic", "claude-haiku-4-5-20251001"),
    ],
}


class TestLadderModelTable:
    """TASK-4 bidirectional (provider, model) lock over ALL ladder files.

    Purpose: no model swap, rung reorder, rung add/remove or stage
    add/remove can land without a conscious edit of this table — the red
    test IS the feature (pre-plan finding: the video rung-1 model could
    be swapped without breaking a single test). Overrides (reasoning,
    max_output_tokens) are deliberately NOT locked here; they live in the
    targeted override tests above, so number tuning does not devalue this
    lock.
    """

    def test_stage_universe_matches_table(self) -> None:
        config = load_ladder_config(Path("config"))
        assert set(config.stages) == set(LADDER_MODEL_TABLE)

    @pytest.mark.parametrize("stage_name", sorted(LADDER_MODEL_TABLE))
    def test_stage_rungs_match_table(self, stage_name: str) -> None:
        config = load_ladder_config(Path("config"))
        rungs = [
            (entry.provider, entry.model)
            for entry in config.get_stage(stage_name).ladder
        ]
        assert rungs == LADDER_MODEL_TABLE[stage_name]
