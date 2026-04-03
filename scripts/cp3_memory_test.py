"""CP-3: Streaming Memory Pipeline verification on CP-2 delta results.

Runs MemoryPipeline.process_scene() on two scenes (Video 3 s26 + s43),
showing streaming updates: instant per frame, scene per frame, video per scene.

LLM calls: ~1 per frame (scene memory) + 1 per scene (video memory).
4 frames x 2 scenes = ~10 LLM calls total.

Usage:
    uv run python scripts/cp3_memory_test.py
"""

from __future__ import annotations

import asyncio
import html
import json
import os
from pathlib import Path

from _utils import load_env

OUT_DIR = Path("tmp/cp3-memory")

TESTS = [
    {
        "video": "video3_style3_13min",
        "label": "Video 3 — Assembly (mid-build)",
        "scene_id": 26,
        "results_path": "tmp/cp2-delta-quality/results_video3_style3_13min_s26.json",
    },
    {
        "video": "video3_style3_13min",
        "label": "Video 3 — Final demo",
        "scene_id": 43,
        "results_path": "tmp/cp2-delta-quality/results_video3_style3_13min_s43.json",
    },
]


async def run_memory_pipeline(
    tests: list[dict],
) -> list[dict]:
    """Run streaming MemoryPipeline on each test scene."""
    from course_supporter.key_pool import KeyPool
    from course_supporter.vd.memory_pipeline import (
        MemoryPipeline,
        build_instant_memory,
    )
    from course_supporter.vd.schemas import (
        EyesResult,
        SceneMemory,
        VideoMemory,
    )

    load_env()
    raw_keys = os.environ.get("GEMINI_API_KEY", "")
    key_pool = KeyPool(raw_keys)

    pipeline = MemoryPipeline(
        key_pool,
        model="gemini-2.5-flash",
        rpm_per_key=5,
    )

    video_memory = VideoMemory()
    previous_scene_mem: SceneMemory | None = None
    all_results: list[dict] = []

    for t in tests:
        label = t["label"]
        scene_id = t["scene_id"]

        print(f"\n  [{label}] scene {scene_id}...")

        # Load CP-2 Eyes results
        cp2_results = json.loads(Path(t["results_path"]).read_text())  # noqa: ASYNC240
        eyes_results = [EyesResult(**r) for r in cp2_results]

        # Track per-frame evolution
        frame_snapshots: list[dict] = []
        instant = None
        scene_memory = SceneMemory(
            scene_id=scene_id,
            previous_scene_summary=(
                previous_scene_mem.summary[:300] if previous_scene_mem else ""
            ),
        )

        for i, eyes in enumerate(eyes_results):
            instant = build_instant_memory(eyes, instant)

            print(
                f"    Frame {i + 1}/{len(eyes_results)}: "
                f"{eyes.frame_id} ({'DELTA' if eyes.is_delta else 'FULL'})...",
                end=" ",
                flush=True,
            )

            scene_memory = await pipeline.update_scene_memory(
                instant,
                scene_memory,
            )
            print(f"OK (scenes_seen={scene_memory.frames_seen})")

            frame_snapshots.append(
                {
                    "frame_id": eyes.frame_id,
                    "timestamp_sec": eyes.timestamp_sec,
                    "is_delta": eyes.is_delta,
                    "instant": {
                        "current": instant.current,
                        "previous": instant.previous,
                    },
                    "scene_memory_snapshot": {
                        "summary": scene_memory.summary,
                        "scene_type": scene_memory.scene_type,
                        "topics": scene_memory.topics,
                        "importance": scene_memory.importance,
                        "frames_seen": scene_memory.frames_seen,
                    },
                }
            )

        # Update video memory once per scene
        print("    Updating video memory...", end=" ", flush=True)
        video_memory = await pipeline.update_video_memory(
            scene_memory,
            video_memory,
            previous_scene_mem,
        )
        print(f"OK ({len(video_memory.text)} chars)")

        result = {
            "label": label,
            "scene_id": scene_id,
            "frame_count": len(eyes_results),
            "frame_snapshots": frame_snapshots,
            "final_scene_memory": {
                "summary": scene_memory.summary,
                "scene_type": scene_memory.scene_type,
                "topics": scene_memory.topics,
                "importance": scene_memory.importance,
                "frames_seen": scene_memory.frames_seen,
            },
            "video_memory": {
                "text": video_memory.text,
                "scenes_processed": video_memory.scenes_processed,
            },
        }
        all_results.append(result)
        previous_scene_mem = scene_memory

    return all_results


