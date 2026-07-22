"""№21 (decision 7): the per-file describe prompt is role-aware.

An auxiliary file is described CONCISELY (not less thoroughly) — the
``code_segment_description`` prompt carries a concision instruction only when
``is_auxiliary`` is true. Both branches must compile under the loader's
``StrictUndefined`` (the production render contract passes ``is_auxiliary`` on
every call).
"""

from __future__ import annotations

from pathlib import Path

from course_supporter.llm.prompt_loader_md import load_prompt

# test file -> test_prompts -> unit -> tests -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = _REPO_ROOT / "prompts"
_REF = "code_segment_description/v1.md"


def _render(*, is_auxiliary: bool) -> str:
    prompt = load_prompt(_REF, base_path=_PROMPTS_DIR)
    rendered = prompt.render(
        file_path="src/app.ts",
        skeleton="class App {}",
        namespace_core="App",
        language="Ukrainian",
        is_auxiliary=is_auxiliary,
    )
    assert rendered.system is not None
    return rendered.system


def test_auxiliary_prompt_carries_concision_instruction() -> None:
    system = _render(is_auxiliary=True)
    assert "AUXILIARY FILE" in system
    assert "CONCISELY" in system


def test_full_prompt_has_no_auxiliary_instruction() -> None:
    system = _render(is_auxiliary=False)
    assert "AUXILIARY FILE" not in system


def test_both_branches_compile_under_strict_undefined() -> None:
    # A missing ``is_auxiliary`` would UndefinedError; passing both values must
    # render cleanly — the guard on the production render contract.
    assert _render(is_auxiliary=True)
    assert _render(is_auxiliary=False)
