"""Generate unified CP-2 review HTML for ultrawide monitor.

Layout per frame pair:
  [frame N][frame N+1]    — split 50/50, proportional
  [desc  N][desc  N+1]    — side by side

Usage:
    uv run python scripts/cp2_generate_review_html.py
"""

from __future__ import annotations

import html
import json
from base64 import b64encode
from pathlib import Path

OUT_PATH = Path("tmp/cp2-review.html")

# All results to combine
SOURCES = [
    {
        "label": "Video 1 — Python (A/B test, Scene 10)",
        "results_path": "tmp/cp2-ab-test/results_conditional.json",
        "video_dir": "tmp/cp1-test/video1_python_16min",
    },
    {
        "label": "Video 2 — Hardware (first frames)",
        "results_path": "tmp/cp2-quality/results_video2_style2_5min.json",
        "video_dir": "tmp/cp1-test/video2_style2_5min",
    },
    {
        "label": "Video 2 — Static scene (Scene 7, delta)",
        "results_path": "tmp/cp2-delta-quality/results_video2_style2_5min_s7.json",
        "video_dir": "tmp/cp1-test/video2_style2_5min",
    },
    {
        "label": "Video 2 — Rotating plate (Scene 12, delta)",
        "results_path": "tmp/cp2-delta-quality/results_video2_style2_5min_s12.json",
        "video_dir": "tmp/cp1-test/video2_style2_5min",
    },
    {
        "label": "Video 3 — Electronics (first frames)",
        "results_path": "tmp/cp2-quality/results_video3_style3_13min.json",
        "video_dir": "tmp/cp1-test/video3_style3_13min",
    },
    {
        "label": "Video 3 — Assembly mid-build (Scene 26, delta)",
        "results_path": "tmp/cp2-delta-quality/results_video3_style3_13min_s26.json",
        "video_dir": "tmp/cp1-test/video3_style3_13min",
    },
    {
        "label": "Video 3 — Final demo (Scene 43, delta)",
        "results_path": "tmp/cp2-delta-quality/results_video3_style3_13min_s43.json",
        "video_dir": "tmp/cp1-test/video3_style3_13min",
    },
]


def _frame_b64(img_path: Path) -> str:
    """Full-quality base64 for large display."""
    return b64encode(img_path.read_bytes()).decode()


def _find_frame(video_dir: str, filename: str) -> Path | None:
    frames_dir = Path(video_dir) / "frames"
    direct = frames_dir / filename
    if direct.exists():
        return direct
    for sub in frames_dir.iterdir():
        if sub.is_dir():
            candidate = sub / filename
            if candidate.exists():
                return candidate
    return None


def _get_filename(r: dict) -> str:
    """Get filename from result dict (different keys in different sources)."""
    return r.get("_filename", r.get("filename", ""))


