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
)

# ── Helpers ────────────────────────────────────────────────────────


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


_PIPELINE_YAML = """\
stages:
  example_stage:
    prompt_ref: "prompts/example/v1.md"
    ladder:
      - provider: anthropic
        model: claude-sonnet-4-20250514
      - provider: gemini
        model: gemini-2.5-flash
"""

_METHODIST_YAML = """\
stages:
  methodist_demo:
    prompt_ref: "prompts/methodist_demo/v1.md"
    ladder:
      - provider: anthropic
        model: claude-sonnet-4-20250514
"""

_MENTOR_YAML = """\
stages:
  mentor_demo:
    prompt_ref: "prompts/mentor_demo/v1.md"
    ladder:
      - provider: deepseek
        model: deepseek-chat
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
            "example_stage",
            "methodist_demo",
            "mentor_demo",
        }

    def test_get_stage_returns_validated_config(self, tmp_path: Path) -> None:
        config = load_ladder_config(_three_file_directory(tmp_path))

        stage = config.get_stage("example_stage")
        assert stage.prompt_ref == "prompts/example/v1.md"
        assert len(stage.ladder) == 2
        assert stage.ladder[0] == LadderEntry(
            provider="anthropic", model="claude-sonnet-4-20250514"
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
        assert set(config.stages) == {"example_stage"}

    def test_missing_directory_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_ladder_config(tmp_path / "nope")

    def test_empty_yaml_file_treated_as_empty_stages(self, tmp_path: Path) -> None:
        _write(tmp_path / "ladders_empty.yaml", "")
        _write(tmp_path / "ladders_pipeline.yaml", _PIPELINE_YAML)

        config = load_ladder_config(tmp_path)
        assert set(config.stages) == {"example_stage"}


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
        assert "example_stage" in msg
        assert "ladders_a.yaml" in msg
        assert "ladders_b.yaml" in msg


# ── Schema strictness (extra="forbid") ─────────────────────────────


class TestExtraForbid:
    def test_typo_in_top_level_key_rejected(self, tmp_path: Path) -> None:
        # "stagez" instead of "stages"
        _write(
            tmp_path / "ladders_typo.yaml",
            "stagez:\n  example_stage:\n    prompt_ref: x\n    ladder:\n"
            "      - provider: a\n        model: b\n",
        )

        with pytest.raises(ValueError, match="Invalid ladder config"):
            load_ladder_config(tmp_path)

    def test_typo_in_stage_field_rejected(self, tmp_path: Path) -> None:
        # "leader" instead of "ladder"
        _write(
            tmp_path / "ladders_typo.yaml",
            "stages:\n  example_stage:\n    prompt_ref: x\n    leader:\n"
            "      - provider: a\n        model: b\n",
        )

        with pytest.raises(ValueError, match="Invalid ladder config"):
            load_ladder_config(tmp_path)

    def test_typo_in_ladder_entry_rejected(self, tmp_path: Path) -> None:
        # "providr" instead of "provider"
        _write(
            tmp_path / "ladders_typo.yaml",
            "stages:\n  example_stage:\n    prompt_ref: x\n    ladder:\n"
            "      - providr: a\n        model: b\n",
        )

        with pytest.raises(ValueError, match="Invalid ladder config"):
            load_ladder_config(tmp_path)


# ── Required-field / min-length validation ─────────────────────────


class TestSchemaValidation:
    def test_empty_ladder_rejected(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "ladders_empty_chain.yaml",
            "stages:\n  example_stage:\n    prompt_ref: x\n    ladder: []\n",
        )

        with pytest.raises(ValueError, match="Invalid ladder config"):
            load_ladder_config(tmp_path)

    def test_missing_prompt_ref_rejected(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "ladders_missing.yaml",
            "stages:\n  example_stage:\n    ladder:\n"
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

        # All three checked-in files are picked up; all stage names
        # are unique across them (covers the canonical "example_stage"
        # asked for by the acceptance criteria).
        assert "example_stage" in config.stages
        # Other example stages from the methodist/mentor files load too.
        assert "methodist_example" in config.stages
        assert "mentor_example" in config.stages

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
            ("dashscope", "qwen3-vl-32b-instruct"),
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
        # (f89ce84) reaches the SDK boundary. Defensive guard for
        # Qwen3-VL non-reasoning by construction.
        config = load_ladder_config(Path("config"))
        primary = config.get_stage("presentation_pass_1_vision").ladder[0]

        assert primary.provider == "dashscope"
        assert primary.model == "qwen3-vl-32b-instruct"
        assert primary.reasoning == {"exclude": True}
        assert primary.max_output_tokens is None

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
