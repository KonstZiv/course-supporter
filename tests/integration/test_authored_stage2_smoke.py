"""Real-API authored Stage 2 safety smoke test (RUN_SMOKE-gated; Phase 2.3 hotfix).

Exercises run_stage2_safety_check(content_kind="authored") against the real
safety_check_authored prompt + real LLM ladder. Asserts that an authored deck
carrying branding + external links is accepted (is_safe=True) — the contract
the homework-framed prompt violated (rejected such content as off_topic).
Closes the real-LLM gap: existing Stage 2 tests mock the provider boundary,
so they cannot catch a prompt-content false positive. Mirrors
test_audio_pipeline_smoke.py / test_presentation_pipeline_smoke.py.
"""

from __future__ import annotations

import os
import uuid

import pytest

from course_supporter.security.schemas import SafetyResult
from course_supporter.security.stage2 import run_stage2_safety_check
from course_supporter.service_logging import set_job_from_arq

pytestmark = [
    pytest.mark.authored_safety_smoke,
    pytest.mark.skipif(
        not os.getenv("RUN_SMOKE"),
        reason=(
            "Real-API authored Stage 2 smoke test gated by RUN_SMOKE env "
            "variable; opt-in only to avoid accidental real-API consumption."
        ),
    ),
]

# Legitimate authored material: course content + platform branding + an
# external resource link. Under the homework prompt this was rejected as
# off_topic ("advertising"); the authored prompt must accept it.
_AUTHORED_TEXT = (
    "Lesson 5: Python lists, dictionaries, tuples, and slices.\n"
    "Brought to you by an educational platform — subscribe at example-platform.com.\n"
    "Further reading: https://docs.python.org/3/tutorial/datastructures.html\n\n"
    "A list is an ordered, mutable collection: nums = [1, 2, 3]. A dict maps "
    "keys to values: ages = {'ann': 30}. Tuples are immutable: p = (1, 2). "
    "Slicing: nums[1:3] returns [2, 3]."
)


async def test_authored_stage2_accepts_branding_and_links() -> None:
    """Authored deck with branding + external link → is_safe=True."""
    from course_supporter.llm.stage_router import StageRouter

    stage_router = StageRouter.from_config()
    set_job_from_arq(uuid.uuid4())

    result = await run_stage2_safety_check(
        _AUTHORED_TEXT,
        router=stage_router,
        content_kind="authored",
    )
    assert isinstance(result, SafetyResult)
    # The fix: advertising/branding + external links are NOT violations.
    assert result.is_safe is True
    assert result.violations == []
