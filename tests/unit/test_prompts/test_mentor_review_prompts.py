"""Smoke tests for the Mentor review graph prompts (sprint-mentor T6).

Loader-level invariants — each prompt loads, exposes its sections, and renders
with a complete sample context (catching Jinja typos / missing render vars)
without any real LLM call. Optional fields (author_mentor_notes, student_note)
render in both the present and absent branches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from course_supporter.llm.prompt_loader_md import load_prompt

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = _REPO_ROOT / "prompts"

_NODE_COURSE = "mentor_layered_evaluation_node_course/v1.md"
_INDUSTRY = "mentor_layered_evaluation_industry/v1.md"
_DENOISING = "mentor_denoising/v1.md"
_SYNTHESIS = "mentor_synthesis/v1.md"

_NODE_SUMMARY: dict[str, Any] = {
    "title": "Recursion",
    "description": "Recursion basics.",
    "learning_objectives": ["Define a base case"],
    "success_criteria": ["Terminates"],
    "common_mistakes": ["Infinite recursion"],
}
_COURSE_SUMMARY: dict[str, Any] = {
    "title": "Algorithms",
    "description": "Intro algorithms.",
    "enclosing_context": "Part of CS101.",
    "success_criteria": ["Sound complexity reasoning"],
}
_CRITERIA = [{"statement": "Has a base case", "evidence": "explicit if-return"}]


class TestNodeCoursePrompt:
    def test_renders_with_author_notes(self) -> None:
        prompt = load_prompt(_NODE_COURSE, base_path=_PROMPTS_DIR)
        rendered = prompt.render(
            task_title="T",
            task_description="D",
            task_text="X",
            criteria=_CRITERIA,
            node_summary=_NODE_SUMMARY,
            course_summary=_COURSE_SUMMARY,
            author_mentor_notes="emphasise edge cases",
            submission_text="def f(): ...",
            language="English",
        )
        assert rendered.system is not None
        assert "emphasise edge cases" in rendered.system
        assert "<student_submission>" in rendered.user

    def test_renders_without_author_notes_or_criteria(self) -> None:
        prompt = load_prompt(_NODE_COURSE, base_path=_PROMPTS_DIR)
        rendered = prompt.render(
            task_title="T",
            task_description="D",
            task_text="X",
            criteria=[],
            node_summary=_NODE_SUMMARY,
            course_summary=_COURSE_SUMMARY,
            author_mentor_notes=None,
            submission_text="...",
            language=None,
        )
        assert rendered.user is not None
        assert "No cached criteria" in rendered.user


class TestIndustryPrompt:
    def test_renders(self) -> None:
        prompt = load_prompt(_INDUSTRY, base_path=_PROMPTS_DIR)
        rendered = prompt.render(
            task_title="T",
            task_description="D",
            task_text="X",
            submission_text="...",
            language="English",
        )
        assert rendered.system is not None
        assert "industry" in rendered.system.lower()


class TestDenoisingPrompt:
    def test_renders_with_history(self) -> None:
        prompt = load_prompt(_DENOISING, base_path=_PROMPTS_DIR)
        rendered = prompt.render(
            aggregate_score=75,
            current_layers=[
                {"layer": "node", "score": 80, "weaknesses": ["off-by-one"]}
            ],
            history=[
                {
                    "submission_id": "s1",
                    "score": 70,
                    "correctness": "partially_correct",
                    "same_task": True,
                    "weaknesses": ["off-by-one"],
                }
            ],
            denoise_cap=10,
            language="English",
        )
        assert rendered.user is not None
        assert "s1" in rendered.user

    def test_renders_empty_history(self) -> None:
        prompt = load_prompt(_DENOISING, base_path=_PROMPTS_DIR)
        rendered = prompt.render(
            aggregate_score=50,
            current_layers=[],
            history=[],
            denoise_cap=10,
            language=None,
        )
        assert rendered.user is not None
        assert "No prior attempts" in rendered.user


class TestSynthesisPrompt:
    def test_renders_with_note_and_signals(self) -> None:
        prompt = load_prompt(_SYNTHESIS, base_path=_PROMPTS_DIR)
        rendered = prompt.render(
            persona="an experienced senior colleague",
            layers=[
                {"layer": "node", "score": 80, "strengths": ["clear"], "weaknesses": []}
            ],
            aggregate_score=75,
            denoised_score=72,
            history_reconciliation={"recidivism": [], "corrections": []},
            score_signals=[
                {"layer": "node", "direction": "up", "magnitude": 4, "reason": "clear"}
            ],
            student_note="please focus on style",
            language="English",
        )
        assert rendered.system is not None
        assert "please focus on style" in rendered.system
        assert rendered.user is not None
        assert "[up 4] node" in rendered.user

    def test_renders_without_note(self) -> None:
        prompt = load_prompt(_SYNTHESIS, base_path=_PROMPTS_DIR)
        rendered = prompt.render(
            persona="an experienced senior colleague",
            layers=[],
            aggregate_score=50,
            denoised_score=50,
            history_reconciliation={"recidivism": [], "corrections": []},
            score_signals=[],
            student_note=None,
            language=None,
        )
        assert rendered.system is not None
        assert "left no note" in rendered.system
