"""VD-017 Outline Spike: video → STT ‖ VD → SourceDocument → OutlineAgent (4 models).

Processes multiple videos through the full pipeline and generates
MaterialOutline with 4 different LLM models for A/B comparison.

Checkpoint/resume at every stage:
- STT result cached per video
- VD result cached per video (with per-scene checkpoint)
- Outline result cached per video per model

Usage:
    uv run python scripts/vd017_outline_spike.py

Re-run safely: skips completed stages, resumes VD from last checkpoint.
Delete tmp/vd-017-spike/ to restart from scratch.
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from _utils import load_env

if TYPE_CHECKING:
    from course_supporter.models.source import SourceDocument

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VIDEO_DIR = Path("current-doc/vd-spike/example-video")
OUT_DIR = Path("tmp/vd-017-spike")

VIDEOS = [
    "2.1 What is ORM.mp4",
    "2.2 Configure Django ORM.mp4",
    "2.3 Models overview.mp4",
]

# Models to compare for outline generation
OUTLINE_MODELS: list[dict[str, str]] = [
    {
        "id": "claude-sonnet-4-20250514",
        "provider": "anthropic",
        "label": "Claude Sonnet 4",
    },
    {"id": "gpt-4o", "provider": "openai", "label": "GPT-4o"},
    {"id": "gpt-4.1", "provider": "openai", "label": "GPT-4.1"},
    {"id": "deepseek-chat", "provider": "deepseek", "label": "DeepSeek V3"},
]


def slug(video_name: str) -> str:
    """Turn filename into a filesystem-safe slug."""
    return video_name.replace(" ", "_").replace(".mp4", "").lower()


# ---------------------------------------------------------------------------
# STT (cached per video)
# ---------------------------------------------------------------------------


async def run_stt(video_path: Path, cache_path: Path) -> dict:
    """Extract audio → STT Router → cached JSON."""
    if cache_path.exists():
        print(f"  STT: cached ({cache_path.name})")
        return json.loads(cache_path.read_text())

    from course_supporter.config import get_settings
    from course_supporter.stt.setup import create_stt_router

    load_env()
    settings = get_settings()
    router = create_stt_router(settings)

    print("  STT: extracting audio...")
    from course_supporter.ingestion.video import _extract_audio_ffmpeg

    t0 = time.monotonic()
    audio_path = await _extract_audio_ffmpeg(str(video_path))

    try:
        print(f"  STT: transcribing ({router.available_providers()})...")
        result = await router.transcribe(action="transcribe", audio_path=audio_path)
    finally:
        Path(audio_path).unlink(missing_ok=True)

    elapsed = round(time.monotonic() - t0, 1)
    print(
        f"  STT: done in {elapsed}s — {len(result.segments)} segments, "
        f"provider={result.provider}"
    )

    data = {
        "segments": [
            {"start_sec": s.start_sec, "end_sec": s.end_sec, "text": s.text}
            for s in result.segments
        ],
        "provider": result.provider,
        "model_id": result.model_id,
        "latency_ms": result.latency_ms,
        "audio_duration_sec": result.audio_duration_sec,
        "elapsed_sec": elapsed,
    }
    cache_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


# ---------------------------------------------------------------------------
# VD (cached per video, with per-scene checkpoint)
# ---------------------------------------------------------------------------


async def run_vd(video_path: Path, ckpt_path: Path, result_path: Path) -> dict:
    """Run VDPipeline with checkpoint/resume."""
    if result_path.exists():
        print(f"  VD: cached ({result_path.name})")
        return json.loads(result_path.read_text())

    from course_supporter.key_pool import KeyPool
    from course_supporter.vd.frame_sampler import FrameSampler
    from course_supporter.vd.memory_pipeline import MemoryPipeline
    from course_supporter.vd.schemas import SamplingParams, SceneMemory, VideoMemory
    from course_supporter.vd.visual_analyzer import VisualAnalyzer

    load_env()
    raw_keys = os.environ.get("GEMINI_API_KEY", "")
    key_pool = KeyPool(raw_keys)

    sampler = FrameSampler(SamplingParams())
    memory = MemoryPipeline(key_pool, model="gemini-2.5-flash", rpm_per_key=2)
    analyzer = VisualAnalyzer(
        key_pool, model="gemini-2.5-flash", rpm_per_key=2, memory=memory
    )

    print("  VD Stage A: sampling frames...")
    t0 = time.monotonic()
    temp_dir = Path(tempfile.mkdtemp(prefix="vd017_"))

    try:
        sampling = await sampler.sample(video_path, temp_dir)
        stage_a_sec = round(time.monotonic() - t0, 1)
        print(
            f"    {len(sampling.frames)} frames, "
            f"{len(sampling.scenes)} scenes, {stage_a_sec}s"
        )

        print("  VD Stage B: visual analysis + memory...")
        t1 = time.monotonic()

        scene_results: list[dict] = []
        video_memory = VideoMemory()
        previous_scene: SceneMemory | None = None
        skip_scene_ids: set[int] = set()

        if ckpt_path.exists():
            ckpt = json.loads(ckpt_path.read_text())
            scene_results = ckpt.get("scenes", [])
            video_memory = VideoMemory(
                text=ckpt.get("video_memory", ""),
                scenes_processed=len(scene_results),
            )
            skip_scene_ids = {s["scene_id"] for s in scene_results}
            if scene_results:
                last = scene_results[-1]["scene_memory"]
                previous_scene = SceneMemory(
                    scene_id=scene_results[-1]["scene_id"],
                    summary=last["summary"],
                    scene_type=last["scene_type"],
                    topics=last["topics"],
                    importance=last["importance"],
                    frames_seen=last["frames_seen"],
                )
            print(f"    Resuming: {len(scene_results)} scenes done")

        for scene in sampling.scenes:
            if scene.scene_id in skip_scene_ids:
                continue
            scene_frames = [f for f in sampling.frames if f.scene_id == scene.scene_id]
            if not scene_frames:
                continue

            scene_memory = SceneMemory(
                scene_id=scene.scene_id,
                previous_scene_summary=(
                    previous_scene.summary[:300] if previous_scene else ""
                ),
            )

            print(
                f"    Scene {scene.scene_id} "
                f"({scene.start_sec:.0f}-{scene.end_sec:.0f}s, "
                f"{len(scene_frames)} frames)...",
                end=" ",
                flush=True,
            )

            eyes_results, final_scene_mem = await analyzer.analyze_scene(
                scene,
                scene_frames,
                temp_dir,
                video_memory=video_memory,
                scene_memory=scene_memory,
            )

            resolved = final_scene_mem or scene_memory
            video_memory = await memory.update_video_memory(
                resolved, video_memory, previous_scene
            )

            n_delta = sum(1 for r in eyes_results if r.is_delta)
            print(
                f"OK ({len(eyes_results)} eyes, {n_delta} delta, "
                f"type={resolved.scene_type})"
            )

            scene_results.append(
                {
                    "scene_id": scene.scene_id,
                    "start_sec": scene.start_sec,
                    "end_sec": scene.end_sec,
                    "n_frames": len(scene_frames),
                    "scene_memory": {
                        "summary": resolved.summary,
                        "scene_type": resolved.scene_type,
                        "topics": resolved.topics,
                        "importance": resolved.importance,
                        "frames_seen": resolved.frames_seen,
                    },
                }
            )
            previous_scene = resolved

            # Checkpoint after each scene
            ckpt_path.write_text(
                json.dumps(
                    {"video_memory": video_memory.text, "scenes": scene_results},
                    ensure_ascii=False,
                )
            )

        stage_b_sec = round(time.monotonic() - t1, 1)
        total_sec = round(time.monotonic() - t0, 1)

    finally:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)

    result = {
        "stats": {
            "total_frames": len(sampling.frames),
            "total_scenes": len(sampling.scenes),
            "processed_scenes": len(scene_results),
            "stage_a_sec": stage_a_sec,
            "stage_b_sec": stage_b_sec,
            "total_sec": total_sec,
        },
        "video_memory": video_memory.text,
        "scenes": scene_results,
    }
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    return result


# ---------------------------------------------------------------------------
# Build SourceDocument from STT + VD
# ---------------------------------------------------------------------------


def build_source_document(
    video_name: str, stt_data: dict, vd_data: dict
) -> SourceDocument:
    """Combine STT + VD into a SourceDocument."""
    from course_supporter.models.source import (
        ChunkType,
        ContentChunk,
        SourceDocument,
    )

    stt_chunks = [
        ContentChunk(
            chunk_type=ChunkType.TRANSCRIPT,
            text=seg["text"],
            index=i,
            start_sec=seg["start_sec"],
            end_sec=seg["end_sec"],
        )
        for i, seg in enumerate(stt_data["segments"])
    ]

    vd_scenes = vd_data["scenes"]
    vd_chunks: list[ContentChunk] = []
    for i, scene in enumerate(vd_scenes):
        start = scene["start_sec"]
        end = (
            vd_scenes[i + 1]["start_sec"]
            if i + 1 < len(vd_scenes)
            else scene["end_sec"]
        )
        vd_chunks.append(
            ContentChunk(
                chunk_type=ChunkType.VISUAL_SCENE,
                text=scene["scene_memory"]["summary"],
                index=i,
                start_sec=start,
                end_sec=end,
                metadata={
                    "scene_id": scene["scene_id"],
                    "scene_type": scene["scene_memory"]["scene_type"],
                },
            )
        )

    return SourceDocument(
        source_type="video",
        source_url=f"file:///{video_name}",
        title=video_name.replace(".mp4", ""),
        chunks=stt_chunks + vd_chunks,
    )


# ---------------------------------------------------------------------------
# Outline generation (cached per video per model)
# ---------------------------------------------------------------------------


async def run_outline(
    source_doc: SourceDocument,
    model_cfg: dict[str, str],
    cache_path: Path,
) -> dict:
    """Run OutlineAgent with a specific model. Returns outline + metadata."""
    if cache_path.exists():
        print(f"    {model_cfg['label']}: cached")
        return json.loads(cache_path.read_text())

    from course_supporter.config import get_settings
    from course_supporter.llm.setup import create_model_router

    load_env()
    settings = get_settings()
    router = create_model_router(settings)

    print(f"    {model_cfg['label']}: generating...", end=" ", flush=True)
    t0 = time.monotonic()

    # Override: call router directly with specific model
    from course_supporter.agents.prompt_loader import format_user_prompt, load_prompt
    from course_supporter.models.outline import MaterialOutline

    prompt_data = load_prompt("prompts/outline/v1.yaml")
    user_prompt = format_user_prompt(
        prompt_data.user_prompt_template,
        source_doc.model_dump_json(),
    )

    result_obj, response = await router.complete_structured(
        action="material_outline",
        prompt=user_prompt,
        response_schema=MaterialOutline,
        system_prompt=prompt_data.system_prompt,
        temperature=0.0,
        strategy=model_cfg["id"],  # strategy name = model id (see below)
    )
    elapsed = round(time.monotonic() - t0, 1)
    outline: MaterialOutline = result_obj

    # Guard: ensure we got the requested model, not a silent fallback
    if response.model_id != model_cfg["id"]:
        msg = (
            f"FALLBACK DETECTED: requested {model_cfg['id']} "
            f"but got {response.model_id} ({response.provider}). "
            f"Check API key and model config."
        )
        print(f"ERROR: {msg}")
        raise RuntimeError(msg)

    print(
        f"OK ({elapsed}s, {response.tokens_in} in, "
        f"{response.tokens_out} out, model={response.model_id})"
    )

    data = {
        "model_id": response.model_id,
        "model_label": model_cfg["label"],
        "provider": response.provider,
        "tokens_in": response.tokens_in,
        "tokens_out": response.tokens_out,
        "cost_usd": response.cost_usd,
        "elapsed_sec": elapsed,
        "prompt_version": prompt_data.version,
        "outline": outline.model_dump(mode="json"),
    }
    cache_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_CSS = """
