"""Pydantic schemas for the Visual Description (VD) pipeline.

Covers three stages:
- Stage A: Frame sampling, deduplication, scene segmentation.
- Stage B: Per-frame Vision LLM analysis, hierarchical memory.
- Cross-modal alignment (ingestion-level, consumed by ``ingestion/alignment.py``).

All field names and semantics are derived from VD spike JSON structures
(``VD-SPIKE-B/pipeline/``).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Stage A: Frame Sampling
# ---------------------------------------------------------------------------


class FrameSource(StrEnum):
    """Origin of a sampled frame."""

    GOLDEN = "golden"
    GAP_FILL = "gap_fill"


class ChangeClass(StrEnum):
    """Visual-change classification relative to the previous frame."""

    FIRST = "first"
    BOUNDARY = "boundary"
    MEDIUM = "medium"
    LOW = "low"


class SampledFrame(BaseModel):
    """Single sampled video frame with perceptual-hash metadata."""

    frame_id: str = Field(description="Unique ID, e.g. 'golden_006_92s'.")
    filename: str = Field(description="JPEG filename on disk.")
    timestamp_sec: float = Field(ge=0.0)
    scene_id: int = Field(ge=0, description="Assigned during scene segmentation.")
    source: FrameSource
    dhash: str = Field(description="Hex-encoded dHash (hash_size=16 -> 64 hex chars).")
    dhash_dist: float = Field(
        ge=0.0,
        le=1.0,
        description="Normalised Hamming distance from previous frame.",
    )
    time_gap: float = Field(ge=0.0, description="Seconds since previous frame.")
    change_class: ChangeClass

    @property
    def is_gap_fill(self) -> bool:
        """Whether this frame was inserted to fill a coverage gap."""
        return self.source == FrameSource.GAP_FILL


class Scene(BaseModel):
    """Contiguous group of frames sharing a visual context."""

    scene_id: int = Field(ge=0)
    frame_ids: list[str]
    start_sec: float = Field(ge=0.0)
    end_sec: float = Field(ge=0.0)

    @property
    def duration_sec(self) -> float:
        """Scene duration in seconds."""
        return self.end_sec - self.start_sec


class PiPMask(BaseModel):
    """Picture-in-Picture overlay bounding box detected via temporal diff."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    confidence: float = Field(ge=0.0, le=1.0)


class SamplingParams(BaseModel):
    """Parameters for frame extraction and multi-metric deduplication.

    Dedup uses a tiered voting system with 5 metrics:
    dHash, pixel_diff, color_histogram, SSIM, edge_diff.

    - Tier 1: strong signal from any single metric -> keep frame.
    - Tier 2: ``min_votes`` out of 5 metrics vote "changed" -> keep.
    """

    fps: float = 0.5
    hash_size: int = 16
    gap_fill_max_sec: float = 15.0
    scene_boundary_dhash: float = 0.20
    scene_boundary_color_hist: float = Field(
        default=0.20,
        description=(
            "Color histogram distance required to confirm a dHash-based "
            "scene boundary. Prevents motion (rotation, gestures) from "
            "creating false boundaries."
        ),
    )
    scene_boundary_flow_coherence: float = Field(
        default=0.6,
        description=(
            "If optical flow coherence exceeds this, the visual change "
            "is coherent motion (rotation, scroll) — not a new scene."
        ),
    )
    scene_boundary_time_gap: float = 10.0

    # Tier 1: unconditional keep (obvious scene change)
    tier1_dhash: float = Field(
        default=0.15,
        description="dHash dist above this = Tier 1.",
    )
    tier1_pixel_diff: float = Field(
        default=0.10,
        description="Pixel diff above this = Tier 1.",
    )

    # Tier 2: per-metric vote thresholds
    vote_dhash: float = 0.03
    vote_pixel_diff: float = 0.02
    vote_color_hist: float = 0.15
    vote_ssim: float = Field(default=0.95, description="SSIM below this = vote.")
    vote_edge_diff: float = 0.05
    min_votes: int = Field(default=2, ge=1, le=5)

    # Noise floor for pixel diff (intensity levels 0-255)
    pixel_noise_floor: int = Field(default=25, ge=1, le=128)


class FrameSamplingResult(BaseModel):
    """Output of the frame-sampling stage (Stage A)."""

    frames: list[SampledFrame]
    scenes: list[Scene]
    pip_mask: PiPMask | None = None
    video_resolution: tuple[int, int] | None = None
    sampling_params: SamplingParams = Field(default_factory=SamplingParams)
    complete: bool = False


