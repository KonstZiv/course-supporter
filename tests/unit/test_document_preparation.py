"""№21 DOCUMENT_PREPARATION invariant guards.

I3: the prep body is deterministic — zero LLM. The guard scans the body source
for any LLM-router reference and carries a POSITIVE CONTROL (the same detector
MUST fire on ``arq_ingest_material``, which genuinely uses the routers) so a
green scan cannot rot into a no-op.
"""

from __future__ import annotations

import inspect

from course_supporter.api.tasks import arq_ingest_material, arq_prepare_document

# The LLM-router surface a deterministic body must never touch. StageRouter /
# ModelRouter are the ladder entry points; the ``ctx[...]`` keys are how a body
# pulls them out of the ARQ context.
_LLM_ROUTER_TOKENS = ("stage_router", "model_router", "StageRouter", "ModelRouter")


def _body_source(fn: object) -> str:
    """Source of the ORIGINAL task body (unwrap the through_seam decorator)."""
    return inspect.getsource(inspect.unwrap(fn))  # type: ignore[arg-type]


def test_prep_body_uses_no_llm_router() -> None:
    """I3: the prep body never references an LLM router (deterministic, zero LLM)."""
    hits = [t for t in _LLM_ROUTER_TOKENS if t in _body_source(arq_prepare_document)]
    assert not hits, f"prep body references LLM router(s): {hits}"


def test_i3_detector_positive_control() -> None:
    """The detector fires on arq_ingest_material — else the I3 lock is blind."""
    src = _body_source(arq_ingest_material)
    assert any(t in src for t in _LLM_ROUTER_TOKENS), "I3 detector caught nothing"
