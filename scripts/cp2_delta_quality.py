"""CP-2: Delta quality test — run CONDITIONAL on multi-frame scenes.

Tests whether CONDITIONAL delta produces quality results when
frames are similar (mid-scene). Picks scenes with 5+ frames,
runs 4 consecutive frames from each.

Usage:
    uv run python scripts/cp2_delta_quality.py
"""

from __future__ import annotations

import asyncio
import html
import json
import os
from pathlib import Path

from _utils import find_frame, load_env, thumb_b64

OUT_DIR = Path("tmp/cp2-delta-quality")

# Scenes to test: multi-frame, mid-scene for delta
TESTS = [
    {
        "video": "video2_style2_5min",
        "label": "Video 2 — Static scene (monitor)",
        "scene_id": 7,
        "frame_slice": (0, 4),  # first 4 frames
    },
    {
        "video": "video2_style2_5min",
        "label": "Video 2 — Rotating plate",
        "scene_id": 12,
        "frame_slice": (0, 4),
    },
    {
        "video": "video3_style3_13min",
        "label": "Video 3 — Assembly (mid-build)",
        "scene_id": 26,
        "frame_slice": (10, 14),  # mid-scene
    },
    {
        "video": "video3_style3_13min",
        "label": "Video 3 — Final demo",
        "scene_id": 43,
        "frame_slice": (0, 4),
    },
]


async def run_scene_slice(
    video_name: str,
    scene_id: int,
    frame_slice: tuple[int, int],
) -> list[dict]:
    """Run CONDITIONAL analyzer on a slice of frames from a scene."""
    from course_supporter.key_pool import KeyPool
    from course_supporter.vd.schemas import DeltaStrategy, SampledFrame, Scene
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

    scene_data = next(s for s in data["scenes"] if s["scene_id"] == scene_id)
    all_scene_frames = [f for f in data["frames"] if f["scene_id"] == scene_id]

    start, end = frame_slice
    selected = all_scene_frames[start:end]

    scene = Scene(**scene_data)
    frames = [SampledFrame(**f) for f in selected]

    results, _ = await analyzer.analyze_scene(scene, frames, frame_dir)

    out: list[dict] = []
    for r, fd in zip(results, selected, strict=True):
        d = r.model_dump()
        d["_filename"] = fd["filename"]
        d["_video"] = video_name
        out.append(d)

    return out


def generate_html(all_results: dict[str, list[dict]]) -> str:
    parts: list[str] = []
    a = parts.append

    a("<!DOCTYPE html><html><head><meta charset='UTF-8'>")
    a("<title>CP-2: Delta Quality</title>")
    a("<style>")
    a("body{font-family:system-ui;margin:20px;background:#1a1a2e;color:#eee}")
    a("h1{color:#e94560}h2{color:#4ecca3}h3{color:#e9a945}")
    a(".frame-block{background:#16213e;border-radius:8px;")
    a("padding:16px;margin:12px 0}")
    a(".meta{font-size:12px;color:#888;margin:6px 0}")
    a(".response{font-size:13px;white-space:pre-wrap;")
    a("background:#0f3460;padding:10px;border-radius:4px;")
    a("max-height:500px;overflow-y:auto}")
    a("img{border-radius:4px;max-width:600px;display:block;margin:6px 0}")
    a(".delta-badge{background:#e94560;color:#fff;padding:2px 6px;")
    a("border-radius:4px;font-size:11px;font-weight:bold}")
    a(".full-badge{background:#4ecca3;color:#000;padding:2px 6px;")
    a("border-radius:4px;font-size:11px;font-weight:bold}")
    a("</style></head><body>")
    a("<h1>CP-2: Delta Quality Review</h1>")
    a("<p>CONDITIONAL strategy — LLM decides full vs delta</p>")

    for label, results in all_results.items():
        a(f"<h2>{html.escape(label)}</h2>")
        total_out = sum(r["output_tokens"] for r in results)
        n_delta = sum(1 for r in results if r["is_delta"])
        a(f"<p>{len(results)} frames, {total_out} tokens out, {n_delta} deltas</p>")

        for r in results:
            video = r["_video"]
            fname = r["_filename"]
            frame_dir = Path(f"tmp/cp1-test/{video}/frames")
            fpath = find_frame(frame_dir, fname)

            badge = (
                "<span class='delta-badge'>DELTA</span>"
                if r["is_delta"]
                else "<span class='full-badge'>FULL</span>"
            )

            a("<div class='frame-block'>")
            a(f"<h3>{r['frame_id']} ({r['timestamp_sec']:.0f}s) {badge}</h3>")

            if fpath:
                b64 = thumb_b64(fpath, 600)
                a(f"<img src='data:image/jpeg;base64,{b64}'/>")

            a(
                f"<div class='meta'>"
                f"{r['latency_sec']}s | "
                f"in={r['input_tokens']} out={r['output_tokens']}"
                f"</div>"
            )

            resp = html.escape(r.get("response", ""))
            a(f"<div class='response'>{resp}</div>")
            a("</div>")

    a("</body></html>")
    return "".join(parts)


async def main() -> None:
    print("=" * 60)
    print("CP-2: Delta Quality Test")
    print("=" * 60)

    OUT_DIR.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    all_results: dict[str, list[dict]] = {}

    for t in TESTS:
        label = t["label"]
        cache = OUT_DIR / f"results_{t['video']}_s{t['scene_id']}.json"

        if cache.exists():
            print(f"\n  [{label}] cached")
            all_results[label] = json.loads(cache.read_text())
            continue

        print(f"\n  [{label}] scene {t['scene_id']}...")
        results = await run_scene_slice(
            t["video"],
            t["scene_id"],
            t["frame_slice"],
        )
        all_results[label] = results
        cache.write_text(json.dumps(results, indent=2, ensure_ascii=False))

        total = sum(r["output_tokens"] for r in results)
        deltas = sum(1 for r in results if r["is_delta"])
        print(f"    {total} tokens, {deltas} deltas")

    html_content = generate_html(all_results)
    html_path = OUT_DIR / "delta_quality.html"
    html_path.write_text(html_content)
    print(f"\n\nReview: file://{html_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
