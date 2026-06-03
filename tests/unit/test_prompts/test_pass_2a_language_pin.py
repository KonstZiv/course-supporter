"""Pass 2a language-pin render tests (task 2.4.14).

The three Pass 2a prompts (``pass_2a_mapping/v1.md`` for text+web,
``audio_pass_2a_mapping/v1.md``, ``video_pass_2a_mapping/v1.md``) all
expose a ``{{ language }}`` Jinja variable that pins the output
language and drives the bilingual concept-doubling rule. They share
the same defensive ``{% if language %}…{% else %}…{% endif %}``
structure: a truthy value selects the pinned branch, ``None`` (or any
falsy value) selects the legacy "detect from input" branch, and a
missing context entry would raise ``jinja2.UndefinedError`` under the
StagePrompt loader's ``StrictUndefined`` mode.

These tests pin the **render contract** (no live LLM): the prompt
must compile cleanly under both pin and fallback contexts, must
reference the expected branch markers, and must never UndefinedError
on a ``language=None`` call (which is the actual unrooted-defence
path through production code).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from course_supporter.llm.prompt_loader_md import load_prompt

# ``parents[3]`` walks: test file -> test_prompts -> unit -> tests -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = _REPO_ROOT / "prompts"

# The four Pass 2a prompt refs touched by tasks 2.4.14 + 2.4.15.
# ``presentation_pass_2a_mapping/v1.md`` joins the parametrisation in
# task 2.4.15: it carries the same ``{% if language %}…{% else %}…``
# defensive gate as the three task-14 prompts, so every branch test
# in this module applies to it identically.
_PASS_2A_REFS = [
    "pass_2a_mapping/v1.md",
    "audio_pass_2a_mapping/v1.md",
    "video_pass_2a_mapping/v1.md",
    "presentation_pass_2a_mapping/v1.md",
]


def _render_kwargs_for(prompt_ref: str, *, language: str | None) -> dict[str, object]:
    """Minimal render context for each prompt (besides ``language``)."""
    if prompt_ref == "pass_2a_mapping/v1.md":
        return {"text": "sample document body", "language": language}
    if prompt_ref == "audio_pass_2a_mapping/v1.md":
        return {
            "words_count": 5,
            "words_json": '[{"text":"hello","start":0.0,"end":1.0}]',
            "language": language,
        }
    if prompt_ref == "video_pass_2a_mapping/v1.md":
        return {
            "words_count": 5,
            "words_json": '[{"text":"hello","start":0.0,"end":1.0}]',
            "visuals": "[word ~0, 00:00] sample slide",
            "language": language,
        }
    if prompt_ref == "presentation_pass_2a_mapping/v1.md":
        slides_json = "\n".join(f"[Slide {i}] sample slide {i}" for i in (1, 2, 3))
        return {
            "file_title": "Sample Deck",
            "n_slides": 3,
            "slides_json": slides_json,
            "language": language,
        }
    raise AssertionError(f"unexpected prompt_ref: {prompt_ref}")


@pytest.mark.parametrize("prompt_ref", _PASS_2A_REFS)
class TestPass2aLanguagePin:
    """Branch-coverage tests for the ``{{ language }}`` pin."""

    def test_pin_branch_renders_with_language_name(self, prompt_ref: str) -> None:
        # Truthy ``language`` selects the pinned branch — the rendered
        # system prompt names the course language explicitly. We assert
        # the literal name is present, which proves the {% if %} guard
        # opened correctly AND the variable interpolated.
        prompt = load_prompt(prompt_ref, base_path=_PROMPTS_DIR)
        rendered = prompt.render(**_render_kwargs_for(prompt_ref, language="Ukrainian"))

        assert rendered.system is not None
        assert "Ukrainian" in rendered.system
        # Pin-branch directive must surface (sanity that we did not
        # accidentally land in the else-branch despite the variable
        # interpolation succeeding).
        assert "pinned" in rendered.system.lower()

    def test_fallback_branch_renders_without_pin_when_language_none(
        self, prompt_ref: str
    ) -> None:
        # ``None`` is falsy → {% if language %} selects the else-branch
        # ("detect from input" — legacy behaviour). The pinned-language
        # name MUST NOT appear and the rendered prompt MUST NOT raise.
        prompt = load_prompt(prompt_ref, base_path=_PROMPTS_DIR)
        rendered = prompt.render(**_render_kwargs_for(prompt_ref, language=None))

        assert rendered.system is not None
        # Legacy fallback wording lives in the else-branch only.
        # Normalise whitespace because Jinja preserves the source
        # newlines inside the {% else %} block.
        normalised = " ".join(rendered.system.split())
        assert "Detect concepts in whichever language" in normalised
        # Pin-branch marker must be absent on the fallback path.
        assert "pinned" not in normalised.lower()

    def test_render_does_not_raise_undefinederror_when_language_is_none(
        self, prompt_ref: str
    ) -> None:
        # Defense-in-depth against StrictUndefined: every production
        # call site passes ``language=...`` (possibly ``None``), so the
        # Jinja context always carries the key — ``UndefinedError`` here
        # would mean the prompt accidentally references an additional
        # undefined variable in the language section.
        prompt = load_prompt(prompt_ref, base_path=_PROMPTS_DIR)
        # No exception expected.
        prompt.render(**_render_kwargs_for(prompt_ref, language=None))

    def test_calibration_does_not_drag_ukrainian_term_into_other_course_language(
        self, prompt_ref: str
    ) -> None:
        # Regression guard for operator's correction 1 (2026-06-03): the
        # previous calibration body interpolated ``{{ language }}`` into
        # sentences with concrete Ukrainian facts ("змінна"), which for
        # ``language="Turkish"`` rendered "The Turkish pedagogical
        # tradition uses 'змінна'" — disinformation for any non-Ukrainian
        # course. The fix pins the example to a literal Ukrainian
        # illustration and lets the abstract rule reference the actual
        # ``{{ language }}`` separately. Bug surface: course language
        # and "змінна" co-located in the *same line* (single sentence).
        prompt = load_prompt(prompt_ref, base_path=_PROMPTS_DIR)
        rendered = prompt.render(**_render_kwargs_for(prompt_ref, language="Turkish"))

        assert rendered.system is not None
        # The course language is interpolated all over the pinned
        # branch, so the right invariant is: nowhere is the course
        # language ("Turkish") in the *same line* as the Ukrainian
        # native term "змінна". That is exactly the
        # "{{ language }} pedagogical tradition uses 'змінна'"
        # regression we are guarding against.
        for line in rendered.system.splitlines():
            if "змінна" in line:
                assert "Turkish" not in line, (
                    "Regression of correction 1: course language "
                    "co-located with the Ukrainian native term in a "
                    f"single sentence: {line!r}"
                )
        # Additionally — the Ukrainian native term must only surface
        # within a few lines of an explicit "Ukrainian" anchor (the
        # fixed-illustration framing); a stray "змінна" floating in a
        # generic {{ language }} sentence would mean we re-introduced
        # the bug at a different spot.
        lines = rendered.system.splitlines()
        for idx, line in enumerate(lines):
            if "змінна" in line:
                window = " ".join(lines[max(0, idx - 2) : idx + 3])
                assert "Ukrainian" in window, (
                    "'змінна' surfaced outside the fixed Ukrainian "
                    f"illustration window (lines {idx - 2}..{idx + 2}): "
                    f"{window!r}"
                )

    def test_calibration_section_is_gated_on_language(self, prompt_ref: str) -> None:
        # Operator's correction 2 (2026-06-03): the calibration must be
        # wrapped in ``{% if language %}`` because its content directly
        # contradicts the fallback ``{% else %}`` ("detect from input,
        # no doubling"). With ``language=None`` no calibration text
        # should reach the rendered prompt.
        prompt = load_prompt(prompt_ref, base_path=_PROMPTS_DIR)
        rendered = prompt.render(**_render_kwargs_for(prompt_ref, language=None))
        assert rendered.system is not None
        assert "Concept-doubling calibration" not in rendered.system
        # The Ukrainian native term is exclusive to the calibration body
        # → must not surface at all on the fallback path.
        assert "змінна" not in rendered.system

    def test_code_cohesion_instruction_survives_language_rewrite(
        self, prompt_ref: str
    ) -> None:
        # Task-doc explicit constraint (extension 2 of pre-flight):
        # the pre-existing code-cohesion instruction must NOT be lost
        # in the language-note rewrite. We assert the surviving wording
        # appears in the rendered system prompt regardless of pin vs.
        # fallback branch.
        prompt = load_prompt(prompt_ref, base_path=_PROMPTS_DIR)
        rendered = prompt.render(**_render_kwargs_for(prompt_ref, language="Ukrainian"))
        assert rendered.system is not None
        assert "code" in rendered.system.lower()
        # Specifically: code identifiers / blocks must not be treated
        # as segment boundaries. This is the load-bearing invariant
        # for the offset/word-idx mapping; if it disappears the
        # downstream Pass 2b slice misaligns. ``boundar`` covers both
        # "segment boundary" (audio/video singular) and "segment
        # boundaries" (text plural).
        assert "boundar" in rendered.system.lower()
