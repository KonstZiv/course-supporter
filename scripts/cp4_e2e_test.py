"""VD-011 E2E Test: video → STT ‖ VD → alignment → SourceDocument.

Combined checkpoint script covering CP-4 (VD quality), VD-011 (integration),
and CP-5 (cross-modal alignment) in a single API call session.

Usage:
    uv run python scripts/cp4_e2e_test.py [video_path] [--max-sec 300]

Checkpoint/resume: saves after each VD scene. Re-run to continue from where
you left off. Delete tmp/cp4-e2e/checkpoint_*.json to restart.

STT result is cached separately from VD — each branch runs independently.
"""

from __future__ import annotations

import argparse
import asyncio
import html as html_mod
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from _utils import find_frame, load_env, thumb_b64

OUT_DIR = Path("tmp/cp4-e2e")
DEFAULT_VIDEO = "tmp/cp1-test/video1_python_16min.mp4"


# ---------------------------------------------------------------------------
# Data containers (serializable to/from JSON checkpoint)
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    """All data needed for the HTML report."""

    video: str
    max_sec: float | None

    # STT branch
    stt_segments: int
    stt_provider: str
    stt_model: str
    stt_latency_ms: int
    stt_audio_duration_sec: float | None
    stt_chunks_json: list[dict]

    # VD branch
    vd_stats: dict
    vd_video_memory: str
    vd_scenes: list[dict]

    # Alignment
    alignment: dict

    # Timing
    stt_time_sec: float
    vd_time_sec: float
    total_time_sec: float

    def to_dict(self) -> dict:
        return self.__dict__

    @classmethod
    def from_dict(cls, d: dict) -> RunResult:
        return cls(**d)


# ---------------------------------------------------------------------------
# STT branch
# ---------------------------------------------------------------------------


