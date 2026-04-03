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
import time
from pathlib import Path
from typing import Any

import structlog

from course_supporter.key_pool import KeyPool
from course_supporter.vd.schemas import (
    EyesResult,
    InstantMemory,
    Scene,
    SceneMemory,
    VideoMemory,
)

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

    Args:
        key_pool: Gemini API key pool for LLM calls.
        model: Text LLM model identifier.
        fallback_model: Model to try on 429 rate limit.
        rpm_per_key: Requests per minute per API key.
    """

    def __init__(
        self,
        key_pool: KeyPool,
        *,
        model: str = "gemini-2.5-flash",
        fallback_model: str = "gemini-3.1-flash-lite-preview",
        rpm_per_key: int = 10,
    ) -> None:
        self._key_pool = key_pool
        self._model = model
        self._fallback_model = fallback_model
        total_rpm = rpm_per_key * len(key_pool)
        self._min_interval = 60.0 / total_rpm
        self._last_call_time = 0.0
        self._lock = asyncio.Lock()

    async def update_scene_memory(
        self,
        instant: InstantMemory,
        scene_memory: SceneMemory,
    ) -> SceneMemory:
        """Update scene memory with a new frame observation.

        Called once per frame within a scene. LLM synthesizes
        what is happening in the scene overall, not just this frame.
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
        """Rate-limited text LLM call with retry and model fallback."""
        import google.genai as genai

        from course_supporter.vd.visual_analyzer import _is_rate_limit, _retry_wait

        for attempt in range(_max_retries + 1):
            use_model = self._model if attempt == 0 else self._fallback_model

            async with self._lock:
                elapsed = time.monotonic() - self._last_call_time
                if elapsed < self._min_interval:
                    await asyncio.sleep(self._min_interval - elapsed)

                key = self._key_pool.next_key()
                client = genai.Client(api_key=key.get_secret_value())

                t0 = time.monotonic()
                try:
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=use_model,
                        contents=prompt,
                    )
                except Exception as exc:
                    self._last_call_time = time.monotonic()
                    if attempt < _max_retries and _is_rate_limit(exc):
                        wait = _retry_wait(exc, attempt)
                        logger.warning(
                            "memory_rate_limit_retry",
                            attempt=attempt + 1,
                            wait_sec=wait,
                            fallback_model=self._fallback_model,
                        )
                        await asyncio.sleep(wait)
                        continue
                    logger.exception(
                        "memory_llm_error",
                        model=use_model,
                    )
                    raise
                self._last_call_time = time.monotonic()

            logger.info(
                "memory_llm_call",
                model=use_model,
                latency=round(time.monotonic() - t0, 2),
            )
            return response.text or ""

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
