"""Krok 4 — Pass 1 / «Eyes» vision chain-diff description (Phase 2.4).

The first real LLM call in the video pipeline. A vision model (Qwen3-VL
rung 1 → Gemini fallback, ``config/ladders_video.yaml``) describes the
kept frames as time-bound text in a *chain-diff* mode: an ``anchor`` is a
full, self-contained description; a ``diff`` records only what changed
versus the previous frame.

Frames are packed into chunks under an output-token budget (one vision
call per chunk, N frames per call); the first frame of every chunk is an
anchor so the chunk is self-sufficient. The chain is **within-chunk
only** — no cross-chunk memory is carried, so chunks are independent and
run with bounded concurrency. This is a thinner reuse of the
``vd.visual_analyzer`` knowledge (R0.2: anchor/diff idea + prompt shape),
which described frames one at a time rather than in chunks.

PiP responsibility split (sealed Krok3→Krok4 contract): Krok 3 *detects*
the talking-head box and leaves the output JPEGs raw; Pass 1 *applies*
the mask here (grey-fills the box) before the frame is sent to the model,
so the vision model never describes the overlay.

Output is plain text (NOT JSON, symmetric with presentation Pass 1):
one marked block per frame, parsed back into ``FrameDescription`` by the
``[FRAME HH:MM:SS]`` marker. A wrong block count or a misaligned marker
raises ``StructuralRetryError`` so the router's instructor-style retry +
ladder fallback handles it; terminal failure raises ``ProcessingError``
(R1 ``state=ERROR``).
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import structlog

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.video_pipeline.schemas import (
    ChangeClass,
    FrameDescription,
    FrameKind,
)
from course_supporter.llm.error_categories import StructuralRetryError

if TYPE_CHECKING:
    from pathlib import Path

    from course_supporter.ingestion.video_pipeline.schemas import (
        DetectionResult,
        PiPMask,
        SampledFrame,
        Scene,
        VideoFileMetadata,
    )
    from course_supporter.llm.stage_router import StageRouter

logger = structlog.get_logger()

_STAGE_NAME = "video_pass_1_vision"

# ── calibration constants (seed; spike/RUN_SMOKE-tuned, C6) ──────────────
# Output-token budget per chunk: the greedy packer adds frames until the
# *estimated* output reaches this. 6144 = 3/4 of the Qwen3-VL output ceiling
# (8192), leaving ~2048 headroom for chain-diff verbosity spread. The hard
# ceiling stays 8192 (the per-rung ``max_output_tokens`` in
# ``ladders_video.yaml``); the spike confirms real output < 8192 at the chosen
# frame count — if it nears the ceiling, lower the count, never raise the
# budget.
_BUDGET_TOKENS = 6144
# Max images per vision call. ``vd/`` sent <= 3; the chunk-diff jump to many
# frames is bounded here pending the DashScope / Gemini per-request image-cap
# probe (RUN_SMOKE C4 — neither provider enforces a cap in our code, so this
# is likely the binding constraint, not the token budget). Conservative seed.
_IMAGE_BUDGET = 12
# Per-frame output-token estimates driving the greedy packer. An anchor carries
# a full description, a diff only a delta — seed values, calibrated against ESC
# ``tokens_out`` during the spike.
_EST_ANCHOR_TOKENS = 700
_EST_DIFF_TOKENS = 200
# Bounded concurrency over chunks. Chunks are independent (within-chunk chain
# only), so they may run concurrently; kept low to stay under provider rate
# limits on heavy multi-image calls (tune at RUN_SMOKE).
_CHUNK_CONCURRENCY = 2
# Neutral mid-grey written over the PiP box before a frame is sent to the
# vision model (so it does not describe the talking-head overlay). Independent
# of ``detection._PIP_MASK_GRAY`` — same value, different rationale: detection
# needs a *constant* fill across frames for stable dedup metrics, here it is a
# *visually neutral* fill for the LLM. They coincide at 128 by choice.
_PIP_FILL_GRAY = 128

# Anchored to line start (the prompt requires each marker on its own line),
# so a ``[FRAME ..]``-shaped string inside a description does not split a block.
_MARKER_RE = re.compile(r"^\[FRAME \d{2}:\d{2}:\d{2}\]", re.MULTILINE)


@dataclass(frozen=True)
class _PF:
    """A planned frame paired with its scene id (scene id lives on ``Scene``)."""

    sampled: SampledFrame
    scene_id: int


def _format_marker(position_ms: int) -> str:
    """``frame_position_ms`` → the ``[FRAME HH:MM:SS]`` marker (HH for 150-min)."""
    total_sec = position_ms // 1000
    hours, rem = divmod(total_sec, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"[FRAME {hours:02d}:{minutes:02d}:{seconds:02d}]"


def _kind_at(index_in_chunk: int, change_class: ChangeClass) -> FrameKind:
    """Anchor iff first-in-chunk (self-sufficient) or a natural scene start."""
    if index_in_chunk == 0:
        return FrameKind.ANCHOR
    if change_class in (ChangeClass.FIRST, ChangeClass.BOUNDARY):
        return FrameKind.ANCHOR
    return FrameKind.DIFF


def _est_cost(kind: FrameKind) -> int:
    return _EST_ANCHOR_TOKENS if kind is FrameKind.ANCHOR else _EST_DIFF_TOKENS


def _chunk_cost(frames: list[_PF]) -> int:
    """Estimated output tokens for a chunk (first frame anchored)."""
    return sum(
        _est_cost(_kind_at(i, pf.sampled.change_class)) for i, pf in enumerate(frames)
    )


# ── chunk planning (budget + image cap + soft scene boundary) ────────────


def _split_unit(frames: list[_PF]) -> list[list[_PF]]:
    """Split one over-budget scene into sub-chunks (greedy; first = anchor)."""
    out: list[list[_PF]] = []
    cur: list[_PF] = []
    cur_cost = 0
    for pf in frames:
        kind = _kind_at(len(cur), pf.sampled.change_class)
        cost = _est_cost(kind)
        if cur and (cur_cost + cost > _BUDGET_TOKENS or len(cur) >= _IMAGE_BUDGET):
            out.append(cur)
            cur, cur_cost = [], 0
            cost = _est_cost(FrameKind.ANCHOR)  # new chunk → first frame anchored
        cur.append(pf)
        cur_cost += cost
    if cur:
        out.append(cur)
    return out


def _scene_units(scenes: list[Scene]) -> list[list[_PF]]:
    """One unit per scene; over-budget scenes are pre-split so each unit fits.

    Keeping a scene whole (unless it alone exceeds the budget) is the soft
    "do not break a scene" rule — chunk boundaries fall between scenes, not
    inside one, except for a single scene too large to fit.
    """
    units: list[list[_PF]] = []
    for scene in scenes:
        pfs = [_PF(sampled=f, scene_id=scene.scene_id) for f in scene.frames]
        if not pfs:
            continue
        if _chunk_cost(pfs) <= _BUDGET_TOKENS and len(pfs) <= _IMAGE_BUDGET:
            units.append(pfs)
        else:
            units.extend(_split_unit(pfs))
    return units


def _plan_chunks(scenes: list[Scene]) -> list[list[_PF]]:
    """Pack scene units into chunks under the token budget + image cap."""
    chunks: list[list[_PF]] = []
    cur: list[_PF] = []
    for unit in _scene_units(scenes):
        if cur and (
            _chunk_cost(cur) + _chunk_cost(unit) > _BUDGET_TOKENS
            or len(cur) + len(unit) > _IMAGE_BUDGET
        ):
            chunks.append(cur)
            cur = []
        cur.extend(unit)
    if cur:
        chunks.append(cur)
    return chunks


# ── PiP masking (applied here, not in Krok 3) ────────────────────────────


def _read_masked_jpeg(path: Path, mask: PiPMask | None) -> bytes:
    """Read a raw frame JPEG, grey-fill the PiP box, return re-encoded JPEG.

    The mask box (from Krok 3) is in the same pixel space as the frame
    (ffmpeg extracts at native resolution, no scale filter), so no
    coordinate scaling is needed — only a defensive clamp to the decoded
    frame dimensions (guards a rotated container whose coded size differs).
    """
    img = cv2.imread(str(path))
    if img is None:
        raise ProcessingError(f"Pass 1 cannot read frame {path.name}.")
    if mask is not None:
        height, width = img.shape[:2]
        x1 = max(0, min(mask.x, width))
        y1 = max(0, min(mask.y, height))
        x2 = max(0, min(mask.x + mask.width, width))
        y2 = max(0, min(mask.y + mask.height, height))
        if x2 > x1 and y2 > y1:
            img[y1:y2, x1:x2] = _PIP_FILL_GRAY
    ok, buf = cv2.imencode(".jpg", img)
    if not ok:
        raise ProcessingError(f"Pass 1 cannot re-encode frame {path.name}.")
    return bytes(buf.tobytes())


def _prep_images(chunk: list[_PF], mask: PiPMask | None) -> list[bytes]:
    """Decode + PiP-mask every frame JPEG in a chunk (blocking cv2 work)."""
    return [_read_masked_jpeg(pf.sampled.frame_path, mask) for pf in chunk]


# ── chunk-response parsing (plain text → N FrameDescription) ─────────────


def _parse_chunk(content: str, chunk: list[_PF]) -> list[FrameDescription]:
    """Split the chunk response into one ``FrameDescription`` per frame.

    Markers delimit the blocks; frames are assigned ordinally with the
    marker doubling as a checksum. A wrong block count or a marker that
    does not match its expected frame raises ``StructuralRetryError`` so
    the router retries once with feedback, then falls back.
    """
    matches = list(_MARKER_RE.finditer(content))
    if len(matches) != len(chunk):
        raise StructuralRetryError(
            f"expected {len(chunk)} [FRAME HH:MM:SS] block(s), got "
            f"{len(matches)}; emit exactly one marked block per frame, in order."
        )

    descriptions: list[FrameDescription] = []
    for i, (pf, match) in enumerate(zip(chunk, matches, strict=True)):
        expected = _format_marker(pf.sampled.frame_position_ms)
        if match.group() != expected:
            raise StructuralRetryError(
                f"block {i + 1} marker mismatch: expected {expected}, got "
                f"{match.group()}; copy each marker verbatim, in order."
            )
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        descriptions.append(
            FrameDescription(
                scene_id=pf.scene_id,
                frame_position_ms=pf.sampled.frame_position_ms,
                description=content[start:end].strip(),
                kind=_kind_at(i, pf.sampled.change_class),
            )
        )
    return descriptions


# ── orchestration ────────────────────────────────────────────────────────


async def _describe_chunk(
    chunk: list[_PF],
    mask: PiPMask | None,
    stage_router: StageRouter,
) -> list[FrameDescription]:
    """One vision call for a chunk → its parsed frame descriptions."""
    images = await asyncio.to_thread(_prep_images, chunk, mask)
    frames_ctx = [
        {
            "marker": _format_marker(pf.sampled.frame_position_ms),
            "kind": _kind_at(i, pf.sampled.change_class).name,
        }
        for i, pf in enumerate(chunk)
    ]

    parsed: dict[str, list[FrameDescription]] = {}

    def _validator(content: str) -> None:
        parsed["frames"] = _parse_chunk(content, chunk)

    await stage_router.execute_for_stage(
        _STAGE_NAME,
        response_validator=_validator,
        contents=images,
        n_frames=len(chunk),
        frames=frames_ctx,
    )
    # ``execute_for_stage`` only returns on a validated success (it raises
    # ``LadderExhaustedError`` otherwise), so the validator has run and
    # populated ``parsed`` — guard defensively against an empty-content path.
    result = parsed.get("frames")
    if result is None:
        raise ProcessingError("Pass 1 chunk produced no parseable description.")
    return result


async def run_pass_1(
    detection_result: DetectionResult,
    file_metadata: VideoFileMetadata,
    *,
    stage_router: StageRouter,
) -> list[FrameDescription]:
    """Krok 4 — describe every kept frame via chunked chain-diff vision calls.

    Chunks are independent (within-chunk chain only) and run with bounded
    concurrency; results are flattened in chunk order so the descriptions
    stay chronological. Any chunk failure (ladder exhausted, persistent
    parse mismatch, unreadable frame) is fail-fast → ``ProcessingError``
    (R1 ``state=ERROR``).
    """
    chunks = _plan_chunks(detection_result.scenes)
    if not chunks:
        return []

    mask = detection_result.pip_mask
    logger.info(
        "video_pass1_start",
        chunks=len(chunks),
        frames=sum(len(c) for c in chunks),
        resolution=file_metadata.resolution,
        pip=mask is not None,
    )

    sem = asyncio.Semaphore(_CHUNK_CONCURRENCY)

    async def _run_one(chunk: list[_PF]) -> list[FrameDescription]:
        async with sem:
            return await _describe_chunk(chunk, mask, stage_router)

    results = await asyncio.gather(
        *(_run_one(chunk) for chunk in chunks), return_exceptions=True
    )

    descriptions: list[FrameDescription] = []
    errors: list[tuple[int, BaseException]] = []
    for idx, result in enumerate(results):
        if isinstance(result, BaseException):
            errors.append((idx, result))
        else:
            descriptions.extend(result)

    if errors:
        first_idx, first_exc = errors[0]
        raise ProcessingError(
            f"Pass 1 failed on chunk {first_idx}: {first_exc}; "
            f"{len(errors)}/{len(chunks)} chunks failed"
        )

    logger.info("video_pass1_done", descriptions=len(descriptions))
    return descriptions