# ---------------------------------------------------------------------------
# Stage B: Visual Analysis (Eyes + Hierarchical Memory)
# ---------------------------------------------------------------------------


class DeltaStrategy(StrEnum):
    """How to handle frames similar to the previous one."""

    NONE = "none"
    EXPLICIT = "explicit"
    CONDITIONAL = "conditional"


class EyesResult(BaseModel):
    """Per-frame Vision LLM response (prompt v3, Markdown output)."""

    frame_id: str
    timestamp_sec: float = Field(ge=0.0)
    scene_id: int = Field(ge=0)
    response: str = Field(description="Raw Markdown from Vision LLM.")
    n_images: int = Field(
        ge=1,
        le=3,
        description="1 (main) + up to 2 context frames.",
    )
    latency_sec: float = Field(ge=0.0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    description: str = Field(
        default="",
        description="First ~300 chars of response (used as scene context).",
    )
    scene_type: str = Field(
        default="",
        description="Extracted 'Setting' value from prompt v3 response.",
    )
    is_delta: bool = Field(
        default=False,
        description="True if response describes only changes from base frame.",
    )
    base_frame_id: str | None = Field(
        default=None,
        description="Frame ID of the base (full) description for delta.",
    )
    importance: int = Field(default=3, ge=1, le=5)


class MergeMethod(StrEnum):
    """Strategy used to combine frame-level texts within a scene."""

    SINGLE = "single"
    CODE_DIFF = "code_diff"
    LLM_MERGE = "llm_merge"
    CODE_DIFF_FALLBACK = "code_diff_fallback"


class InstantMemory(BaseModel):
    """Per-scene merged text extraction (code-based or LLM merge)."""

    scene_id: int = Field(ge=0)
    merged_text: str
    frame_count: int = Field(ge=1)
    method: MergeMethod


class SceneMemory(BaseModel):
    """Per-scene LLM-generated summary and structured metadata."""

    scene_id: int = Field(ge=0)
    scene_type: str
    summary: str
    complete_text: str = ""
    topics: list[str] = Field(default_factory=list)
    importance: int = Field(default=3, ge=1, le=5)


class CourseMemorySnapshot(BaseModel):
    """Course-level context at a specific point in processing."""

    scene_id: int = Field(ge=0)
    context_snapshot: str


class CourseMemory(BaseModel):
    """Running course-level context (<=200 words), updated per scene."""

    text: str = Field(
        default="",
        description="Current course context fed back into Eyes prompt.",
    )
    scenes_processed: int = Field(default=0, ge=0)
    history: list[CourseMemorySnapshot] = Field(default_factory=list)


class SceneAnalysis(BaseModel):
    """Combined analysis for a single scene."""

    scene: Scene
    eyes_results: list[EyesResult]
    instant_memory: InstantMemory
    scene_memory: SceneMemory


class VDResult(BaseModel):
    """Final output of the VD pipeline (Stages A + B)."""

    scenes: list[SceneAnalysis]
    course_memory: CourseMemory
    frames_total: int = Field(ge=0)
    frames_analyzed: int = Field(ge=0)
    model: str = Field(description="Vision LLM model ID used.")


# ---------------------------------------------------------------------------
# Cross-modal Alignment (ingestion level — NOT part of VD module)
# ---------------------------------------------------------------------------


class AlignedSegment(BaseModel):
    """Single temporally-aligned segment combining STT and VD data."""

    start_sec: float = Field(ge=0.0)
    end_sec: float = Field(ge=0.0)
    stt_text: str | None = None
    vd_scene_id: int | None = None
    vd_summary: str | None = None
    semantic_overlap: float = Field(default=0.0, ge=0.0, le=1.0)
    conflicts: list[str] = Field(default_factory=list)
    alignment_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class CoverageGap(BaseModel):
    """Time interval not covered by either STT or VD."""

    start_sec: float = Field(ge=0.0)
    end_sec: float = Field(ge=0.0)
    gap_type: str = Field(description="'stt_only', 'vd_only', or 'neither'.")


class AlignmentReport(BaseModel):
    """Summary report of cross-modal alignment quality."""

    segments: list[AlignedSegment] = Field(default_factory=list)
    coverage_gaps: list[CoverageGap] = Field(default_factory=list)
    vd_orphans: list[int] = Field(
        default_factory=list,
        description="Scene IDs with no matching STT segment.",
    )
    stt_orphans: list[int] = Field(
        default_factory=list,
        description="STT segment indices with no matching VD scene.",
    )
    conflicts: list[str] = Field(default_factory=list)
    semantic_coverage: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of segments with semantic overlap > 0.",
    )
