"""CP-1: Frame Sampler verification — download videos, extract frames, generate gallery.

No LLM calls. Local-only: FFmpeg, dHash, PiP detection.

Usage:
    uv run python scripts/cp1_frame_sampler_test.py
"""

from __future__ import annotations

import asyncio
import html
import json
import subprocess
import sys
from pathlib import Path

from _utils import thumb_b64

VIDEOS = [
    {
        "url": "https://youtu.be/bDoAPEktQhg",
        "name": "video1_python_16min",
        "label": "Video 1 — Python (~16 min)",
    },
    {
        "url": "https://youtu.be/bds7jE9KThw",
        "name": "video2_style2_5min",
        "label": "Video 2 — Style 2 (~5.5 min)",
    },
    {
        "url": "https://youtu.be/ifH2h6kSzIM",
        "name": "video3_style3_13min",
        "label": "Video 3 — Style 3 (~13 min)",
    },
]

OUT_DIR = Path("tmp/cp1-test")


def download_video(url: str, name: str) -> Path:
    """Download video via yt-dlp if not already cached."""
    video_path = OUT_DIR / f"{name}.mp4"
    if video_path.exists():
        print(f"  [cached] {video_path}")
        return video_path

    print(f"  Downloading {url} ...")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "-f",
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
            "--merge-output-format",
            "mp4",
            "-o",
            str(video_path),
            "--no-playlist",
            url,
        ],
        check=True,
    )
    return video_path


def generate_gallery(
    label: str,
    result_json: dict,
    frames_dir: Path,
) -> str:
    """Generate HTML section for one video."""
    frames = result_json["frames"]
    scenes = result_json["scenes"]
    pip = result_json.get("pip_mask")
    params = result_json.get("sampling_params", {})
    resolution = result_json.get("video_resolution")

    lines: list[str] = []
    a = lines.append

    a(f"<h2>{html.escape(label)}</h2>")
    a("<div class='stats'>")
    a(f"<b>Frames:</b> {len(frames)} | ")
    a(f"<b>Scenes:</b> {len(scenes)} | ")
    res_str = f"{resolution[0]}x{resolution[1]}" if resolution else "?"
    a(f"<b>Resolution:</b> {res_str} | ")
    if pip:
        a(
            f"<b>PiP mask:</b> ({pip['x']}, {pip['y']}) "
            f"{pip['width']}x{pip['height']} "
            f"conf={pip['confidence']}"
        )
    else:
        a("<b>PiP mask:</b> None")
    a(
        f" | <b>Params:</b> fps={params.get('fps')}, "
        f"dhash_threshold={params.get('dhash_threshold')}, "
        f"gap_fill_max={params.get('gap_fill_max_sec')}s"
    )
    a("</div>")

    # Group frames by scene
    scene_map: dict[int, list[dict]] = {}
    for f in frames:
        sid = f["scene_id"]
        scene_map.setdefault(sid, []).append(f)

    for scene in scenes:
        sid = scene["scene_id"]
        scene_frames = scene_map.get(sid, [])
        dur = scene["end_sec"] - scene["start_sec"]

        a(
            f"<h3>Scene {sid} "
            f"({scene['start_sec']:.0f}s – {scene['end_sec']:.0f}s, "
            f"{dur:.0f}s, {len(scene_frames)} frames)</h3>"
        )
        a("<div class='scene'>")

        for f in scene_frames:
            fpath = frames_dir / f["filename"]
            if not fpath.exists():
                # Try in subdirs
                for sub in frames_dir.iterdir():
                    if sub.is_dir():
                        candidate = sub / f["filename"]
                        if candidate.exists():
                            fpath = candidate
                            break

            ts = f["timestamp_sec"]
            dist = f["dhash_dist"]
            cc = f["change_class"]
            src = f["source"]

            badge_class = "badge-golden" if src == "golden" else "badge-fill"
            cc_class = f"cc-{cc}"

            if fpath.exists():
                b64 = thumb_b64(fpath, max_w=320)
                img_tag = f'<img src="data:image/jpeg;base64,{b64}" />'
            else:
                img_tag = f'<div class="no-img">{f["filename"]}</div>'

            a(f"<div class='frame {cc_class}'>")
            a(img_tag)
            a(
                f"<div class='meta'>"
                f"<b>{ts:.1f}s</b> | dist={dist:.3f} | "
                f"<span class='{badge_class}'>{src}</span> | "
                f"<span class='cc'>{cc}</span>"
                f"</div>"
            )
            a("</div>")

        a("</div>")

    return "\n".join(lines)


