"""Hierarchical memory pipeline: instant -> scene -> course.

Three aggregation levels for VD pipeline output:

1. **Instant Memory** — per-scene code-based text merge (no LLM).
   Falls back to LLM merge when overlap detection fails.
2. **Scene Memory** — per-scene LLM synthesis (type, summary, topics).
3. **Course Memory** — running course context (<=200 words), updated
   per scene, fed back into Eyes prompt for next scene.
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
    CourseMemory,
    CourseMemorySnapshot,
    EyesResult,
    InstantMemory,
    MergeMethod,
    Scene,
    SceneMemory,
)

logger = structlog.get_logger()

_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_template(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text()


# ---------------------------------------------------------------------------
# Text extraction and code-based merge
# ---------------------------------------------------------------------------


def _extract_element_text(response: str) -> str:
    """Extract text content from Eyes Markdown response.

    Collects all content from ``## Element N`` sections.
    """
    lines: list[str] = []
    in_element = False
    for line in response.splitlines():
        if line.startswith("## Element"):
            in_element = True
            continue
        if line.startswith("## ") and in_element:
            if "Element" not in line:
                in_element = False
                continue
            continue
        if in_element:
            lines.append(line)

    if lines:
        return "\n".join(lines)
    return response


def _merge_code_texts(base: str, new: str) -> str:
    """Merge two code text extractions by finding line overlap.

    Looks for the longest suffix of *base* that matches a prefix of
    *new* (line-by-line, stripped), then concatenates without the
    duplicate region.  If no overlap is found, joins with a separator.
    """
    base_lines = base.strip().splitlines()
    new_lines = new.strip().splitlines()
    if not new_lines:
        return base
    if not base_lines:
        return new

    best_overlap = 0
    for overlap_len in range(1, min(len(base_lines), len(new_lines)) + 1):
        if all(
            b.strip() == n.strip()
            for b, n in zip(
                base_lines[-overlap_len:],
                new_lines[:overlap_len],
                strict=False,
            )
        ):
            best_overlap = overlap_len

    if best_overlap > 0:
        return "\n".join(base_lines + new_lines[best_overlap:])
    return base + "\n\n--- next frame ---\n\n" + new


def build_instant_memory(
    scene: Scene,
    eyes_results: list[EyesResult],
) -> InstantMemory:
    """Merge frame-level texts into a single per-scene extraction.

    Uses code-based overlap merge (no LLM).  Returns an
    ``InstantMemory`` with ``method='single'`` or ``'code_diff'``.
    The ``'code_diff_fallback'`` method is set when the merge
    produces too many separators (caller may upgrade to LLM merge).
    """
    texts = [_extract_element_text(r.response) for r in eyes_results]

    if len(texts) <= 1:
        return InstantMemory(
            scene_id=scene.scene_id,
            merged_text=texts[0] if texts else "",
            frame_count=len(texts),
            method=MergeMethod.SINGLE,
        )

    merged = texts[0]
    for t in texts[1:]:
        merged = _merge_code_texts(merged, t)

    separator_count = merged.count("--- next frame ---")
    method = (
        MergeMethod.CODE_DIFF_FALLBACK
        if separator_count > len(texts) // 2
        else MergeMethod.CODE_DIFF
    )

    return InstantMemory(
        scene_id=scene.scene_id,
        merged_text=merged,
        frame_count=len(texts),
        method=method,
    )


# ---------------------------------------------------------------------------
# MemoryPipeline (LLM-based scene + course synthesis)
# ---------------------------------------------------------------------------


class MemoryPipeline:
    """Hierarchical memory: instant merge -> scene synthesis -> course context.

    Args:
        key_pool: Gemini API key pool for LLM calls.
        model: Text LLM model (same as Eyes or cheaper).
        rpm_per_key: Requests per minute per API key.
    """

    def __init__(
        self,
        key_pool: KeyPool,
        *,
        model: str = "gemini-2.5-flash",
        rpm_per_key: int = 10,
    ) -> None:
        self._key_pool = key_pool
        self._model = model
        total_rpm = rpm_per_key * len(key_pool)
        self._min_interval = 60.0 / total_rpm
        self._last_call_time = 0.0
        self._lock = asyncio.Lock()

    async def process_scene(
        self,
        scene: Scene,
        eyes_results: list[EyesResult],
        course_memory: CourseMemory,
    ) -> tuple[InstantMemory, SceneMemory, CourseMemory]:
        """Run all three memory stages for one scene.

        Returns:
            Tuple of (instant_memory, scene_memory, updated_course_memory).
        """
        # 1. Instant merge (code-based, no LLM)
        instant = build_instant_memory(scene, eyes_results)

        # 1b. LLM fallback if code merge failed
        if instant.method == MergeMethod.CODE_DIFF_FALLBACK:
            instant = await self._llm_merge(scene, eyes_results, instant)

        # 2. Scene synthesis (LLM)
        scene_mem = await self._synthesize_scene(
            scene,
            eyes_results,
            instant,
        )

        # 3. Course memory update
        updated_course = await self._update_course_memory(
            scene,
            scene_mem,
            course_memory,
        )

        return instant, scene_mem, updated_course

    async def _llm_merge(
        self,
        scene: Scene,
        eyes_results: list[EyesResult],
        fallback_instant: InstantMemory,
    ) -> InstantMemory:
        """Attempt LLM merge when code-based merge has too many separators."""
        texts = [_extract_element_text(r.response) for r in eyes_results]
        frame_texts = "\n\n".join(
            f"Frame {j + 1} ({eyes_results[j].timestamp_sec:.0f}s):\n{t}"
            for j, t in enumerate(texts)
        )
        template = _load_template("instant_merge.txt")
        prompt = template.format(n=len(texts), frame_texts=frame_texts)

        try:
            result = await self._call_llm(prompt)
            logger.info(
                "instant_llm_merge",
                scene_id=scene.scene_id,
                frames=len(texts),
            )
            return InstantMemory(
                scene_id=scene.scene_id,
                merged_text=result,
                frame_count=len(texts),
                method=MergeMethod.LLM_MERGE,
            )
        except Exception:
            logger.warning(
                "instant_llm_merge_failed",
                scene_id=scene.scene_id,
            )
            return fallback_instant

    async def _synthesize_scene(
        self,
        scene: Scene,
        eyes_results: list[EyesResult],
        instant: InstantMemory,
    ) -> SceneMemory:
        """LLM synthesis: generate scene type, summary, topics."""
        descriptions = "\n".join(
            f"- {r.timestamp_sec:.0f}s: {r.description}" for r in eyes_results
        )
        template = _load_template("scene_memory.txt")
        prompt = template.format(
            scene_id=scene.scene_id,
            n=len(eyes_results),
            ts_start=scene.start_sec,
            ts_end=scene.end_sec,
            descriptions=descriptions,
            merged_text=instant.merged_text[:2000],
        )

        raw = await self._call_llm(prompt)
        return self._parse_scene_memory(scene.scene_id, raw)

    async def _update_course_memory(
        self,
        scene: Scene,
        scene_mem: SceneMemory,
        current: CourseMemory,
    ) -> CourseMemory:
        """Update running course context with new scene info."""
        # Skip LLM for low-importance scenes — just append
        if scene_mem.importance <= 2 and current.text:
            short = scene_mem.summary[:80]
            new_text = f"{current.text}\n+ scene {scene.scene_id}: {short}"
            return CourseMemory(
                text=new_text,
                scenes_processed=current.scenes_processed + 1,
                history=[
                    *current.history,
                    CourseMemorySnapshot(
                        scene_id=scene.scene_id,
                        context_snapshot=new_text[:500],
                    ),
                ],
            )

        topics = ", ".join(scene_mem.topics) if scene_mem.topics else "N/A"
        template = _load_template("course_memory.txt")
        prompt = template.format(
            previous_context=current.text or "(beginning of video)",
            scene_summary=scene_mem.summary,
            topics=topics,
        )

        new_text = await self._call_llm(prompt)
        return CourseMemory(
            text=new_text,
            scenes_processed=current.scenes_processed + 1,
            history=[
                *current.history,
                CourseMemorySnapshot(
                    scene_id=scene.scene_id,
                    context_snapshot=new_text[:500],
                ),
            ],
        )

    async def _call_llm(self, prompt: str) -> str:
        """Rate-limited text LLM call."""
        import google.genai as genai

        async with self._lock:
            elapsed = time.monotonic() - self._last_call_time
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)

            key = self._key_pool.next_key()
            client = genai.Client(api_key=key.get_secret_value())

            t0 = time.monotonic()
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=self._model,
                contents=prompt,
            )
            self._last_call_time = time.monotonic()

            logger.info(
                "memory_llm_call",
                model=self._model,
                latency=round(time.monotonic() - t0, 2),
            )
            return response.text or ""

    @staticmethod
    def _parse_scene_memory(scene_id: int, raw: str) -> SceneMemory:
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
                scene_type=data.get("scene_type", "unknown"),
                summary=data.get("summary", ""),
                complete_text=data.get("complete_text", ""),
                topics=data.get("topics", []),
                importance=min(max(data.get("importance", 3), 1), 5),
            )
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "scene_memory_parse_failed",
                scene_id=scene_id,
            )
            return SceneMemory(
                scene_id=scene_id,
                scene_type="unknown",
                summary=raw[:200],
            )
