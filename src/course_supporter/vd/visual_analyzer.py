"""Per-frame Vision LLM analysis (Eyes step).

Sends each frame (with up to 2 context images) to a Vision LLM and
parses the Markdown response into an ``EyesResult``.  Uses prompt v3
(language-agnostic, Scene Composition + Elements).

Supports three delta strategies for similar frames:
- ``NONE``: always full description (baseline).
- ``EXPLICIT``: we decide based on ``change_class`` — low/medium
  changes get a delta prompt (variant A).
- ``CONDITIONAL``: LLM decides — prompt includes previous description
  and LLM chooses full vs delta (variant B).

All LLM calls are routed through ``ModelRouter`` for unified cost
tracking, fallback chains, and tenant attribution. Rate limiting
is enforced by a shared ``VDRateLimiter``.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from course_supporter.vd.memory_pipeline import (
    MemoryPipeline,
    append_delta_to_scene,
    build_instant_memory,
    needs_llm_scene_update,
)
from course_supporter.vd.rate_limiter import is_rate_limit, retry_wait
from course_supporter.vd.schemas import (
    ChangeClass,
    DeltaStrategy,
    EyesResult,
    InstantMemory,
    SampledFrame,
    Scene,
    SceneMemory,
    VideoMemory,
)

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.vd.rate_limiter import VDRateLimiter

logger = structlog.get_logger()

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_PROMPT_CACHE: dict[str, str] = {}
# Max chars for the visual description extracted from Vision LLM response.
MAX_VISUAL_DESCRIPTION_LEN = 300


def _load_prompt(name: str) -> str:
    """Load and cache a prompt template by filename."""
    if name not in _PROMPT_CACHE:
        _PROMPT_CACHE[name] = (_PROMPTS_DIR / name).read_text()
    return _PROMPT_CACHE[name]


def _parse_response(text: str) -> tuple[str, str, int]:
    """Extract (description, scene_type, importance) from LLM response.

    Handles both full Markdown responses and delta/conditional responses.

    Returns:
        Tuple of (description_300chars, scene_type, importance).
    """
    description = ""
    scene_type = ""
    importance = 3

    for line in text.splitlines():
        if line.startswith("**Setting:**"):
            scene_type = line.split("**Setting:**", 1)[-1].strip()
            break

    # Collect Scene Composition section as description
    in_comp = False
    desc_parts: list[str] = []
    for line in text.splitlines():
        if "## Scene Composition" in line:
            in_comp = True
            continue
        if in_comp and line.startswith("## "):
            break
        if in_comp and line.strip():
            desc_parts.append(line.strip())

    description = " ".join(desc_parts[:3]) if desc_parts else text[:200]

    return description[:MAX_VISUAL_DESCRIPTION_LEN], scene_type, importance


def _is_conditional_delta(text: str) -> bool:
    """Check if a conditional-mode response chose delta format."""
    stripped = text.strip()
    return stripped.upper().startswith("CHANGES ONLY:")


_CHANGE_CLASS_LABELS: dict[ChangeClass, str] = {
    ChangeClass.FIRST: "FIRST frame",
    ChangeClass.BOUNDARY: "HIGH change",
    ChangeClass.MEDIUM: "MEDIUM change",
    ChangeClass.LOW: "LOW change",
}


def _build_similarity_hint(frame: SampledFrame | None) -> str:
    """Build a pixel-level similarity hint for the conditional prompt.

    Returns an empty string when no frame metadata is available
    (e.g. first frame or NONE strategy).
    """
    if frame is None:
        return ""
    similarity_pct = round((1.0 - frame.dhash_dist) * 100)
    label = _CHANGE_CLASS_LABELS.get(frame.change_class, frame.change_class.value)
    return (
        f"Pixel-level similarity to previous frame: {similarity_pct}% ({label}). "
        "Note: this metric can be noisy — minor object movement, camera shake, "
        "or animation may inflate the change value. Use your own visual judgement "
        "as the primary signal.\n\n"
    )


def _build_memory_context(
    instant: InstantMemory | None,
    scene_memory: SceneMemory,
    video_memory: VideoMemory,
) -> str:
    """Build a context block from all three memory levels.

    Formatted as a single block for the Eyes prompt.
    Returns empty string when no memory data is available.
    """
    parts: list[str] = []

    if video_memory.text:
        parts.append(f"Video context:\n{video_memory.text}")

    if scene_memory.summary:
        parts.append(f"Current scene so far:\n{scene_memory.summary}")

    if instant is not None and instant.previous:
        parts.append(f"Previous frame:\n{instant.previous}")

    if not parts:
        return ""
    return "\n\n".join(parts) + "\n\n"


class VisualAnalyzer:
    """Per-frame Vision LLM analysis with context images and memory.

    Routes all LLM calls through ``ModelRouter`` for unified cost
    tracking and fallback chains. Rate limiting is enforced by a
    shared ``VDRateLimiter``.

    Args:
        router: ModelRouter for LLM calls.
        rate_limiter: Shared VD rate limiter.
        model: Vision LLM model identifier (for metadata/logging).
        context_max_gap_sec: Max time gap for context images.
        max_context_images: Max number of previous frames as context.
        delta_strategy: How to handle frames similar to previous.
        memory: Optional MemoryPipeline for streaming memory updates.
    """

    def __init__(
        self,
        router: ModelRouter,
        rate_limiter: VDRateLimiter,
        *,
        model: str = "gemini-2.5-flash",
        context_max_gap_sec: float = 7.0,
        max_context_images: int = 2,
        delta_strategy: DeltaStrategy = DeltaStrategy.CONDITIONAL,
        memory: MemoryPipeline | None = None,
    ) -> None:
        self._router = router
        self._rate_limiter = rate_limiter
        self._model = model
        self._context_max_gap = context_max_gap_sec
        self._max_ctx_images = max_context_images
        self._delta_strategy = delta_strategy
        self._memory = memory

    @property
    def model(self) -> str:
        """Vision LLM model identifier."""
        return self._model

    async def analyze_scene(
        self,
        scene: Scene,
        frames: list[SampledFrame],
        frame_dir: Path,
        *,
        video_memory: VideoMemory | None = None,
        scene_memory: SceneMemory | None = None,
    ) -> tuple[list[EyesResult], SceneMemory | None]:
        """Analyze all frames in a scene with streaming memory.

        Args:
            scene: Scene metadata.
            frames: Frames belonging to this scene (ordered by time).
            frame_dir: Directory containing frame JPEG files.
            video_memory: Current video-level context.
            scene_memory: Initial scene memory (carries previous scene info).

        Returns:
            Tuple of (eyes_results, final_scene_memory).
            scene_memory is None when no MemoryPipeline is injected.
        """
        results: list[EyesResult] = []
        instant: InstantMemory | None = None
        current_scene = scene_memory or SceneMemory(scene_id=scene.scene_id)
        current_video = video_memory or VideoMemory()
        delta_chars_accumulated = 0
        deltas_since_llm = 0

        for i, frame in enumerate(frames):
            # Select context images: previous frames within time gap
            ctx_frames: list[SampledFrame] = []
            for j in range(max(0, i - self._max_ctx_images), i):
                gap = frame.timestamp_sec - frames[j].timestamp_sec
                if 0 < gap <= self._context_max_gap:
                    ctx_frames.append(frames[j])

            # Determine if this frame should use delta mode
            prev_result = results[-1] if results else None
            use_delta = self._should_use_delta(frame, prev_result)

            result = await self._analyze_frame(
                frame=frame,
                context_frames=ctx_frames,
                frame_dir=frame_dir,
                instant=instant,
                scene_memory=current_scene,
                video_memory=current_video,
                prev_result=prev_result if use_delta else None,
            )
            results.append(result)

            # Update memory after each frame
            instant = build_instant_memory(result, instant)
            if self._memory is not None:
                if needs_llm_scene_update(
                    instant, delta_chars_accumulated, deltas_since_llm
                ):
                    current_scene = await self._memory.update_scene_memory(
                        instant,
                        current_scene,
                    )
                    delta_chars_accumulated = 0
                    deltas_since_llm = 0
                else:
                    current_scene = append_delta_to_scene(
                        current_scene,
                        instant.current,
                    )
                    delta_chars_accumulated += len(instant.current)
                    deltas_since_llm += 1

        return results, current_scene if self._memory is not None else None

    def _should_use_delta(
        self,
        frame: SampledFrame,
        prev_result: EyesResult | None,
    ) -> bool:
        """Decide whether to use delta mode for this frame."""
        if prev_result is None:
            return False

        if self._delta_strategy == DeltaStrategy.NONE:
            return False

        if self._delta_strategy == DeltaStrategy.CONDITIONAL:
            # Always provide previous description; LLM decides
            return True

        # EXPLICIT: we decide based on change_class
        # FIRST and BOUNDARY always get full description
        # LOW and MEDIUM get delta
        return frame.change_class in (ChangeClass.LOW, ChangeClass.MEDIUM)

    async def _analyze_frame(
        self,
        frame: SampledFrame,
        context_frames: list[SampledFrame],
        frame_dir: Path,
        instant: InstantMemory | None,
        scene_memory: SceneMemory,
        video_memory: VideoMemory,
        prev_result: EyesResult | None,
    ) -> EyesResult:
        """Analyze a single frame with Vision LLM."""
        from google.genai import types as genai_types

        parts: list[Any] = []

        # Context images (previous frames, oldest first)
        for cf in context_frames:
            img_path = self._find_frame(frame_dir, cf.filename)
            parts.append(
                genai_types.Part.from_bytes(
                    data=img_path.read_bytes(),
                    mime_type="image/jpeg",
                ),
            )

        # Main image (current frame — LAST image per prompt)
        main_path = self._find_frame(frame_dir, frame.filename)
        parts.append(
            genai_types.Part.from_bytes(
                data=main_path.read_bytes(),
                mime_type="image/jpeg",
            ),
        )

        # Build prompt text based on strategy
        prompt = self._build_prompt(
            instant=instant,
            scene_memory=scene_memory,
            video_memory=video_memory,
            prev_result=prev_result,
            frame=frame,
        )
        parts.append(genai_types.Part.from_text(text=prompt))

        # Rate-limited API call via ModelRouter
        api_result = await self._call_llm(parts)

        # Parse response
        response_text = api_result["text"]
        description, scene_type, importance = _parse_response(response_text)

        # Determine if response is actually a delta
        is_delta = False
        base_frame_id: str | None = None
        if prev_result is not None:
            if self._delta_strategy == DeltaStrategy.EXPLICIT:
                is_delta = True
                base_frame_id = prev_result.frame_id
            elif self._delta_strategy == DeltaStrategy.CONDITIONAL:
                if _is_conditional_delta(response_text):
                    is_delta = True
                    base_frame_id = prev_result.frame_id

        return EyesResult(
            frame_id=frame.frame_id,
            timestamp_sec=frame.timestamp_sec,
            scene_id=frame.scene_id,
            response=response_text,
            n_images=len(context_frames) + 1,
            latency_sec=api_result["latency_sec"],
            input_tokens=api_result["input_tokens"],
            output_tokens=api_result["output_tokens"],
            description=description,
            scene_type=scene_type,
            importance=importance,
            is_delta=is_delta,
            base_frame_id=base_frame_id,
        )

    async def _call_llm(
        self,
        parts: list[Any],
        *,
        _max_retries: int = 3,
    ) -> dict[str, Any]:
        """Rate-limited Vision LLM call via ModelRouter with retry on 429."""
        from google.genai import types as genai_types

        contents: list[Any] = [genai_types.Content(parts=parts)]

        for attempt in range(_max_retries + 1):
            await self._rate_limiter.acquire()
            t0 = time.monotonic()
            try:
                response = await self._router.complete(
                    action="vd_frame_analysis",
                    prompt="",
                    contents=contents,
                )
                latency = round(time.monotonic() - t0, 2)
                logger.info(
                    "eyes_frame_done",
                    model=response.model_id,
                    latency=latency,
                    tokens_in=response.tokens_in,
                    tokens_out=response.tokens_out,
                )
                return {
                    "text": response.content,
                    "input_tokens": response.tokens_in or 0,
                    "output_tokens": response.tokens_out or 0,
                    "latency_sec": latency,
                }
            except Exception as exc:
                if attempt < _max_retries and is_rate_limit(exc):
                    wait = retry_wait(exc, attempt)
                    logger.warning(
                        "eyes_rate_limit_retry",
                        attempt=attempt + 1,
                        wait_sec=wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.exception(
                    "vision_llm_error",
                    error_type=type(exc).__name__,
                )
                raise

        msg = "Unreachable: retry loop exhausted without return or raise"
        raise RuntimeError(msg)

    def _build_prompt(
        self,
        *,
        instant: InstantMemory | None,
        scene_memory: SceneMemory,
        video_memory: VideoMemory,
        prev_result: EyesResult | None,
        frame: SampledFrame | None = None,
    ) -> str:
        """Build the appropriate prompt based on delta strategy and memory."""
        context_block = _build_memory_context(instant, scene_memory, video_memory)

        # Choose template based on strategy and whether we have prev
        if prev_result is not None:
            if self._delta_strategy == DeltaStrategy.EXPLICIT:
                template = _load_prompt("eyes_v3_delta.txt")
                return template.format(
                    previous_description=prev_result.response[:2000],
                    context_block=context_block,
                )
            if self._delta_strategy == DeltaStrategy.CONDITIONAL:
                similarity_hint = _build_similarity_hint(frame)
                template = _load_prompt("eyes_v3_conditional.txt")
                return template.format(
                    previous_description=prev_result.response[:2000],
                    similarity_hint=similarity_hint,
                    context_block=context_block,
                )

        # Full description (NONE strategy or first frame)
        template = _load_prompt("eyes_v3.txt")
        return template.format(
            context_block=context_block,
        )

    @staticmethod
    def _find_frame(frame_dir: Path, filename: str) -> Path:
        """Locate a frame file in frame_dir or its subdirectories."""
        direct = frame_dir / filename
        if direct.exists():
            return direct
        for sub in frame_dir.iterdir():
            if sub.is_dir():
                candidate = sub / filename
                if candidate.exists():
                    return candidate
        msg = f"Frame not found: {filename} in {frame_dir}"
        raise FileNotFoundError(msg)