async def run_sampler(video_path: Path, name: str) -> tuple[dict, Path]:
    """Run FrameSampler and return (result_dict, frames_dir)."""
    from course_supporter.vd.frame_sampler import FrameSampler

    output_dir = OUT_DIR / name / "frames"
    result_path = OUT_DIR / name / "result.json"

    if result_path.exists():
        print(f"  [cached] {result_path}")
        data = json.loads(result_path.read_text())
        return data, output_dir

    sampler = FrameSampler()
    result = await sampler.sample(video_path, output_dir)

    data = result.model_dump()
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f"  Result: {len(result.frames)} frames, {len(result.scenes)} scenes")
    return data, output_dir


async def main() -> None:
    print("=" * 60)
    print("CP-1: Frame Sampler Verification")
    print("=" * 60)

    # Step 1: Download videos
    print("\n--- Step 1: Download videos ---")
    video_paths: list[Path] = []
    for v in VIDEOS:
        path = download_video(v["url"], v["name"])
        video_paths.append(path)

    # Step 2: Run FrameSampler on each
    print("\n--- Step 2: Run FrameSampler ---")
    results: list[tuple[dict, Path]] = []
    for v, vpath in zip(VIDEOS, video_paths, strict=True):
        print(f"\n  Processing: {v['label']}")
        data, frames_dir = await run_sampler(vpath, v["name"])
        results.append((data, frames_dir))

    # Step 3: Generate HTML gallery
    print("\n--- Step 3: Generate HTML gallery ---")
    gallery_parts: list[str] = []
    for v, (data, frames_dir) in zip(VIDEOS, results, strict=True):
        gallery_parts.append(generate_gallery(v["label"], data, frames_dir))

    css = (
        "body{font-family:system-ui,sans-serif;margin:20px;"
        "background:#1a1a2e;color:#eee}"
        "h2{color:#e94560;border-bottom:2px solid #e94560;"
        "padding-bottom:8px}"
        "h3{color:#e94560;background:#16213e;padding:8px 12px;"
        "border-radius:6px}"
        ".stats{background:#16213e;padding:10px;"
        "border-radius:6px;margin-bottom:16px;font-size:14px}"
        ".scene{display:flex;flex-wrap:wrap;gap:12px;"
        "margin-bottom:20px}"
        ".frame{background:#16213e;border-radius:8px;"
        "padding:6px;max-width:330px}"
        ".frame img{border-radius:4px;width:320px;display:block}"
        ".meta{font-size:12px;padding:4px 2px;color:#aaa}"
        ".badge-golden{color:#4ecca3}"
        ".badge-fill{color:#e9a945}"
        ".cc{font-weight:bold}"
        ".cc-first .meta .cc{color:#4ecca3}"
        ".cc-boundary{border-left:4px solid #e94560}"
        ".cc-boundary .meta .cc{color:#e94560}"
        ".cc-medium .meta .cc{color:#e9a945}"
        ".cc-low .meta .cc{color:#666}"
        ".no-img{width:320px;height:180px;background:#333;"
        "display:flex;align-items:center;"
        "justify-content:center;border-radius:4px;"
        "font-size:12px}"
    )
    body = "".join(gallery_parts)
    gallery_html = (
        "<!DOCTYPE html><html lang='uk'><head>"
        "<meta charset='UTF-8'>"
        "<title>CP-1: Frame Sampler Gallery</title>"
        f"<style>{css}</style></head><body>"
        "<h1>CP-1: Frame Sampler Verification Gallery</h1>"
        "<p>Check: coverage, duplicates, PiP mask, "
        "scene boundaries, gap fill.</p>"
        f"{body}</body></html>"
    )

    gallery_path = OUT_DIR / "cp1_gallery.html"
    gallery_path.write_text(gallery_html)
    print(f"\n  Gallery: {gallery_path}")
    print(f"  Open: file://{gallery_path.resolve()}")

    # Summary
    print("\n--- Summary ---")
    for v, (data, _) in zip(VIDEOS, results, strict=True):
        n_frames = len(data["frames"])
        n_scenes = len(data["scenes"])
        n_fill = sum(1 for f in data["frames"] if f["source"] == "gap_fill")
        pip = data.get("pip_mask")
        pip_str = f"conf={pip['confidence']:.2f}" if pip else "None"
        print(
            f"  {v['label']}: {n_frames} frames ({n_fill} fill), "
            f"{n_scenes} scenes, PiP={pip_str}"
        )

    print("\n" + "=" * 60)
    print("Open cp1_gallery.html and follow CP-1 checklist")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
