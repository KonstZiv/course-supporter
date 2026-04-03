"""CP-2: Quality test — run CONDITIONAL Eyes on diverse frames from Video 2+3.

Picks 5 frames per video (different content types), runs VisualAnalyzer
with CONDITIONAL strategy, generates HTML for human quality review.

Usage:
    uv run python scripts/cp2_quality_test.py
"""

from __future__ import annotations

import asyncio
import html
import json
import os
from pathlib import Path

from _utils import find_frame, load_env, thumb_b64

OUT_DIR = Path("tmp/cp2-quality")

# 5 frames from each video — spread across different scenes
TEST_CONFIGS = [
    {
        "video": "video2_style2_5min",
        "label": "Video 2 — Hardware review",
        "scene_ids": [0, 7, 12, 15, 18],
        "frame_pick": "first",  # pick first frame of each scene
    },
    {
        "video": "video3_style3_13min",
        "label": "Video 3 — Electronics assembly",
        "scene_ids": [0, 4, 26, 31, 43],
        "frame_pick": "first",
    },
]


async def analyze_frames(
    video_name: str,
    scene_ids: list[int],
) -> list[dict]:
    """Run CONDITIONAL Eyes on first frame of each specified scene."""
    from course_supporter.key_pool import KeyPool
    from course_supporter.vd.schemas import (
        DeltaStrategy,
        SampledFrame,
        Scene,
    )
    from course_supporter.vd.visual_analyzer import VisualAnalyzer

    load_env()
    raw_keys = os.environ.get("GEMINI_API_KEY", "")
    key_pool = KeyPool(raw_keys)

    analyzer = VisualAnalyzer(
        key_pool,
        model="gemini-2.5-flash",
        rpm_per_key=5,
        delta_strategy=DeltaStrategy.CONDITIONAL,
    )

    cp1_dir = Path(f"tmp/cp1-test/{video_name}")
    data = json.loads((cp1_dir / "result.json").read_text())
    frame_dir = cp1_dir / "frames"

    results: list[dict] = []

    for sid in scene_ids:
        scene_data = next(s for s in data["scenes"] if s["scene_id"] == sid)
        scene_frames_data = [f for f in data["frames"] if f["scene_id"] == sid]

        if not scene_frames_data:
            continue

        # Take first frame only (for quality check, not delta test)
        first_frame_data = scene_frames_data[0]
        scene = Scene(**scene_data)
        frame = SampledFrame(**first_frame_data)

        print(
            f"    Scene {sid}: {frame.frame_id} ({frame.timestamp_sec:.0f}s)...",
            end=" ",
            flush=True,
        )

        # Analyze single frame (no delta — first frame always full)
        scene_results, _ = await analyzer.analyze_scene(
            scene,
            [frame],
            frame_dir,
        )

        r = scene_results[0].model_dump()
        r["_video"] = video_name
        r["_scene_id"] = sid
        r["_filename"] = first_frame_data["filename"]
        results.append(r)

        print(f"OK ({r['latency_sec']}s, out={r['output_tokens']} tok)")

        await asyncio.sleep(2)

    return results


def generate_html(
    all_results: dict[str, list[dict]],
) -> str:
    """Generate HTML for quality review."""
    parts: list[str] = []
    a = parts.append

    a("<!DOCTYPE html><html><head><meta charset='UTF-8'>")
    a("<title>CP-2: Quality Review</title>")
    a("<style>")
    a("body{font-family:system-ui;margin:20px;background:#1a1a2e;color:#eee}")
    a("h1{color:#e94560}h2{color:#4ecca3}h3{color:#e9a945}")
    a(".frame-block{background:#16213e;border-radius:8px;")
    a("padding:16px;margin:16px 0}")
    a(".meta{font-size:12px;color:#888;margin:8px 0}")
    a(".response{font-size:13px;white-space:pre-wrap;")
    a("background:#0f3460;padding:12px;border-radius:4px;")
    a("max-height:600px;overflow-y:auto}")
    a("img{border-radius:4px;max-width:700px;display:block;margin:8px 0}")
    a("</style></head><body>")
    a("<h1>CP-2: Visual Analyzer Quality Review</h1>")
    a("<p>Model: gemini-2.5-flash | Strategy: CONDITIONAL</p>")
    a("<p>Check: completeness, accuracy, hallucinations</p>")

    for video_label, results in all_results.items():
        a(f"<h2>{html.escape(video_label)}</h2>")

        for r in results:
            video_name = r["_video"]
            sid = r["_scene_id"]
            fname = r["_filename"]
            frame_dir = Path(f"tmp/cp1-test/{video_name}/frames")
            fpath = find_frame(frame_dir, fname)

            a("<div class='frame-block'>")
            a(f"<h3>Scene {sid}: {r['frame_id']} ({r['timestamp_sec']:.0f}s)</h3>")

            if fpath:
                b64 = thumb_b64(fpath, 700)
                a(f"<img src='data:image/jpeg;base64,{b64}'/>")

            a(
                f"<div class='meta'>"
                f"Latency: {r['latency_sec']}s | "
                f"Tokens in: {r['input_tokens']} | "
                f"Tokens out: {r['output_tokens']} | "
                f"Delta: {r['is_delta']}</div>"
            )

            resp = html.escape(r.get("response", ""))
            a(f"<div class='response'>{resp}</div>")
            a("</div>")

    a("</body></html>")
    return "".join(parts)


async def main() -> None:
    print("=" * 60)
    print("CP-2: Quality Test (Video 2 + Video 3)")
    print("=" * 60)

    OUT_DIR.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    all_results: dict[str, list[dict]] = {}

    for cfg in TEST_CONFIGS:
        video = cfg["video"]
        label = cfg["label"]
        cache_path = OUT_DIR / f"results_{video}.json"

        if cache_path.exists():
            print(f"\n  [{label}] cached")
            all_results[label] = json.loads(cache_path.read_text())
            continue

        print(f"\n  [{label}]")
        results = await analyze_frames(video, cfg["scene_ids"])
        all_results[label] = results
        cache_path.write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
        )

    # Generate HTML
    html_content = generate_html(all_results)
    html_path = OUT_DIR / "quality_review.html"
    html_path.write_text(html_content)

    print(f"\n\nReview: file://{html_path.resolve()}")

    # Summary
    print("\n--- Summary ---")
    for label, results in all_results.items():
        total_out = sum(r["output_tokens"] for r in results)
        avg_lat = sum(r["latency_sec"] for r in results) / len(results)
        print(
            f"  {label}: {len(results)} frames, {total_out} tok out, avg {avg_lat:.1f}s"
        )


if __name__ == "__main__":
    asyncio.run(main())