async def run_stt(
    video_path: Path,
    cache_path: Path,
) -> dict:
    """Extract audio → STT Router → cached JSON result."""
    if cache_path.exists():
        print("  STT: loading from cache")
        return json.loads(cache_path.read_text())

    from course_supporter.config import get_settings
    from course_supporter.stt.setup import create_stt_router

    load_env()
    settings = get_settings()
    router = create_stt_router(settings)

    print(f"  STT: providers = {router.available_providers()}")

    # Extract audio
    print("  STT: extracting audio (FFmpeg)...")
    from course_supporter.ingestion.video import _extract_audio_ffmpeg

    t0 = time.monotonic()
    audio_path = await _extract_audio_ffmpeg(str(video_path))

    try:
        print("  STT: transcribing...")
        result = await router.transcribe(
            action="transcribe",
            audio_path=audio_path,
        )
    finally:
        Path(audio_path).unlink(missing_ok=True)

    elapsed = round(time.monotonic() - t0, 1)
    print(
        f"  STT: done in {elapsed}s — "
        f"{len(result.segments)} segments, "
        f"provider={result.provider}, model={result.model_id}"
    )

    data = {
        "segments": [
            {
                "start_sec": s.start_sec,
                "end_sec": s.end_sec,
                "text": s.text,
            }
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
# VD branch (with per-scene checkpoint, reused from cp4_gate_review pattern)
# ---------------------------------------------------------------------------


async def run_vd(
    video_path: Path,
    max_sec: float | None,
    ckpt_path: Path,
    result_path: Path,
) -> dict:
    """Run VDPipeline with checkpoint/resume. Returns serialized result."""
    if result_path.exists():
        print("  VD: loading from cache")
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
        key_pool,
        model="gemini-2.5-flash",
        rpm_per_key=2,
        memory=memory,
    )

    # Stage A
    print("  VD Stage A: sampling frames...")
    t0 = time.monotonic()
    temp_dir = Path(tempfile.mkdtemp(prefix="cp4e2e_"))

    try:
        sampling = await sampler.sample(video_path, temp_dir)
        stage_a_sec = round(time.monotonic() - t0, 1)
        print(
            f"    {len(sampling.frames)} frames, "
            f"{len(sampling.scenes)} scenes, {stage_a_sec}s"
        )

        scenes = sampling.scenes
        if max_sec is not None:
            scenes = [s for s in scenes if s.start_sec < max_sec]
            print(f"    Filtered to {len(scenes)} scenes (max_sec={max_sec})")

        # Stage B — with checkpoint/resume
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
            print(f"    Resuming from checkpoint: {len(scene_results)} scenes done")

        for scene in scenes:
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
                resolved,
                video_memory,
                previous_scene,
            )

            n_delta = sum(1 for r in eyes_results if r.is_delta)
            print(
                f"OK ({len(eyes_results)} eyes, {n_delta} delta, "
                f"type={resolved.scene_type})"
            )

            # Collect frame data
            frame_thumbs: list[dict] = []
            for er in eyes_results:
                frame_data = next(
                    (f for f in scene_frames if f.frame_id == er.frame_id),
                    None,
                )
                thumb = ""
                if frame_data:
                    fpath = find_frame(temp_dir, frame_data.filename)
                    if fpath is not None:
                        thumb = thumb_b64(fpath, max_w=400)

                frame_thumbs.append(
                    {
                        "frame_id": er.frame_id,
                        "timestamp_sec": er.timestamp_sec,
                        "is_delta": er.is_delta,
                        "description": er.description[:200],
                        "response": er.response,
                        "scene_type": er.scene_type,
                        "tokens_in": er.input_tokens,
                        "tokens_out": er.output_tokens,
                        "latency_sec": er.latency_sec,
                        "thumb_b64": thumb,
                    }
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
                    "frames": frame_thumbs,
                }
            )
            previous_scene = resolved

            # Checkpoint after each scene
            ckpt_path.write_text(
                json.dumps(
                    {"video_memory": video_memory.text, "scenes": scene_results},
                    ensure_ascii=False,
                ),
            )

        stage_b_sec = round(time.monotonic() - t1, 1)
        total_sec = round(time.monotonic() - t0, 1)

    finally:
        import shutil

        shutil.rmtree(temp_dir, ignore_errors=True)

    total_tokens_in = sum(f["tokens_in"] for s in scene_results for f in s["frames"])
    total_tokens_out = sum(f["tokens_out"] for s in scene_results for f in s["frames"])
    eyes_calls = sum(len(s["frames"]) for s in scene_results)

    result = {
        "stats": {
            "total_frames": len(sampling.frames),
            "total_scenes": len(sampling.scenes),
            "processed_scenes": len(scene_results),
            "eyes_calls": eyes_calls,
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
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
# Alignment (no API calls — purely algorithmic)
# ---------------------------------------------------------------------------


def run_alignment(
    stt_data: dict,
    vd_data: dict,
) -> dict:
    """Run CrossModalAligner on STT + VD chunks."""
    from course_supporter.ingestion.alignment import CrossModalAligner
    from course_supporter.models.source import ChunkType, ContentChunk

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

    vd_chunks = [
        ContentChunk(
            chunk_type=ChunkType.VISUAL_SCENE,
            text=scene["scene_memory"]["summary"],
            index=i,
            start_sec=scene["start_sec"],
            end_sec=scene["end_sec"],
            metadata={
                "scene_id": scene["scene_id"],
                "scene_type": scene["scene_memory"]["scene_type"],
            },
        )
        for i, scene in enumerate(vd_data["scenes"])
    ]

    aligner = CrossModalAligner()
    report = aligner.align(stt_chunks, vd_chunks)

    return {
        "total_segments": len(report.segments),
        "coverage_gaps": [
            {
                "start_sec": g.start_sec,
                "end_sec": g.end_sec,
                "gap_type": g.gap_type,
            }
            for g in report.coverage_gaps
        ],
        "vd_orphans": report.vd_orphans,
        "stt_orphans": report.stt_orphans,
        "conflicts": report.conflicts,
        "semantic_coverage": report.semantic_coverage,
        "segments": [
            {
                "start_sec": s.start_sec,
                "end_sec": s.end_sec,
                "stt_text": (s.stt_text or "")[:200],
                "vd_scene_id": s.vd_scene_id,
                "vd_summary": (s.vd_summary or "")[:200],
                "semantic_overlap": round(s.semantic_overlap, 3),
                "alignment_confidence": round(s.alignment_confidence, 3),
                "conflicts": s.conflicts,
            }
            for s in report.segments
        ],
    }


# ---------------------------------------------------------------------------
# HTML report generation (3 sections)
# ---------------------------------------------------------------------------


_CSS = """
body { font-family: system-ui, sans-serif; margin: 0; padding: 20px;
       background: #0a0a1a; color: #eee; max-width: 1600px; margin: 0 auto; }
h1 { color: #e94560; }
h2 { color: #4ecca3; border-bottom: 2px solid #4ecca3;
     padding: 8px 0; margin-top: 40px; }
h3 { color: #e9a945; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
         gap: 12px; margin: 20px 0; }
.stat-card { background: #16213e; border-radius: 8px; padding: 16px;
             text-align: center; }
.stat-value { font-size: 28px; font-weight: bold; color: #4ecca3; }
.stat-label { font-size: 12px; color: #888; margin-top: 4px; }
.scene-block { background: #16213e; border-radius: 8px;
               padding: 16px; margin: 16px 0; }
.scene-header { display: flex; justify-content: space-between;
                align-items: center; margin-bottom: 12px; }
.badge { padding: 2px 8px; border-radius: 4px; font-size: 12px;
         font-weight: bold; display: inline-block; }
.badge-type { background: #3a7bd5; color: #fff; }
.badge-imp { background: #e94560; color: #fff; }
.badge-delta { background: #e94560; color: #fff; font-size: 10px; }
.badge-full { background: #4ecca3; color: #000; font-size: 10px; }
.summary { font-style: italic; color: #ccc; background: #1a3a5a;
           padding: 10px; border-radius: 4px;
           border-left: 3px solid #4ecca3; margin: 8px 0; }
.topics { display: flex; gap: 6px; flex-wrap: wrap; margin: 8px 0; }
.topic { background: #2a4a7f; padding: 1px 6px; border-radius: 10px;
         font-size: 11px; }
.frames-grid { display: grid;
               grid-template-columns: repeat(auto-fill, minmax(420px, 1fr));
               gap: 8px; margin-top: 12px; }
.frame-card { background: #0f3460; border-radius: 4px; padding: 10px; }
.frame-card img { width: 100%; border-radius: 4px; margin-bottom: 6px; }
.frame-meta { font-size: 11px; color: #888; margin: 4px 0; }
.response { font-size: 12px; white-space: pre-wrap; background: #0a1a30;
            padding: 8px; border-radius: 4px; max-height: 300px;
            overflow-y: auto; margin-top: 6px; line-height: 1.4; }
.video-memory { background: #0d2137; border: 2px solid #4ecca3;
                border-radius: 8px; padding: 16px; margin: 20px 0;
                font-size: 14px; line-height: 1.6; }
.timeline { background: #16213e; border-radius: 8px; padding: 16px;
            margin: 16px 0; }
.timeline-row { display: flex; align-items: center; gap: 12px;
                padding: 4px 0; border-bottom: 1px solid #1a2a4e; }
.timeline-ts { font-family: monospace; color: #4ecca3; min-width: 80px; }
.timeline-type { min-width: 120px; }
.pass { color: #4ecca3; font-weight: bold; }
.fail { color: #e94560; font-weight: bold; }
.warn { color: #e9a945; font-weight: bold; }
.checklist { list-style: none; padding: 0; }
.checklist li { padding: 4px 0; font-size: 14px; }
.gap-row { display: flex; gap: 12px; padding: 4px 0;
           border-bottom: 1px solid #1a2a4e; font-size: 13px; }
.align-seg { display: grid; grid-template-columns: 80px 1fr 1fr 80px;
             gap: 8px; padding: 6px 0; border-bottom: 1px solid #1a2a4e;
             font-size: 12px; }
.conflict-item { background: #3a1010; border-left: 3px solid #e94560;
                 padding: 8px; margin: 4px 0; border-radius: 4px;
                 font-size: 13px; }
"""


def _esc(text: str) -> str:
    return html_mod.escape(text)


def generate_html(
    stt_data: dict,
    vd_data: dict,
    alignment: dict,
    video_name: str,
    max_sec: float | None,
    stt_time: float,
    vd_time: float,
    total_time: float,
) -> str:
    """Generate E2E report HTML with 3 sections."""
    p: list[str] = []
    a = p.append

    a("<!DOCTYPE html><html><head><meta charset='UTF-8'>")
    a(f"<title>VD-011 E2E: {_esc(video_name)}</title>")
    a(f"<style>{_CSS}</style></head><body>")

    max_note = f" (first {max_sec}s)" if max_sec else ""
    a(f"<h1>VD-011 E2E Report: {_esc(video_name)}{max_note}</h1>")
    a(f"<p>Total time: {total_time}s (STT: {stt_time}s, VD: {vd_time}s)</p>")

    # ---- SECTION 1: CP-4 — VD Pipeline Quality ----
    _section_cp4(a, vd_data)

    # ---- SECTION 2: VD-011 — E2E Integration ----
    _section_vd011(a, stt_data, vd_data, alignment, total_time)

    # ---- SECTION 3: CP-5 — Cross-Modal Alignment ----
    _section_cp5(a, alignment)

    a("</body></html>")
    return "".join(p)


def _section_cp4(a, vd: dict) -> None:
    """Section 1: CP-4 — VD Pipeline Quality."""
    a("<h2>Section 1: CP-4 — VD Pipeline Quality</h2>")

    stats = vd["stats"]

    # Stats cards
    a("<div class='stats'>")
    for label, value in [
        ("Scenes", stats["processed_scenes"]),
        ("Frames analyzed", stats["eyes_calls"]),
        ("Tokens in", f"{stats['total_tokens_in']:,}"),
        ("Tokens out", f"{stats['total_tokens_out']:,}"),
        ("Stage A", f"{stats['stage_a_sec']}s"),
        ("Stage B", f"{stats['stage_b_sec']}s"),
        ("Total VD", f"{stats['total_sec']}s"),
    ]:
        a(
            f"<div class='stat-card'>"
            f"<div class='stat-value'>{value}</div>"
            f"<div class='stat-label'>{label}</div></div>"
        )
    a("</div>")

    # Delta efficiency
    total_frames = stats["eyes_calls"]
    if total_frames > 0:
        n_delta = sum(1 for s in vd["scenes"] for f in s["frames"] if f["is_delta"])
        pct = round(100 * n_delta / total_frames, 1)
        a(f"<p>Delta efficiency: {n_delta}/{total_frames} frames = {pct}%</p>")

    # Timeline
    a("<h3>Timeline</h3>")
    a("<div class='timeline'>")
    for s in vd["scenes"]:
        sm = s["scene_memory"]
        imp = sm["importance"]
        imp_color = "#e94560" if imp >= 4 else "#888"
        a("<div class='timeline-row'>")
        a(f"<span class='timeline-ts'>{s['start_sec']:.0f}-{s['end_sec']:.0f}s</span>")
        a(
            f"<span class='timeline-type'>"
            f"<span class='badge badge-type'>{_esc(sm['scene_type'])}"
            f"</span></span>"
        )
        a(f"<span style='color:{imp_color}'>*{imp}</span>")
        a(f"<span>{_esc(sm['summary'][:120])}</span>")
        a("</div>")
    a("</div>")

    # Video memory
    a("<h3>Final Video Memory</h3>")
    a(f"<div class='video-memory'>{_esc(vd['video_memory'])}</div>")

    # Scene details
    a("<h3>Scene Details</h3>")
    for s in vd["scenes"]:
        sm = s["scene_memory"]
        n_delta = sum(1 for f in s["frames"] if f["is_delta"])

        a("<div class='scene-block'>")
        a("<div class='scene-header'>")
        a(f"<h3>Scene {s['scene_id']} ({s['start_sec']:.0f}-{s['end_sec']:.0f}s)</h3>")
        a(
            f"<span>"
            f"<span class='badge badge-type'>{_esc(sm['scene_type'])}</span> "
            f"<span class='badge badge-imp'>*{sm['importance']}</span> "
            f"{s['n_frames']} frames ({n_delta} delta)"
            f"</span>"
        )
        a("</div>")

        a(f"<div class='summary'>{_esc(sm['summary'])}</div>")

        if sm["topics"]:
            a("<div class='topics'>")
            for t in sm["topics"]:
                a(f"<span class='topic'>{_esc(t)}</span>")
            a("</div>")

        # Frames
        a("<div class='frames-grid'>")
        for f in s["frames"]:
            badge = (
                "<span class='badge badge-delta'>DELTA</span>"
                if f["is_delta"]
                else "<span class='badge badge-full'>FULL</span>"
            )
            a("<div class='frame-card'>")
            if f["thumb_b64"]:
                a(f"<img src='data:image/jpeg;base64,{f['thumb_b64']}'/>")
            a(
                f"<div class='frame-meta'>"
                f"{_esc(f['frame_id'])} ({f['timestamp_sec']:.0f}s) {badge} | "
                f"{f['latency_sec']}s | "
                f"in={f['tokens_in']} out={f['tokens_out']}"
                f"</div>"
            )
            a(f"<div class='response'>{_esc(f['response'])}</div>")
            a("</div>")
        a("</div>")  # frames-grid
        a("</div>")  # scene-block


def _section_vd011(a, stt: dict, vd: dict, alignment: dict, total_time: float) -> None:
    """Section 2: VD-011 — E2E Integration."""
    a("<h2>Section 2: VD-011 — E2E Integration</h2>")

    n_stt = len(stt["segments"])
    n_vd = len(vd["scenes"])
    has_both = n_stt > 0 and n_vd > 0
    strategy = "stt+vd" if has_both else "stt"

    # Summary
    a("<div class='stats'>")
    for label, value in [
        ("TRANSCRIPT chunks", n_stt),
        ("VISUAL_SCENE chunks", n_vd),
        ("Strategy", strategy),
        ("Total chunks", n_stt + n_vd),
    ]:
        a(
            f"<div class='stat-card'>"
            f"<div class='stat-value'>{value}</div>"
            f"<div class='stat-label'>{label}</div></div>"
        )
    a("</div>")

    # Compute coverage
    stt_duration = stt.get("audio_duration_sec") or 0
    if stt_duration == 0 and stt["segments"]:
        stt_duration = max(s["end_sec"] for s in stt["segments"])

    # Union coverage: merge all time intervals from STT + VD
    intervals: list[tuple[float, float]] = []
    for seg in stt["segments"]:
        if seg["start_sec"] is not None and seg["end_sec"] is not None:
            intervals.append((seg["start_sec"], seg["end_sec"]))
    for scene in vd["scenes"]:
        intervals.append((scene["start_sec"], scene["end_sec"]))

    covered = _union_coverage(intervals)
    coverage_pct = round(100 * covered / stt_duration, 1) if stt_duration else 0

    # Acceptance checklist
    a("<h3>Acceptance Checklist</h3>")
    a("<ul class='checklist'>")

    def check(ok: bool, label: str) -> str:
        cls = "pass" if ok else "fail"
        mark = "PASS" if ok else "FAIL"
        return f"<li><span class='{cls}'>[{mark}]</span> {label}</li>"

    a(check(has_both, "SourceDocument has both TRANSCRIPT and VISUAL_SCENE"))
    a(check(n_stt > 0, f"STT segments > 0 (got {n_stt})"))
    a(check(n_vd > 0, f"VD scenes > 0 (got {n_vd})"))
    a(check(coverage_pct > 90, f"Coverage > 90% (got {coverage_pct}%)"))
    a(
        check(
            strategy == "stt+vd",
            f"Strategy = stt+vd (got {strategy})",
        )
    )

    # Performance check (5-min video < 15 min)
    perf_ok = total_time < 900
    a(check(perf_ok, f"Performance: {total_time:.0f}s < 900s"))

    a("</ul>")

    # Coverage heatmap (text-based)
    a(
        f"<p>Audio duration: {stt_duration:.0f}s | "
        f"Covered: {covered:.0f}s | Coverage: {coverage_pct}%</p>"
    )

    # STT provider info
    a(
        f"<p>STT: provider={_esc(stt.get('provider', '?'))}, "
        f"model={_esc(stt.get('model_id', '?'))}, "
        f"latency={stt.get('latency_ms', 0)}ms</p>"
    )


def _section_cp5(a, alignment: dict) -> None:
    """Section 3: CP-5 — Cross-Modal Alignment."""
    a("<h2>Section 3: CP-5 — Cross-Modal Alignment</h2>")

    n_seg = alignment["total_segments"]
    n_gaps = len(alignment["coverage_gaps"])
    n_neither = sum(1 for g in alignment["coverage_gaps"] if g["gap_type"] == "neither")
    sem_cov = alignment["semantic_coverage"]
    n_conflicts = len(alignment["conflicts"])
    n_vd_orphans = len(alignment["vd_orphans"])
    n_stt_orphans = len(alignment["stt_orphans"])

    # Stats
    a("<div class='stats'>")
    for label, value in [
        ("Aligned segments", n_seg),
        ("Coverage gaps", n_gaps),
        ("Dead zones", n_neither),
        ("Semantic coverage", f"{sem_cov:.2f}"),
        ("Conflicts", n_conflicts),
        ("VD orphans", n_vd_orphans),
        ("STT orphans", n_stt_orphans),
    ]:
        a(
            f"<div class='stat-card'>"
            f"<div class='stat-value'>{value}</div>"
            f"<div class='stat-label'>{label}</div></div>"
        )
    a("</div>")

    # Acceptance checklist
    a("<h3>Acceptance Checklist</h3>")
    a("<ul class='checklist'>")

    def check(ok: bool, label: str) -> str:
        cls = "pass" if ok else "fail"
        mark = "PASS" if ok else "FAIL"
        return f"<li><span class='{cls}'>[{mark}]</span> {label}</li>"

    a(check(sem_cov > 0.5, f"Semantic coverage > 0.5 (got {sem_cov:.2f})"))
    a(check(n_neither == 0, f"Dead zones = 0 (got {n_neither})"))

    total_chunks = n_seg + n_vd_orphans + n_stt_orphans
    orphan_pct = (
        round(100 * (n_vd_orphans + n_stt_orphans) / total_chunks, 1)
        if total_chunks
        else 0
    )
    a(check(orphan_pct <= 30, f"Orphans <= 30% (got {orphan_pct}%)"))
    a("</ul>")

    # Coverage gaps
    if alignment["coverage_gaps"]:
        a("<h3>Coverage Gaps</h3>")
        a("<div class='timeline'>")
        for g in alignment["coverage_gaps"]:
            color = "#e94560" if g["gap_type"] == "neither" else "#e9a945"
            a(
                f"<div class='gap-row'>"
                f"<span class='timeline-ts'>"
                f"{g['start_sec']:.0f}-{g['end_sec']:.0f}s</span>"
                f"<span style='color:{color}'>{g['gap_type']}</span>"
                f"</div>"
            )
        a("</div>")

    # Conflicts
    if alignment["conflicts"]:
        a("<h3>Conflicts</h3>")
        for c in alignment["conflicts"]:
            a(f"<div class='conflict-item'>{_esc(c)}</div>")

    # Aligned segments (first 50 for readability)
    shown = alignment["segments"][:50]
    a(f"<h3>Aligned Segments (showing {len(shown)}/{n_seg})</h3>")
    a("<div class='timeline'>")
    a(
        "<div class='align-seg' style='font-weight:bold;color:#4ecca3'>"
        "<span>Time</span><span>STT</span>"
        "<span>VD</span><span>Conf</span></div>"
    )
    for seg in shown:
        conf = seg["alignment_confidence"]
        conf_color = "#4ecca3" if conf > 0.5 else "#e9a945"
        a("<div class='align-seg'>")
        a(
            f"<span class='timeline-ts'>"
            f"{seg['start_sec']:.0f}-{seg['end_sec']:.0f}s</span>"
        )
        a(f"<span>{_esc(seg['stt_text'][:100])}</span>")
        vd_text = seg["vd_summary"] or f"(scene {seg['vd_scene_id']})"
        a(f"<span>{_esc(vd_text[:100])}</span>")
        a(f"<span style='color:{conf_color}'>{conf:.2f}</span>")
        a("</div>")
    a("</div>")


def _union_coverage(intervals: list[tuple[float, float]]) -> float:
    """Calculate total covered seconds from a list of (start, end) intervals."""
    if not intervals:
        return 0.0
    sorted_iv = sorted(intervals)
    merged: list[tuple[float, float]] = [sorted_iv[0]]
    for start, end in sorted_iv[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum(end - start for start, end in merged)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description="VD-011 E2E Test")
    parser.add_argument(
        "video",
        nargs="?",
        default=DEFAULT_VIDEO,
        help="Path to video file",
    )
    parser.add_argument(
        "--max-sec",
        type=float,
        default=None,
        help="Limit processing to first N seconds",
    )
    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        print(f"Video not found: {video_path}")
        return

    suffix = f"_{int(args.max_sec)}s" if args.max_sec else ""
    stem = video_path.stem

    print("=" * 60)
    print(f"VD-011 E2E Test: {video_path.name}")
    if args.max_sec:
        print(f"  Limited to first {args.max_sec}s")
    print("=" * 60)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # --- STT branch ---
    stt_cache = OUT_DIR / f"stt_{stem}{suffix}.json"
    print("\n[1/3] STT Branch")
    t0 = time.monotonic()
    stt_data = await run_stt(video_path, stt_cache)
    stt_time = round(time.monotonic() - t0, 1)

    # --- VD branch ---
    vd_ckpt = OUT_DIR / f"checkpoint_{stem}{suffix}.json"
    vd_result = OUT_DIR / f"vd_{stem}{suffix}.json"
    print("\n[2/3] VD Branch")
    t1 = time.monotonic()
    vd_data = await run_vd(video_path, args.max_sec, vd_ckpt, vd_result)
    vd_time = round(time.monotonic() - t1, 1)

    total_time = round(time.monotonic() - t0, 1)

    # --- Alignment (no API calls) ---
    print("\n[3/3] Cross-Modal Alignment")
    alignment = run_alignment(stt_data, vd_data)
    print(
        f"  {alignment['total_segments']} segments, "
        f"semantic_coverage={alignment['semantic_coverage']:.2f}, "
        f"gaps={len(alignment['coverage_gaps'])}, "
        f"conflicts={len(alignment['conflicts'])}"
    )

    # --- Generate HTML report ---
    html_content = generate_html(
        stt_data=stt_data,
        vd_data=vd_data,
        alignment=alignment,
        video_name=stem,
        max_sec=args.max_sec,
        stt_time=stt_time,
        vd_time=vd_time,
        total_time=total_time,
    )
    html_path = OUT_DIR / f"e2e_report_{stem}{suffix}.html"
    html_path.write_text(html_content)

    # Save full result JSON
    full_result = {
        "video": str(video_path),
        "max_sec": args.max_sec,
        "stt": stt_data,
        "vd_stats": vd_data["stats"],
        "alignment_summary": {
            "total_segments": alignment["total_segments"],
            "semantic_coverage": alignment["semantic_coverage"],
            "coverage_gaps": len(alignment["coverage_gaps"]),
            "dead_zones": sum(
                1 for g in alignment["coverage_gaps"] if g["gap_type"] == "neither"
            ),
            "conflicts": len(alignment["conflicts"]),
            "vd_orphans": len(alignment["vd_orphans"]),
            "stt_orphans": len(alignment["stt_orphans"]),
        },
        "timing": {
            "stt_sec": stt_time,
            "vd_sec": vd_time,
            "total_sec": total_time,
        },
    }
    summary_path = OUT_DIR / f"summary_{stem}{suffix}.json"
    summary_path.write_text(json.dumps(full_result, indent=2, ensure_ascii=False))

    # --- Print summary ---
    print(f"\n{'=' * 60}")
    print("RESULTS")
    print(f"{'=' * 60}")
    print(f"  STT: {len(stt_data['segments'])} segments ({stt_time}s)")
    print(
        f"  VD:  {vd_data['stats']['processed_scenes']} scenes, "
        f"{vd_data['stats']['eyes_calls']} frames ({vd_time}s)"
    )
    print(
        f"  Alignment: {alignment['total_segments']} segments, "
        f"semantic_coverage={alignment['semantic_coverage']:.2f}"
    )
    print(f"  Total: {total_time}s")
    print(f"\n  Report: file://{html_path.resolve()}")
    print(f"  Summary: {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())
