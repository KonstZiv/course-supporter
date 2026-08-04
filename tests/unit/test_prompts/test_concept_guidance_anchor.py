"""Concept-quality phase 1, lever 2 (Д2): stricter concept-composition anchor.

The stricter instruction — do not name invented teaching artifacts, do not
list technologies the material does not show in use — is inserted verbatim
into the concept-requirements section of every per-file description prompt.

Before this test the section's presence was guarded only by a single
presentation test (``test_presentation_prompts.py``), so removing the rule
from the other four templates would have gone unnoticed. This test closes that
gap across all five.

The anchor is static text in the raw ``## System`` section (outside every
Jinja conditional), so loading without rendering is sufficient.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from course_supporter.llm.prompt_loader_md import load_prompt

# test file -> test_prompts -> unit -> tests -> repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = _REPO_ROOT / "prompts"

# Own list — deliberately NOT reusing ``_PASS_2A_REFS`` from
# test_pass_2a_language_pin.py: that set drives language-pin / concept-doubling
# checks that ``code_segment_description`` does not have.
_PER_FILE_DESCRIPTION_REFS = [
    "pass_2a_mapping/v1.md",
    "audio_pass_2a_mapping/v1.md",
    "video_pass_2a_mapping/v1.md",
    "presentation_pass_2a_mapping/v1.md",
    "code_segment_description/v1.md",
]

_ANCHOR = (
    "Do not list a concept that names a teaching artifact the author "
    "invented for demonstration — class, component, variable, file, or "
    "project names that exist only as examples. Do not list a technology, "
    "library, or framework unless the material itself shows it in use."
)


@pytest.mark.parametrize("prompt_ref", _PER_FILE_DESCRIPTION_REFS)
def test_concept_guidance_anchor_present(prompt_ref: str) -> None:
    prompt = load_prompt(prompt_ref, base_path=_PROMPTS_DIR)
    assert prompt.system is not None
    assert _ANCHOR in prompt.system
