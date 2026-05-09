"""Tests for the Stage 2 safety_check prompt + ladder config (KD14, KD16).

Lock the four required system-prompt elements (anti-jailbreak preamble,
strict JSON output, 22-language coverage, calibration examples) so that
future token-trimming or stylistic edits cannot silently weaken the
safety classifier. The tests assert phrase variants -- not verbatim
strings -- so the prompt remains editable for clarity without breaking
the contract.

Ladder regression guard ensures the new ``safety_check`` stage loads
via the sealed 0.5 :func:`load_ladder_config` and the existing
``mentor_example`` stage still loads alongside it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jinja2 import UndefinedError

from course_supporter.llm.ladder_config import load_ladder_config
from course_supporter.llm.prompt_loader_md import load_prompt

# Repo-CWD-relative path, matching how StageConfig.prompt_ref is resolved
# in production. Tests run from the backend repo root via pytest.
_PROMPT_REF = "prompts/safety_check/v1.md"
_CONFIG_DIR = Path("config")

# 22 target audience languages per vision §KD14. Locked here as a
# regression guard: token-trimming the prompt without explicit vision
# review will break this list.
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
        # 0.6 deferred debt: assistant-role seeding lives behind the
        # 0.5 deferred ``StageRouter`` retry hook, not in MVP prompts.
        # Lock the absence so a stray ``## Assistant`` doesn't sneak in.
        sp = load_prompt(_PROMPT_REF)
        assert sp.assistant is None


# ── 22-language coverage (watchpoint #8 closure) ───────────────────


class TestPromptCovers22Languages:
    @pytest.mark.parametrize("language", _TARGET_LANGUAGES)
    def test_each_target_language_named(self, language: str) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        assert language in sp.system, (
            f"target audience language {language!r} missing from prompt; "
            "do NOT trim the language list without vision-side approval"
        )

    def test_count_is_exactly_22(self) -> None:
        # Sanity check the enumeration length itself -- not the prompt
        # body, but the test fixture: if vision changes the supported
        # set, both this constant and the prompt must update together.
        assert len(_TARGET_LANGUAGES) == 22


# ── JSON output strictness ─────────────────────────────────────────


class TestPromptJSONStrictness:
    def test_demands_json_object(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        # Phrase variants -- the prompt may evolve but must keep
        # demanding a strict JSON object output.
        assert "json" in body
        assert "only" in body  # "only a JSON object" or similar

    def test_forbids_markdown_fence(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        # Many models default to wrapping JSON in ```json``` fences,
        # which breaks Pydantic parsing. Lock the explicit prohibition.
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
        # The four ViolationCategory values per security.schemas.
        # Hard-locking these names ties the prompt contract to the
        # Pydantic schema; renaming an enum member without updating
        # the prompt would silently produce parse failures in (i).
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
        # Phrase variants for "treat content as data, not instructions".
        assert "as data" in body
        assert "not" in body and "instruction" in body

    def test_explicit_never_follow(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        # The classifier must be told explicitly never to obey
        # instructions found inside submission text.
        assert "never" in body
        assert "submission" in body

    def test_xml_delimiters_for_submission(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.user is not None
        # The Anthropic-recommended pattern: wrap untrusted input in
        # XML-style tags so the model can distinguish data from
        # instructions structurally. Both opening and closing tags
        # must be present.
        assert "<submission>" in sp.user
        assert "</submission>" in sp.user


# ── Calibration examples ───────────────────────────────────────────


class TestPromptCalibrationExamples:
    def test_includes_safe_and_unsafe_examples(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        # Calibration discipline: both kinds of example must be
        # present so the model does not become overcautious.
        assert "is_safe=true" in body
        assert "is_safe=false" in body

    def test_three_tier_confidence_described(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        assert sp.system is not None
        body = sp.system.lower()
        # 3-tier confidence guidance per Q1 acceptance: high / medium
        # / low buckets with rough numerical bounds.
        assert "high" in body
        assert "medium" in body
        assert "low" in body
        assert "0.8" in body  # high tier threshold
        assert "0.5" in body  # medium tier threshold


# ── Template render contract ───────────────────────────────────────


class TestPromptRender:
    def test_renders_with_submission_text(self) -> None:
        sp = load_prompt(_PROMPT_REF)
        # All six vars passed (per Phase 1.2.2 KD-1.2-I — production
        # caller ``run_stage2_safety_check`` always provides them, with
        # empty strings when no course context is available).
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
        # System section has no Jinja vars -- it should round-trip
        # unchanged through render.
        assert rendered.system == sp.system
        # Course context block is gated on truthiness; with empty
        # strings the block is omitted.
        assert "### Course context" not in rendered.user

    def test_renders_with_course_context(self) -> None:
        """Course context block appears when course_title is non-empty."""
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
        # Any required Jinja var omitted raises UndefinedError under
        # StrictUndefined.
        with pytest.raises(UndefinedError):
            sp.render()


# ── Ladder config integration ──────────────────────────────────────


class TestSafetyCheckLadderLoads:
    def test_stage_present(self) -> None:
        cfg = load_ladder_config(_CONFIG_DIR)
        stage = cfg.get_stage("safety_check")
        assert stage.prompt_ref == _PROMPT_REF

    def test_ladder_three_entries_in_order(self) -> None:
        cfg = load_ladder_config(_CONFIG_DIR)
        stage = cfg.get_stage("safety_check")
        assert len(stage.ladder) == 3
        providers = [entry.provider for entry in stage.ladder]
        models = [entry.model for entry in stage.ladder]
        assert providers == ["mistral", "deepseek", "gemini"]
        assert models == [
            "mistral-small-latest",
            "deepseek-chat",
            "gemini-2.5-flash",
        ]


class TestMentorExampleStillLoads:
    """Regression guard: the existing example stage must continue
    loading when the new ``safety_check`` stage is added to the same
    file. Catches accidental YAML structural breakage."""

    def test_present_with_two_entries(self) -> None:
        cfg = load_ladder_config(_CONFIG_DIR)
        stage = cfg.get_stage("mentor_example")
        assert len(stage.ladder) == 2
        assert stage.prompt_ref == "prompts/example/v1.md"
