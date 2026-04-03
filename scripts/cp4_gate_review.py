"""CP-4: Gate Review — run full VDPipeline on a video, generate HTML report.

Usage:
    uv run python scripts/cp4_gate_review.py [--max-sec 300]

Set --max-sec to limit processing to first N seconds of video.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import os
import time
from pathlib import Path

from _utils import find_frame, load_env, thumb_b64

OUT_DIR = Path("tmp/cp4-gate")

# Default test video
DEFAULT_VIDEO = "tmp/cp1-test/video1_python_16min.mp4"


async def run_pipeline(
    video_path: Path,
    max_sec: float | None = None,
) -> dict:
    """Run VDPipeline and return serialized result + stats."""
    from course_supporter.key_pool import KeyPool
    from course_supporter.vd.frame_sampler import FrameSampler
    from course_supporter.vd.memory_pipeline import MemoryPipeline
    from course_supporter.vd.schemas import SamplingParams, SceneMemory, VideoMemory
    from course_supporter.vd.visual_analyzer import VisualAnalyzer

    load_env()
    raw_keys = os.environ.get("GEMINI_API_KEY", "")
    key_pool = KeyPool(raw_keys)

    # Components — rpm_per_key=2 to stay within shared quota
    sampler = FrameSampler(SamplingParams())
    memory = MemoryPipeline(key_pool, model="gemini-2.5-flash", rpm_per_key=2)
    analyzer = VisualAnalyzer(
        key_pool,
        model="gemini-2.5-flash",
        rpm_per_key=2,
        memory=memory,
    )

    # Stage A: Sample frames
    print("\n  Stage A: Sampling frames...")
    t0 = time.monotonic()

    import tempfile

    temp_dir = Path(tempfile.mkdtemp(prefix="cp4_"))
    try:
        sampling = await sampler.sample(video_path, temp_dir)
        stage_a_sec = round(time.monotonic() - t0, 1)
        print(
            f"    {len(sampling.frames)} frames, "
            f"{len(sampling.scenes)} scenes, {stage_a_sec}s"
        )

        # Filter scenes by max_sec if set
        scenes = sampling.scenes
        if max_sec is not None:
            scenes = [s for s in scenes if s.start_sec < max_sec]
            print(f"    Filtered to {len(scenes)} scenes (max_sec={max_sec})")

        # Stage B: Analyze with streaming memory (with per-scene checkpoint)
        print("\n  Stage B: Visual analysis + memory...")
        t1 = time.monotonic()

        # Resume from checkpoint if exists
        suffix = f"_{int(max_sec)}s" if max_sec else ""
        ckpt_path = OUT_DIR / f"checkpoint_{video_path.stem}{suffix}.json"
        scene_results: list[dict] = []
        video_memory = VideoMemory()
        previous_scene: SceneMemory | None = None
        total_eyes_calls = 0
        total_memory_calls = 0
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
            total_eyes_calls = sum(len(s["frames"]) for s in scene_results)
            total_memory_calls = sum(
                s["scene_memory"]["frames_seen"] + 1 for s in scene_results
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
            total_eyes_calls += len(eyes_results)

            resolved = final_scene_mem or scene_memory

            # Update video memory
            video_memory = await memory.update_video_memory(
                resolved,
                video_memory,
                previous_scene,
            )
            total_memory_calls += len(scene_frames) + 1  # scene updates + video update

            n_delta = sum(1 for r in eyes_results if r.is_delta)
            print(
                f"OK ({len(eyes_results)} eyes, {n_delta} delta, "
                f"type={resolved.scene_type})"
            )

            # Collect frame thumbnails
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

            # Save checkpoint after each scene
            ckpt_path.write_text(
                json.dumps(
                    {
                        "video_memory": video_memory.text,
                        "scenes": scene_results,
                    },
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

    return {
        "video": str(video_path),
        "max_sec": max_sec,
        "stats": {
            "total_frames": len(sampling.frames),
            "total_scenes": len(sampling.scenes),
            "processed_scenes": len(scene_results),
            "eyes_calls": total_eyes_calls,
            "memory_calls": total_memory_calls,
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "stage_a_sec": stage_a_sec,
            "stage_b_sec": stage_b_sec,
            "total_sec": total_sec,
        },
        "video_memory": video_memory.text,
        "scenes": scene_results,
    }


def generate_html(result: dict) -> str:
    """Generate CP-4 Gate Review HTML report."""
    parts: list[str] = []
    a = parts.append

    stats = result["stats"]
    video_name = Path(result["video"]).stem

    a("<!DOCTYPE html><html><head><meta charset='UTF-8'>")
    a(f"<title>CP-4: Gate Review — {video_name}</title>")
    a("<style>")
    a("""
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
""")
    a("</style></head><body>")

    # Header
    a(f"<h1>CP-4: Gate Review — {html.escape(video_name)}</h1>")
    max_note = f" (first {result['max_sec']}s)" if result.get("max_sec") else ""
    a(f"<p>Model: gemini-2.5-flash | Strategy: CONDITIONAL{max_note}</p>")

    # Stats
    a("<div class='stats'>")
    for label, value in [
        ("Scenes", stats["processed_scenes"]),
        ("Frames analyzed", stats["eyes_calls"]),
        ("LLM calls", stats["eyes_calls"] + stats["memory_calls"]),
        ("Tokens in", f"{stats['total_tokens_in']:,}"),
        ("Tokens out", f"{stats['total_tokens_out']:,}"),
        ("Stage A", f"{stats['stage_a_sec']}s"),
        ("Stage B", f"{stats['stage_b_sec']}s"),
        ("Total time", f"{stats['total_sec']}s"),
    ]:
        a(
            f"<div class='stat-card'>"
            f"<div class='stat-value'>{value}</div>"
            f"<div class='stat-label'>{label}</div></div>"
        )
    a("</div>")

    # Timeline
    a("<h2>Timeline</h2>")
    a("<div class='timeline'>")
    for s in result["scenes"]:
        sm = s["scene_memory"]
        imp_color = "#e94560" if sm["importance"] >= 4 else "#888"
        a("<div class='timeline-row'>")
        a(f"<span class='timeline-ts'>{s['start_sec']:.0f}–{s['end_sec']:.0f}s</span>")
        a(
            f"<span class='timeline-type'>"
            f"<span class='badge badge-type'>{sm['scene_type']}</span></span>"
        )
        a(f"<span style='color:{imp_color}'>★{sm['importance']}</span>")
        a(f"<span>{html.escape(sm['summary'][:120])}</span>")
        a("</div>")
    a("</div>")

    # Video Memory
    a("<h2>Final Video Memory</h2>")
    a(f"<div class='video-memory'>{html.escape(result['video_memory'])}</div>")

    # Scene details
    a("<h2>Scene Details</h2>")
    for s in result["scenes"]:
        sm = s["scene_memory"]
        n_delta = sum(1 for f in s["frames"] if f["is_delta"])

        a("<div class='scene-block'>")
        a("<div class='scene-header'>")
        a(f"<h3>Scene {s['scene_id']} ({s['start_sec']:.0f}–{s['end_sec']:.0f}s)</h3>")
        a(
            f"<span>"
            f"<span class='badge badge-type'>{sm['scene_type']}</span> "
            f"<span class='badge badge-imp'>★{sm['importance']}</span> "
            f"{s['n_frames']} frames ({n_delta} delta)"
            f"</span>"
        )
        a("</div>")

        a(f"<div class='summary'>{html.escape(sm['summary'])}</div>")

        if sm["topics"]:
            a("<div class='topics'>")
            for t in sm["topics"]:
                a(f"<span class='topic'>{html.escape(t)}</span>")
            a("</div>")

        # Frame grid
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
                f"{f['frame_id']} ({f['timestamp_sec']:.0f}s) {badge} | "
                f"{f['latency_sec']}s | "
                f"in={f['tokens_in']} out={f['tokens_out']}"
                f"</div>"
            )
            a(f"<div class='response'>{html.escape(f['response'])}</div>")
            a("</div>")
        a("</div>")  # frames-grid

        a("</div>")  # scene-block

    a("</body></html>")
    return "".join(parts)


async def main() -> None:
    parser = argparse.ArgumentParser(description="CP-4 Gate Review")
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
    if not video_path.exists():  # noqa: ASYNC240
        print(f"Video not found: {video_path}")
        return

    print("=" * 60)
    print(f"CP-4: Gate Review — {video_path.name}")
    if args.max_sec:
        print(f"  Limited to first {args.max_sec}s")
    print("=" * 60)

    OUT_DIR.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240

    suffix = f"_{int(args.max_sec)}s" if args.max_sec else ""
    cache_path = OUT_DIR / f"result_{video_path.stem}{suffix}.json"

    if cache_path.exists():
        print(f"\n  Cached. Delete {cache_path} to rerun.")
        result = json.loads(cache_path.read_text())
    else:
        result = await run_pipeline(video_path, max_sec=args.max_sec)
        cache_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False),
        )

    html_content = generate_html(result)
    html_path = OUT_DIR / f"review_{video_path.stem}{suffix}.html"
    html_path.write_text(html_content)

    print(f"\n\nReview: file://{html_path.resolve()}")
    print("\n--- Stats ---")
    s = result["stats"]
    print(f"  Scenes: {s['processed_scenes']}")
    print(f"  Frames: {s['eyes_calls']}")
    print(f"  Time: {s['total_sec']}s (A: {s['stage_a_sec']}s, B: {s['stage_b_sec']}s)")
    print(f"  Tokens: {s['total_tokens_in']:,} in, {s['total_tokens_out']:,} out")


if __name__ == "__main__":
    asyncio.run(main())
