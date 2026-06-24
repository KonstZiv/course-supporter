"""Smoke test for the sanity gate prompt (sprint-mentor T7).

Loader-level invariants — the prompt loads, exposes its sections, and renders
with a complete sample context (catching Jinja typos / missing render vars)
without any real LLM call. The untrusted-submission framing and both language
branches render.
"""

from __future__ import annotations

from pathlib import Path

from course_supporter.llm.prompt_loader_md import load_prompt

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = _REPO_ROOT / "prompts"
_SANITY = "sanity_check/v1.md"


class TestSanityPrompt:
    def test_renders_with_language(self) -> None:
        prompt = load_prompt(_SANITY, base_path=_PROMPTS_DIR)
        rendered = prompt.render(
            task_title="Recursion",
            task_description="Implement factorial.",
            task_text="Write a recursive factorial.",
            submission_text="def f(): ...",
            language="Ukrainian",
        )
        assert rendered.system is not None
        assert "Ukrainian" in rendered.system
        assert "<student_submission>" in rendered.user
        assert "def f(): ..." in rendered.user

    def test_renders_without_language(self) -> None:
        prompt = load_prompt(_SANITY, base_path=_PROMPTS_DIR)
        rendered = prompt.render(
            task_title="T",
            task_description="D",
            task_text="X",
            submission_text="...",
            language=None,
        )
        assert rendered.system is not None
        # Conservative framing must survive both branches.
        assert "match" in rendered.system.lower()
        assert "mismatch" in rendered.system.lower()