body { font-family: system-ui, sans-serif; margin: 0; padding: 20px;
       background: #0a0a1a; color: #eee; max-width: 1800px; margin: 0 auto; }
h1 { color: #e94560; }
h2 { color: #4ecca3; border-bottom: 2px solid #4ecca3; padding: 8px 0; margin-top: 40px; }
h3 { color: #e9a945; margin-top: 24px; }
h4 { color: #7ec8e3; margin-top: 16px; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
         gap: 10px; margin: 16px 0; }
.stat-card { background: #16213e; border-radius: 8px; padding: 12px; text-align: center; }
.stat-value { font-size: 24px; font-weight: bold; color: #4ecca3; }
.stat-label { font-size: 11px; color: #888; margin-top: 4px; }
.model-block { background: #16213e; border-radius: 8px; padding: 16px; margin: 12px 0; }
.section-block { background: #1a1a2e; border-radius: 6px; padding: 12px; margin: 8px 0;
                 border-left: 3px solid #4ecca3; }
.code-block { background: #0d1117; border-radius: 4px; padding: 10px; margin: 6px 0;
              font-family: monospace; font-size: 13px; white-space: pre-wrap;
              overflow-x: auto; color: #c9d1d9; }
.narration { background: #1e2d3d; border-radius: 4px; padding: 10px; margin: 6px 0;
             line-height: 1.5; white-space: pre-wrap; }
.concepts { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0; }
.concept-tag { background: #3a7bd5; color: #fff; padding: 2px 8px; border-radius: 4px;
               font-size: 12px; }
.meta-row { display: flex; gap: 16px; flex-wrap: wrap; font-size: 13px; color: #aaa; }
.compare-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(400px, 1fr));
                gap: 16px; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; }
th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #333; }
th { background: #16213e; color: #4ecca3; }
"""


def _esc(text: str) -> str:
    return html_mod.escape(text)


def _render_outline_sections(outline: dict) -> str:
    """Render outline sections as HTML."""
    parts: list[str] = []
    for i, sec in enumerate(outline.get("sections", [])):
        time_range = f"{sec['start_sec']:.0f}s – {sec['end_sec']:.0f}s"
        parts.append('<div class="section-block">')
        parts.append(
            f"<h4>Section {i + 1}: {_esc(sec['title'])} "
            f'<span style="color:#888; font-size:12px;">({time_range})</span></h4>'
        )

        parts.append(f'<div class="narration">{_esc(sec["narration"])}</div>')

        if sec.get("screen_content"):
            parts.append(
                f'<div style="color:#aaa; margin:4px 0;">'
                f"<b>Screen:</b> {_esc(sec['screen_content'])}</div>"
            )

        for snippet in sec.get("code_snippets", []):
            parts.append(
                f'<div style="color:#aaa; font-size:12px;">'
                f"{_esc(snippet.get('language', ''))} — "
                f"{_esc(snippet.get('context', ''))}</div>"
            )
            parts.append(f'<div class="code-block">{_esc(snippet["code"])}</div>')

        if sec.get("key_concepts"):
            parts.append('<div class="concepts">')
            for c in sec["key_concepts"]:
                parts.append(f'<span class="concept-tag">{_esc(c)}</span>')
            parts.append("</div>")

        parts.append("</div>")
    return "\n".join(parts)


def generate_report(
    all_results: dict[str, dict],
    report_path: Path,
) -> None:
    """Generate HTML comparison report."""
    parts: list[str] = []
    parts.append(f"<html><head><style>{_CSS}</style></head><body>")
    parts.append("<h1>VD-017: Material Outline — A/B Comparison</h1>")

    for video_name, video_data in all_results.items():
        parts.append(f"<h2>{_esc(video_name)}</h2>")

        # STT & VD stats
        stt = video_data.get("stt", {})
        vd = video_data.get("vd", {})
        vd_stats = vd.get("stats", {})
        parts.append('<div class="stats">')
        for label, value in [
            ("STT segments", len(stt.get("segments", []))),
            ("STT provider", stt.get("provider", "?")),
            ("VD scenes", vd_stats.get("processed_scenes", "?")),
            ("VD frames", vd_stats.get("total_frames", "?")),
            ("Duration", f"{stt.get('audio_duration_sec', 0):.0f}s"),
        ]:
            parts.append(
                f'<div class="stat-card">'
                f'<div class="stat-value">{value}</div>'
                f'<div class="stat-label">{label}</div></div>'
            )
        parts.append("</div>")

        # Cost comparison table
        outlines = video_data.get("outlines", {})
        if outlines:
            parts.append("<h3>Model Comparison</h3>")
            parts.append(
                "<table><tr><th>Model</th><th>Tokens In</th><th>Tokens Out</th>"
                "<th>Cost</th><th>Time</th><th>Sections</th></tr>"
            )
            for model_label, odata in outlines.items():
                outline = odata.get("outline", {})
                parts.append(
                    f"<tr><td>{_esc(model_label)}</td>"
                    f"<td>{odata.get('tokens_in', 0):,}</td>"
                    f"<td>{odata.get('tokens_out', 0):,}</td>"
                    f"<td>${odata.get('cost_usd', 0):.4f}</td>"
                    f"<td>{odata.get('elapsed_sec', 0):.1f}s</td>"
                    f"<td>{len(outline.get('sections', []))}</td></tr>"
                )
            parts.append("</table>")

        # Per-model outlines
        for model_label, odata in outlines.items():
            outline = odata.get("outline", {})
            parts.append(f"<h3>{_esc(model_label)}</h3>")
            parts.append('<div class="model-block">')

            # Macro fields
            parts.append('<div class="meta-row">')
            for k in ["title", "language", "source_type", "target_audience"]:
                val = outline.get(k, "")
                parts.append(f"<span><b>{k}:</b> {_esc(str(val))}</span>")
            parts.append("</div>")

            if outline.get("summary"):
                parts.append(
                    f'<div style="margin:8px 0; color:#ccc;">'
                    f"<b>Summary:</b> {_esc(outline['summary'])}</div>"
                )
            if outline.get("topics"):
                parts.append(
                    f'<div style="margin:4px 0; color:#aaa;">'
                    f"<b>Topics:</b> {', '.join(outline['topics'])}</div>"
                )
            if outline.get("tools"):
                parts.append(
                    f'<div style="margin:4px 0; color:#aaa;">'
                    f"<b>Tools:</b> {', '.join(outline['tools'])}</div>"
                )

            presenter = outline.get("presenter", {})
            if presenter:
                parts.append(
                    f'<div style="margin:4px 0; color:#aaa;">'
                    f"<b>Presenter:</b> {_esc(presenter.get('description', ''))}"
                    f" ({_esc(presenter.get('style', ''))})</div>"
                )

            # Sections
            parts.append(_render_outline_sections(outline))
            parts.append("</div>")  # model-block

    parts.append("</body></html>")
    report_path.write_text("\n".join(parts), encoding="utf-8")
    print(f"\nReport: {report_path}")


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    all_results: dict[str, dict] = {}

    for video_name in VIDEOS:
        video_path = VIDEO_DIR / video_name
        if not video_path.exists():
            print(f"SKIP: {video_name} not found")
            continue

        vid_slug = slug(video_name)
        print(f"\n{'=' * 60}")
        print(f"Processing: {video_name}")
        print(f"{'=' * 60}")

        # Stage 1: STT
        stt_cache = OUT_DIR / f"stt_{vid_slug}.json"
        stt_data = await run_stt(video_path, stt_cache)

        # Stage 2: VD
        vd_ckpt = OUT_DIR / f"vd_ckpt_{vid_slug}.json"
        vd_cache = OUT_DIR / f"vd_{vid_slug}.json"
        vd_data = await run_vd(video_path, vd_ckpt, vd_cache)

        # Build SourceDocument
        source_doc = build_source_document(video_name, stt_data, vd_data)
        # Cache SourceDocument for reference
        sd_path = OUT_DIR / f"source_doc_{vid_slug}.json"
        if not sd_path.exists():
            sd_path.write_text(source_doc.model_dump_json(indent=2))

        # Stage 3: Outline (4 models)
        print("  Outline generation:")
        outlines: dict[str, dict] = {}
        for model_cfg in OUTLINE_MODELS:
            outline_cache = OUT_DIR / f"outline_{vid_slug}_{model_cfg['id']}.json"
            odata = await run_outline(source_doc, model_cfg, outline_cache)
            outlines[model_cfg["label"]] = odata

        all_results[video_name] = {
            "stt": stt_data,
            "vd": vd_data,
            "outlines": outlines,
        }

    # Generate HTML report
    report_path = OUT_DIR / "outline_comparison.html"
    generate_report(all_results, report_path)
    print("\nDone! Open the report to compare outline quality across models.")


if __name__ == "__main__":
    asyncio.run(main())