def generate() -> None:
    parts: list[str] = []
    a = parts.append

    a("<!DOCTYPE html><html><head><meta charset='UTF-8'>")
    a("<title>CP-2: Full Review</title>")
    a("<style>")
    a("""
body { font-family: system-ui, sans-serif; margin: 0; padding: 20px;
       background: #0a0a1a; color: #eee; }
h1 { color: #e94560; margin: 20px 0; }
h2 { color: #4ecca3; border-bottom: 2px solid #4ecca3;
     padding: 12px 0 8px; margin: 40px 0 16px; }
.pair { display: grid; grid-template-columns: 1fr 1fr;
        gap: 4px; margin-bottom: 2px; }
.pair img { width: 100%; display: block; border-radius: 4px; }
.descs { display: grid; grid-template-columns: 1fr 1fr;
         gap: 4px; margin-bottom: 24px; }
.desc-cell { background: #16213e; border-radius: 4px;
             padding: 10px; font-size: 13px; }
.desc-header { font-size: 11px; color: #888; margin-bottom: 6px;
               display: flex; justify-content: space-between; }
.badge-delta { background: #e94560; color: #fff; padding: 1px 5px;
               border-radius: 3px; font-size: 10px; }
.badge-full { background: #4ecca3; color: #000; padding: 1px 5px;
              border-radius: 3px; font-size: 10px; }
.response { white-space: pre-wrap; max-height: 500px;
            overflow-y: auto; line-height: 1.4; }
.no-img { background: #333; display: flex; align-items: center;
          justify-content: center; min-height: 200px;
          border-radius: 4px; color: #666; }
""")
    a("</style></head><body>")
    a("<h1>CP-2: Visual Analyzer Quality Review</h1>")
    a("<p>Model: gemini-2.5-flash | Strategy: CONDITIONAL</p>")
    a("<p>Layout: [frame N][frame N+1] / [description N][description N+1]</p>")

    for src in SOURCES:
        rpath = Path(src["results_path"])
        if not rpath.exists():
            continue

        results = json.loads(rpath.read_text())
        if not results:
            continue

        # Resolve filenames
        for r in results:
            if "_filename" not in r:
                # For AB test results — filename is in CP1 data
                data = json.loads(
                    (Path(src["video_dir"]) / "result.json").read_text(),
                )
                fmap = {f["frame_id"]: f["filename"] for f in data["frames"]}
                for rr in results:
                    rr["_filename"] = fmap.get(rr["frame_id"], "")
                break

        a(f"<h2>{html.escape(src['label'])} ({len(results)} frames)</h2>")

        # Generate pairs: (0,1), (1,2), (2,3), ...
        for i in range(len(results) - 1):
            r1 = results[i]
            r2 = results[i + 1]

            f1 = _find_frame(src["video_dir"], _get_filename(r1))
            f2 = _find_frame(src["video_dir"], _get_filename(r2))

            # Images row
            a("<div class='pair'>")
            if f1:
                a(f"<img src='data:image/jpeg;base64,{_frame_b64(f1)}'/>")
            else:
                a(f"<div class='no-img'>{_get_filename(r1)}</div>")
            if f2:
                a(f"<img src='data:image/jpeg;base64,{_frame_b64(f2)}'/>")
            else:
                a(f"<div class='no-img'>{_get_filename(r2)}</div>")
            a("</div>")

            # Descriptions row
            a("<div class='descs'>")
            for r in [r1, r2]:
                badge = (
                    "<span class='badge-delta'>DELTA</span>"
                    if r.get("is_delta")
                    else "<span class='badge-full'>FULL</span>"
                )
                a("<div class='desc-cell'>")
                a(
                    f"<div class='desc-header'>"
                    f"<span>{r['frame_id']} ({r['timestamp_sec']:.0f}s) "
                    f"{badge}</span>"
                    f"<span>{r.get('latency_sec', 0)}s | "
                    f"out={r.get('output_tokens', 0)}</span>"
                    f"</div>"
                )
                resp = html.escape(r.get("response", ""))
                a(f"<div class='response'>{resp}</div>")
                a("</div>")
            a("</div>")

        # Last frame standalone (if only 1 frame or show the last one)
        if len(results) == 1:
            r = results[0]
            f = _find_frame(src["video_dir"], _get_filename(r))
            a("<div class='pair'>")
            if f:
                a(f"<img src='data:image/jpeg;base64,{_frame_b64(f)}'/>")
            a("<div></div>")
            a("</div>")
            badge = (
                "<span class='badge-delta'>DELTA</span>"
                if r.get("is_delta")
                else "<span class='badge-full'>FULL</span>"
            )
            a("<div class='descs'>")
            a("<div class='desc-cell'>")
            a(
                f"<div class='desc-header'>"
                f"<span>{r['frame_id']} {badge}</span>"
                f"<span>out={r.get('output_tokens', 0)}</span>"
                f"</div>"
            )
            resp = html.escape(r.get("response", ""))
            a(f"<div class='response'>{resp}</div>")
            a("</div><div></div></div>")

    a("</body></html>")

    OUT_PATH.write_text("".join(parts))
    print(f"Generated: file://{OUT_PATH.resolve()}")
    print(f"Size: {OUT_PATH.stat().st_size / 1024 / 1024:.1f} MB")


if __name__ == "__main__":
    generate()
