"""Mock data-structure shapes for the video pipeline skeleton (Phase 2.4).

These carriers mirror the *forms* of the per-step structures described in
``PHASE-2-4.md`` §1 (Krok 1-4 intermediate state). The skeleton (task
2.4.1) threads them in-memory between gnízda — no DB / Redis / external
calls. Tasks 2.4.2-2.4.7 replace the stub bodies in :mod:`steps` with
real logic; the shapes here are deliberately minimal and may be widened
(or moved to the proper inter-stage transport — Redis per the audio
precedent — when a real, large transient payload motivates it).

Pass 2a/2b/2c (Krok 5-7) reuse the existing pipeline carriers
``DocumentSummaryDraft`` / ``DocumentSegmentDraft`` rather than redefining
them here.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class VideoFileMetadata(BaseModel):
    """Krok 1 — container metadata probed from the uploaded video (§1).

    Real probe (ffprobe) lands in task 2.4.2; the skeleton returns a
    fixed mock shape without decoding the container.
    """

    duration_ms: int
    codec: str
    resolution: str


class SttWord(BaseModel):
    """Single word-level STT token with millisecond timing (§1 Krok 2)."""

    text: str
    start_ms: int
    end_ms: int


class SttPause(BaseModel):
    """Silence boundary candidate used for Pass 2a alignment (KD2c, §1)."""

    start_ms: int
    end_ms: int


class SttResult(BaseModel):
    """Krok 2 — STT output (ElevenLabs Scribe in the real pipeline, §1).

    ``words`` is a word-level stream (not a flat string) so downstream
    Pass 2a/2b can align segment boundaries to ``pauses``.
    """

    language: str
    duration_ms: int
    words: list[SttWord] = Field(default_factory=list)
    pauses: list[SttPause] = Field(default_factory=list)


class ChangeClass(StrEnum):
    """Frame change classification (§1 Krok 3).

    Drives both scene boundaries (``BOUNDARY`` = new scene start) and the
    Pass 1 anchor/diff decision (``FIRST``/``BOUNDARY`` → anchor).
    """

    FIRST = "first"
    BOUNDARY = "boundary"
    MEDIUM = "medium"
    LOW = "low"


class SampledFrame(BaseModel):
    """One kept frame after dedup, tagged with its change class (§1 Krok 3)."""

    frame_position_ms: int
    change_class: ChangeClass


class Scene(BaseModel):
    """Krok 3 — a detected scene spanning ``[start_ms, end_ms]`` (§1).

    Real detection (ffmpeg + scenedetect, 3-gate) lands in task 2.4.3.
    """

    scene_id: int
    start_ms: int
    end_ms: int
    frames: list[SampledFrame] = Field(default_factory=list)


class FrameKind(StrEnum):
    """Pass 1 description mode — full anchor vs incremental diff (§1 Krok 4)."""

    ANCHOR = "anchor"
    DIFF = "diff"


class FrameDescription(BaseModel):
    """Krok 4 — vision-LLM textual description bound to a timestamp (§1).

    Real Pass 1 chain-diff (vision ladder, Qwen3-VL rung 1) lands in
    task 2.4.4.
    """

    scene_id: int
    frame_position_ms: int
    description: str
    kind: FrameKind
