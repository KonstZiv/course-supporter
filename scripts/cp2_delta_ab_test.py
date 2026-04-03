"""CP-2: A/B test — compare 3 delta strategies on the same scene.

Runs VisualAnalyzer with NONE, EXPLICIT, CONDITIONAL strategies
on Scene 10 of Video 1 (8 frames, 350-392s). Generates HTML comparison.

Usage:
    uv run python scripts/cp2_delta_ab_test.py
"""

from __future__ import annotations

import asyncio
import html
import json
import os
from base64 import b64encode
from pathlib import Path

VIDEO_NAME = "video1_python_16min"
SCENE_ID = 10
OUT_DIR = Path("tmp/cp2-ab-test")
CP1_DIR = Path("tmp/cp1-test") / VIDEO_NAME


def _load_env() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def _thumb_b64(img_path: Path, max_w: int = 320) -> str:
    import io

    from PIL import Image

    img = Image.open(img_path)
    ratio = max_w / img.width
    new_size = (max_w, int(img.height * ratio))
    img = img.resize(new_size, Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return b64encode(buf.getvalue()).decode()


async def run_strategy(
    strategy_name: str,
    scene: object,
    frames: list[object],
    frame_dir: Path,
) -> list[dict]:
    """Run VisualAnalyzer with given strategy, return results."""
    from course_supporter.key_pool import KeyPool
    from course_supporter.vd.schemas import DeltaStrategy
    from course_supporter.vd.visual_analyzer import VisualAnalyzer

    _load_env()
    raw_keys = os.environ.get("GEMINI_API_KEY", "")
    key_pool = KeyPool(raw_keys)

    strategy = DeltaStrategy(strategy_name)
    analyzer = VisualAnalyzer(
        key_pool,
        model="gemini-2.5-flash",
        rpm_per_key=5,
        delta_strategy=strategy,
    )

    print(f"\n  Running {strategy_name} strategy...")
    results = await analyzer.analyze_scene(
        scene,  # type: ignore[arg-type]
        frames,  # type: ignore[arg-type]
        frame_dir,
    )

    return [r.model_dump() for r in results]


def generate_html(
    all_results: dict[str, list[dict]],
    frames_data: list[dict],
    frame_dir: Path,
) -> str:
    """Generate comparison HTML: frames in rows, strategies in columns."""
    strategies = list(all_results.keys())
    parts: list[str] = []
    a = parts.append

    a("<!DOCTYPE html><html><head><meta charset='UTF-8'>")
    a("<title>CP-2: Delta Strategy A/B Test</title>")
    a("<style>")
    a("body{font-family:system-ui;margin:20px;background:#1a1a2e;color:#eee}")
    a("h1{color:#e94560}h2{color:#4ecca3}h3{color:#e9a945}")
    a("table{border-collapse:collapse;width:100%;margin:16px 0}")
    a("th,td{border:1px solid #333;padding:8px;vertical-align:top;text-align:left}")
    a("th{background:#16213e;color:#e94560}")
    a(".response{font-size:12px;white-space:pre-wrap;max-height:400px;")
    a("overflow-y:auto;background:#0f3460;padding:8px;border-radius:4px}")
    a(".meta{font-size:11px;color:#888;margin-bottom:4px}")
    a(".delta-badge{background:#e94560;color:#fff;padding:2px 6px;")
    a("border-radius:4px;font-size:10px;font-weight:bold}")
    a(".full-badge{background:#4ecca3;color:#000;padding:2px 6px;")
    a("border-radius:4px;font-size:10px;font-weight:bold}")
    a("img{border-radius:4px}")
    a(".summary{background:#16213e;padding:12px;border-radius:8px;margin:16px 0}")
    a("</style></head><body>")
    a("<h1>CP-2: Delta Strategy A/B Test</h1>")
    a(f"<p>Scene {SCENE_ID}, Video 1, 8 frames (350-392s)</p>")

    # Summary table
    a("<div class='summary'><h2>Summary</h2><table>")
    a(
        "<tr><th>Strategy</th><th>Total tokens out</th>"
        "<th>Total tokens in</th><th>Total latency</th>"
        "<th>Deltas</th><th>Full</th></tr>"
    )
    for strat in strategies:
        results = all_results[strat]
        total_out = sum(r["output_tokens"] for r in results)
        total_in = sum(r["input_tokens"] for r in results)
        total_lat = sum(r["latency_sec"] for r in results)
        n_delta = sum(1 for r in results if r["is_delta"])
        n_full = len(results) - n_delta
        a(
            f"<tr><td><b>{strat}</b></td><td>{total_out}</td>"
            f"<td>{total_in}</td><td>{total_lat:.1f}s</td>"
            f"<td>{n_delta}</td><td>{n_full}</td></tr>"
        )
    a("</table></div>")

    # Per-frame comparison
    a("<h2>Per-frame comparison</h2>")
    for i, frame_data in enumerate(frames_data):
        fid = frame_data["frame_id"]
        ts = frame_data["timestamp_sec"]
        cc = frame_data["change_class"]
        fpath = _find_frame(frame_dir, frame_data["filename"])

        a(f"<h3>Frame {i}: {fid} ({ts:.0f}s, {cc})</h3>")
        if fpath:
            b64 = _thumb_b64(fpath, 400)
            a(f"<img src='data:image/jpeg;base64,{b64}'/>")

        a("<table><tr>")
        for strat in strategies:
            a(f"<th>{strat}</th>")
        a("</tr><tr>")

        for strat in strategies:
            r = all_results[strat][i]
            badge = (
                "<span class='delta-badge'>DELTA</span>"
                if r["is_delta"]
                else "<span class='full-badge'>FULL</span>"
            )
            a("<td>")
            a(
                f"<div class='meta'>{badge} | "
                f"{r['latency_sec']}s | "
                f"in={r['input_tokens']} "
                f"out={r['output_tokens']}</div>"
            )
            resp = html.escape(r.get("response", ""))
            a(f"<div class='response'>{resp}</div>")
            a("</td>")

        a("</tr></table>")

    a("</body></html>")
    return "".join(parts)


def _find_frame(frame_dir: Path, filename: str) -> Path | None:
    direct = frame_dir / filename
    if direct.exists():
        return direct
    for sub in frame_dir.iterdir():
        if sub.is_dir():
            candidate = sub / filename
            if candidate.exists():
                return candidate
    return None


async def main() -> None:
    print("=" * 60)
    print("CP-2: Delta Strategy A/B Test")
    print("=" * 60)

    # Load scene data from CP-1 results
    result_data = json.loads(
        (CP1_DIR / "result.json").read_text(),
    )
    scene_data = next(s for s in result_data["scenes"] if s["scene_id"] == SCENE_ID)
    frames_data = [f for f in result_data["frames"] if f["scene_id"] == SCENE_ID]
    frame_dir = CP1_DIR / "frames"

    print(f"Scene {SCENE_ID}: {len(frames_data)} frames")
    for f in frames_data:
        print(f"  {f['frame_id']}: {f['timestamp_sec']:.0f}s, {f['change_class']}")

    # Build schema objects
    from course_supporter.vd.schemas import SampledFrame, Scene

    scene = Scene(**scene_data)
    frames = [SampledFrame(**f) for f in frames_data]

    # Run 3 strategies
    OUT_DIR.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    all_results: dict[str, list[dict]] = {}

    for strategy in ["none", "explicit", "conditional"]:
        cache_path = OUT_DIR / f"results_{strategy}.json"
        if cache_path.exists():
            print(f"\n  [{strategy}] cached")
            all_results[strategy] = json.loads(cache_path.read_text())
            continue

        results = await run_strategy(strategy, scene, frames, frame_dir)
        all_results[strategy] = results

        # Save results
        cache_path.write_text(
            json.dumps(results, indent=2, ensure_ascii=False),
        )

        total_out = sum(r["output_tokens"] for r in results)
        total_lat = sum(r["latency_sec"] for r in results)
        n_delta = sum(1 for r in results if r["is_delta"])
        print(
            f"  Done: {total_out} tokens out, {total_lat:.1f}s, {n_delta} deltas",
        )

    # Generate comparison
    html_content = generate_html(all_results, frames_data, frame_dir)
    html_path = OUT_DIR / "ab_comparison.html"
    html_path.write_text(html_content)

    print(f"\n\nComparison: file://{html_path.resolve()}")

    # Print summary
    print("\n--- Summary ---")
    print(
        f"{'Strategy':<15} {'Tokens out':>10} {'Tokens in':>10} "
        f"{'Latency':>8} {'Deltas':>7}"
    )
    print("-" * 55)
    for strat, results in all_results.items():
        total_out = sum(r["output_tokens"] for r in results)
        total_in = sum(r["input_tokens"] for r in results)
        total_lat = sum(r["latency_sec"] for r in results)
        n_delta = sum(1 for r in results if r["is_delta"])
        print(
            f"{strat:<15} {total_out:>10} {total_in:>10} "
            f"{total_lat:>7.1f}s {n_delta:>7}",
        )


if __name__ == "__main__":
    asyncio.run(main())