def generate_html(results: list[dict]) -> str:
    """Generate CP-3 review HTML showing streaming memory evolution."""
    parts: list[str] = []
    a = parts.append

    a("<!DOCTYPE html><html><head><meta charset='UTF-8'>")
    a("<title>CP-3: Streaming Memory Review</title>")
    a("<style>")
    a("""
body { font-family: system-ui, sans-serif; margin: 0; padding: 20px;
       background: #0a0a1a; color: #eee; max-width: 1400px; margin: 0 auto; }
h1 { color: #e94560; }
h2 { color: #4ecca3; border-bottom: 2px solid #4ecca3;
     padding: 8px 0; margin-top: 40px; }
h3 { color: #e9a945; margin-top: 24px; }
.frame-step { background: #16213e; border-radius: 8px;
              padding: 16px; margin: 12px 0;
              border-left: 3px solid #3a7bd5; }
.frame-step.delta { border-left-color: #e94560; }
.level-label { font-size: 11px; text-transform: uppercase;
               color: #888; margin: 12px 0 4px; letter-spacing: 1px; }
.badge { padding: 2px 8px; border-radius: 4px; font-size: 12px;
         font-weight: bold; display: inline-block; }
.badge-instant { background: #3a7bd5; color: #fff; }
.badge-scene { background: #e94560; color: #fff; }
.badge-video { background: #4ecca3; color: #000; }
.badge-delta { background: #e94560; color: #fff; font-size: 10px; }
.badge-full { background: #4ecca3; color: #000; font-size: 10px; }
.content { white-space: pre-wrap; background: #0f3460; padding: 10px;
           border-radius: 4px; font-size: 13px; max-height: 300px;
           overflow-y: auto; margin: 4px 0 8px; line-height: 1.5; }
.content-short { background: #1a3050; padding: 8px;
                 border-radius: 4px; font-size: 13px;
                 margin: 4px 0; font-style: italic; }
.meta { font-size: 12px; color: #888; }
.topics { display: flex; gap: 6px; flex-wrap: wrap; margin: 4px 0; }
.topic { background: #2a4a7f; padding: 1px 6px; border-radius: 10px;
         font-size: 11px; }
.video-block { background: #0d2137; border: 2px solid #4ecca3;
               border-radius: 8px; padding: 16px; margin: 20px 0; }
.arrow { color: #4ecca3; font-size: 18px; text-align: center;
         margin: 4px 0; }
""")
    a("</style></head><body>")
    a("<h1>CP-3: Streaming Memory Pipeline</h1>")
    a(
        "<p>Model: gemini-2.5-flash | Architecture: "
        "Instant (per frame, no LLM) → Scene (per frame, LLM) → "
        "Video (per scene, LLM)</p>"
    )

    for r in results:
        a(
            f"<h2>{html.escape(r['label'])} — Scene {r['scene_id']} "
            f"({r['frame_count']} frames)</h2>"
        )

        for snap in r["frame_snapshots"]:
            delta_cls = " delta" if snap["is_delta"] else ""
            badge = (
                "<span class='badge badge-delta'>DELTA</span>"
                if snap["is_delta"]
                else "<span class='badge badge-full'>FULL</span>"
            )

            a(f"<div class='frame-step{delta_cls}'>")
            a(f"<h3>{snap['frame_id']} ({snap['timestamp_sec']:.0f}s) {badge}</h3>")

            # Instant Memory
            a(
                "<div class='level-label'>"
                "<span class='badge badge-instant'>Instant</span> "
                "Rolling 2-frame window</div>"
            )
            inst = snap["instant"]
            a(
                f"<div class='content-short'>Now: "
                f"{html.escape(inst['current'][:200])}</div>"
            )
            if inst["previous"]:
                a(
                    f"<div class='content-short' style='color:#999'>Prev: "
                    f"{html.escape(inst['previous'][:200])}</div>"
                )

            # Scene Memory snapshot
            a(
                "<div class='level-label'>"
                "<span class='badge badge-scene'>Scene</span> "
                f"After frame {snap['scene_memory_snapshot']['frames_seen']}"
                "</div>"
            )
            sm = snap["scene_memory_snapshot"]
            a(f"<div class='content-short'>{html.escape(sm['summary'])}</div>")
            if sm["topics"]:
                a("<div class='topics'>")
                for t in sm["topics"]:
                    a(f"<span class='topic'>{html.escape(t)}</span>")
                a("</div>")
            a(
                f"<div class='meta'>type={sm['scene_type']} "
                f"importance={sm['importance']}</div>"
            )

            a("</div>")  # frame-step
            a("<div class='arrow'>↓</div>")

        # Video Memory (after scene)
        vm = r["video_memory"]
        a("<div class='video-block'>")
        a(
            "<div class='level-label'>"
            "<span class='badge badge-video'>Video Memory</span> "
            f"After scene {r['scene_id']} "
            f"({vm['scenes_processed']} scenes total)</div>"
        )
        a(f"<div class='content'>{html.escape(vm['text'])}</div>")
        a("</div>")

    a("</body></html>")
    return "".join(parts)


async def main() -> None:
    print("=" * 60)
    print("CP-3: Streaming Memory Pipeline Verification")
    print("=" * 60)

    OUT_DIR.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240

    cache_path = OUT_DIR / "results_streaming.json"
    if cache_path.exists():
        print("\n  Cached. Delete tmp/cp3-memory/results_streaming.json to rerun.")
        results = json.loads(cache_path.read_text())
    else:
        results = await run_memory_pipeline(TESTS)
        cache_path.write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
        )

    html_content = generate_html(results)
    html_path = OUT_DIR / "memory_streaming_review.html"
    html_path.write_text(html_content)
    print(f"\n\nReview: file://{html_path.resolve()}")

    # Summary
    print("\n--- Summary ---")
    for r in results:
        sm = r["final_scene_memory"]
        print(
            f"  Scene {r['scene_id']}: {sm['frames_seen']} frames, "
            f"type={sm['scene_type']}, importance={sm['importance']}"
        )
        print(f"    Topics: {sm['topics']}")
        print(f"    Summary: {sm['summary'][:100]}...")


if __name__ == "__main__":
    asyncio.run(main())
