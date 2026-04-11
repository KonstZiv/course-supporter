"""Hierarchical streaming memory pipeline: instant -> scene -> video.

Three aggregation levels, updated incrementally:

1. **Instant Memory** — per-frame rolling window (no LLM).
   Current frame description + compressed previous frame.
2. **Scene Memory** — per-scene rolling assessment (LLM, per frame).
   What is happening in this scene overall. Updated with each frame.
3. **Video Memory** — running video context (LLM, per scene).
   Short description of the entire video. Updated once per scene.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from course_supporter.vd.rate_limiter import is_rate_limit, retry_wait
from course_supporter.vd.schemas import (
    EyesResult,
    InstantMemory,
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


def _load_template(name: str) -> str:
    if name not in _PROMPT_CACHE:
        _PROMPT_CACHE[name] = (_PROMPTS_DIR / name).read_text()
    return _PROMPT_CACHE[name]


# ---------------------------------------------------------------------------
# Instant Memory (no LLM)
# ---------------------------------------------------------------------------

_MAX_PREVIOUS_LEN = 300
_DELTA_ACCUMULATE_THRESHOLD = 100  # chars of accumulated delta before LLM update
_DELTA_COUNT_THRESHOLD = 5  # max consecutive deltas before LLM update


def _compress_description(text: str, max_len: int = _MAX_PREVIOUS_LEN) -> str:
    """Compress a frame description to a short summary."""
    # Take the first meaningful lines up to max_len
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    result: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) > max_len:
            break
        result.append(line)
        total += len(line)
    return " ".join(result) if result else text[:max_len]


def append_delta_to_scene(
    scene_memory: SceneMemory,
    delta_text: str,
) -> SceneMemory:
    """Mechanically append delta description to scene memory (no LLM).

    Adds the delta text to the summary and increments frames_seen.
    Used when the delta is too short to justify an LLM call.
    """
    separator = " | " if scene_memory.summary else ""
    return SceneMemory(
        scene_id=scene_memory.scene_id,
        summary=scene_memory.summary + separator + delta_text,
        scene_type=scene_memory.scene_type,
        topics=scene_memory.topics,
        importance=scene_memory.importance,
        frames_seen=scene_memory.frames_seen + 1,
        previous_scene_summary=scene_memory.previous_scene_summary,
    )


def needs_llm_scene_update(
    instant: InstantMemory,
    delta_chars_accumulated: int,
    deltas_since_llm: int,
) -> bool:
    """Decide if scene memory needs an LLM update or mechanical append.

    Returns True (LLM needed) when:
    - Frame is not a delta (full description), OR
    - Accumulated delta text exceeds threshold, OR
    - Too many consecutive deltas without LLM update.
    """
    if not instant.is_delta:
        return True
    new_total = delta_chars_accumulated + len(instant.current)
    if new_total > _DELTA_ACCUMULATE_THRESHOLD:
        return True
    return deltas_since_llm + 1 >= _DELTA_COUNT_THRESHOLD


def build_instant_memory(
    eyes_result: EyesResult,
    previous_instant: InstantMemory | None,
) -> InstantMemory:
    """Build instant memory for a single frame.

    Rolling 2-frame window: current description + compressed previous.
    """
    previous = ""
    if previous_instant is not None:
        previous = _compress_description(previous_instant.current)

    return InstantMemory(
        frame_id=eyes_result.frame_id,
        scene_id=eyes_result.scene_id,
        timestamp_sec=eyes_result.timestamp_sec,
        current=eyes_result.description,
        previous=previous,
        is_delta=eyes_result.is_delta,
    )


# ---------------------------------------------------------------------------
# MemoryPipeline (LLM-based scene + video memory)
# ---------------------------------------------------------------------------


class MemoryPipeline:
    """Streaming memory: scene updates per frame, video updates per scene.

    Routes all LLM calls through ``ModelRouter`` for unified cost
    tracking, fallback chains, and tenant attribution. Rate limiting
    is enforced by a shared ``VDRateLimiter``.

    Args:
        router: ModelRouter for LLM calls.
        rate_limiter: Shared VD rate limiter.
    """

    def __init__(
        self,
        router: ModelRouter,
        rate_limiter: VDRateLimiter,
    ) -> None:
        self._router = router
        self._rate_limiter = rate_limiter

    async def update_scene_memory(
        self,
        instant: InstantMemory,
        scene_memory: SceneMemory,
    ) -> SceneMemory:
        """Update scene memory with a new frame observation.

        Called once per frame within a scene. LLM synthesizes
        what is happening in this scene overall, not just this frame.
        """
        template = _load_template("scene_memory_streaming.txt")
        prompt = template.format(
            scene_id=instant.scene_id,
            frame_id=instant.frame_id,
            timestamp=instant.timestamp_sec,
            current_frame=instant.current,
            previous_frame=instant.previous or "(first frame)",
            is_delta=instant.is_delta,
            existing_scene_memory=scene_memory.summary or "(first frame in scene)",
            frames_seen=scene_memory.frames_seen,
            previous_scene=scene_memory.previous_scene_summary
            or "(first scene in video)",
        )

        raw = await self._call_llm(prompt)
        return self._parse_scene_memory(
            scene_id=instant.scene_id,
            raw=raw,
            frames_seen=scene_memory.frames_seen + 1,
            previous_scene_summary=scene_memory.previous_scene_summary,
        )

    async def update_video_memory(
        self,
        scene_memory: SceneMemory,
        video_memory: VideoMemory,
        previous_scene: SceneMemory | None,
    ) -> VideoMemory:
        """Update video memory after a scene completes.

        Called once per scene. LLM updates the running video
        description based on the completed scene.
        """
        prev_scene_text = ""
        if previous_scene is not None:
            prev_scene_text = (
                f"Previous scene (scene {previous_scene.scene_id}): "
                f"{previous_scene.summary}"
            )

        template = _load_template("video_memory.txt")
        prompt = template.format(
            current_video_memory=video_memory.text or "(beginning of video)",
            scene_id=scene_memory.scene_id,
            scene_summary=scene_memory.summary,
            scene_type=scene_memory.scene_type,
            topics=", ".join(scene_memory.topics) if scene_memory.topics else "N/A",
            previous_scene=prev_scene_text or "(first scene)",
        )

        new_text = await self._call_llm(prompt)
        return VideoMemory(
            text=new_text,
            scenes_processed=video_memory.scenes_processed + 1,
        )

    async def process_scene(
        self,
        scene: Scene,
        eyes_results: list[EyesResult],
        video_memory: VideoMemory,
        previous_scene: SceneMemory | None = None,
    ) -> tuple[SceneMemory, VideoMemory]:
        """Process an entire scene: update scene memory per frame, then video.

        Convenience method that runs the full streaming loop for one scene.

        Returns:
            Tuple of (final scene_memory, updated video_memory).
        """
        scene_memory = SceneMemory(
            scene_id=scene.scene_id,
            previous_scene_summary=(
                _compress_description(previous_scene.summary) if previous_scene else ""
            ),
        )
        instant: InstantMemory | None = None

        for eyes in eyes_results:
            instant = build_instant_memory(eyes, instant)
            scene_memory = await self.update_scene_memory(instant, scene_memory)

        updated_video = await self.update_video_memory(
            scene_memory,
            video_memory,
            previous_scene,
        )

        return scene_memory, updated_video

    async def _call_llm(
        self,
        prompt: str,
        *,
        _max_retries: int = 3,
    ) -> str:
        """Rate-limited text LLM call via ModelRouter with retry on 429."""
        for attempt in range(_max_retries + 1):
            await self._rate_limiter.acquire()
            try:
                response = await self._router.complete(
                    action="vd_memory",
                    prompt=prompt,
                )
                logger.info(
                    "memory_llm_call",
                    model=response.model_id,
                    latency_ms=response.latency_ms,
                    tokens_in=response.tokens_in,
                    tokens_out=response.tokens_out,
                )
                return response.content
            except Exception as exc:
                if attempt < _max_retries and is_rate_limit(exc):
                    wait = retry_wait(exc, attempt)
                    logger.warning(
                        "memory_rate_limit_retry",
                        attempt=attempt + 1,
                        wait_sec=wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.exception("memory_llm_error")
                raise

        msg = "Unreachable: retry loop exhausted without return or raise"
        raise RuntimeError(msg)

    @staticmethod
    def _parse_scene_memory(
        scene_id: int,
        raw: str,
        frames_seen: int,
        previous_scene_summary: str,
    ) -> SceneMemory:
        """Parse LLM response into SceneMemory.

        Expects JSON (possibly wrapped in ```json ... ```).
        Falls back to raw text on parse failure.
        """
        inner = raw.strip()
        if inner.startswith("```"):
            lines = inner.splitlines()[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            inner = "\n".join(lines)

        try:
            data: dict[str, Any] = json.loads(inner)
            return SceneMemory(
                scene_id=scene_id,
                summary=data.get("summary", ""),
                scene_type=data.get("scene_type", "unknown"),
                topics=data.get("topics", []),
                importance=min(max(data.get("importance", 3), 1), 5),
                frames_seen=frames_seen,
                previous_scene_summary=previous_scene_summary,
            )
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "scene_memory_parse_failed",
                scene_id=scene_id,
            )
            return SceneMemory(
                scene_id=scene_id,
                summary=raw[:200],
                scene_type="unknown",
                frames_seen=frames_seen,
                previous_scene_summary=previous_scene_summary,
            )
