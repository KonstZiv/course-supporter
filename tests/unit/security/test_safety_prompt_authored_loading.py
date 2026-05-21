"""Tests for the authored-context Stage 2 prompt + ladder (Phase 2.3 hotfix).

Mirrors ``test_safety_prompt_loading.py`` for the new
``prompts/safety_check_authored/v1.md``, locking the four shared
system-prompt elements (anti-jailbreak preamble, strict JSON output,
22-language coverage, calibration examples) PLUS the authored-specific
trust contract: advertising / branding / external links are NOT
violations, ``off_topic`` is high-bar, and the prompt does NOT frame
content as a "homework submission". These authored locks prevent a
future edit from silently re-introducing the homework-first framing
that caused the Phase 2.3 false-positive rejections.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import UndefinedError

from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.prompt_loader_md import load_prompt

_PROMPT_REF = "prompts/safety_check_authored/v1.md"
_CONFIG_DIR = Path("config")

# Same 22 target-audience languages as the homework prompt (KD14).
_TARGET_LANGUAGES: tuple[str, ...] = (
    "English",
    "Ukrainian",
    "Russian",
    "Moldovan",
    "Romanian",
    "Serbian",
    "Montenegrin",
    "Croatian",
    "Bosnian",
    "German",
    "Spanish",
    "French",
    "Czech",
    "Slovak",
    "Slovenian",
    "Italian",
    "Bulgarian",
    "Turkish",
    "Polish",
    "Georgian",
    "Armenian",
    "Hungarian",
)


# ── Prompt structural load ─────────────────────────────────────────


class TestPromptLoad:
    def test_load_returns_system_and_user(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        assert sp.user is not None

    def test_no_assistant_section(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.assistant is None


# ── 22-language coverage ───────────────────────────────────────────


class TestPromptCovers22Languages:
    @pytest.mark.parametrize("language", _TARGET_LANGUAGES)
    def test_each_target_language_named(self, language: str) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        assert language in sp.system, (
            f"target audience language {language!r} missing from authored "
            "prompt; do NOT trim the language list without vision-side approval"
        )

    def test_count_is_exactly_22(self) -> None:
        assert len(_TARGET_LANGUAGES) == 22


# ── JSON output strictness ─────────────────────────────────────────


class TestPromptJSONStrictness:
    def test_demands_json_object(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        assert "json" in body
        assert "only" in body

    def test_forbids_markdown_fence(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        assert (
            "no markdown" in body
            or "no code fence" in body
            or ("no" in body and "fence" in body)
        )

    def test_forbids_preamble(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        assert "no preamble" in body or "no trailing" in body

    def test_lists_violation_enum_values(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        # The four ViolationCategory values ties the prompt to the schema.
        for value in (
            "prompt_injection",
            "off_topic",
            "policy_violation",
            "suspicious_behavior",
        ):
            assert value in sp.system


# ── Anti-jailbreak preamble ────────────────────────────────────────


class TestPromptAntiJailbreak:
    def test_treats_content_as_data(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        assert "as data" in body
        assert "not" in body and "instruction" in body

    def test_explicit_never_follow(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        assert "never" in body
        # Authored framing wraps content as "material", not "submission".
        assert "material" in body

    def test_xml_delimiters_for_material(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.user is not None
        assert "<material>" in sp.user
        assert "</material>" in sp.user


# ── Calibration examples ───────────────────────────────────────────


class TestPromptCalibrationExamples:
    def test_includes_safe_and_unsafe_examples(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        assert "is_safe=true" in body
        assert "is_safe=false" in body

    def test_three_tier_confidence_described(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        assert "high" in body
        assert "medium" in body
        assert "low" in body
        assert "0.8" in body
        assert "0.5" in body


# ── Authored-specific trust contract (locks the fix) ───────────────


class TestAuthoredTrustContract:
    def test_advertising_is_not_a_violation(self) -> None:
        """Advertising/branding must be explicitly declared acceptable."""
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        assert "advertising" in body
        # The whole point of the hotfix: ads/branding are EXPECTED.
        assert "expected" in body
        assert "not violations" in body or "not a violation" in body

    def test_external_links_are_not_a_violation(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        assert "external" in body and "link" in body

    def test_off_topic_is_high_bar(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        assert "high bar" in body
        assert "not course material" in body

    def test_not_framed_as_homework_submission(self) -> None:
        """Regression lock: the authored prompt must NOT inherit the
        homework framing that caused the Phase 2.3 false positives."""
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        assert "homework submission" not in sp.system.lower()
        # Positive framing: authored course materials.
        assert "authored course materials" in sp.system.lower()


# ── Template render contract ───────────────────────────────────────


class TestPromptRender:
    def test_renders_with_submission_text(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        rendered = sp.render(
            submission_text="def fib(n): return n",
            course_title="",
            course_description="",
            node_title="",
            node_description="",
            outline_summary="",
        )
        assert rendered.user is not None
        assert "def fib(n): return n" in rendered.user
        assert rendered.system == sp.system
        assert "### Course context" not in rendered.user

    def test_renders_with_course_context(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        rendered = sp.render(
            submission_text="def fib(n): return n",
            course_title="Python Basics",
            course_description="Introductory Python course",
            node_title="Recursion",
            node_description="Learn about recursive functions",
            outline_summary="Functions, recursion, base cases",
        )
        assert rendered.user is not None
        assert "### Course context" in rendered.user
        assert "Python Basics" in rendered.user
        assert "Recursion" in rendered.user

    def test_strict_undefined_raises_on_missing_var(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        with pytest.raises(UndefinedError):
            sp.render()


# ── Ladder config integration ──────────────────────────────────────


class TestAuthoredSafetyCheckLadderLoads:
    def test_stage_present(self) -> None:
        cfg = load_ladder_config(_CONFIG_DIR)
        stage = cfg.get_stage("safety_check_authored")
        assert stage.prompt_ref == _PROMPT_REF

    def test_ladder_three_entries_in_order(self) -> None:
        cfg = load_ladder_config(_CONFIG_DIR)
        stage = cfg.get_stage("safety_check_authored")
        assert len(stage.ladder) == 3
        providers = [entry.provider for entry in stage.ladder]
        models = [entry.model for entry in stage.ladder]
        assert providers == ["mistral", "deepseek", "gemini"]
        assert models == [
            "mistral-small-latest",
            "deepseek-v4-flash",
            "gemini-2.5-flash",
        ]

    def test_homework_safety_check_still_loads(self) -> None:
        """Regression guard: adding the authored stage must not break
        the existing homework ``safety_check`` stage in the same file."""
        cfg = load_ladder_config(_CONFIG_DIR)
        stage = cfg.get_stage("safety_check")
        assert stage.prompt_ref == "prompts/safety_check/v1.md"
        assert len(stage.ladder) == 3
