"""VD-SPIKE-A: Frame Sampling + PiP Tracking spike script.

Tests FFmpeg scene detection vs fixed fps, dHash dedup with various
parameters, PiP zone detection via temporal diff, and cooldown logic.

Usage:
    uv run python scripts/spike_frame_sampling.py
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import imagehash
import numpy as np
from PIL import Image

VIDEO_PATH = Path("current-doc/vd-spike/sample.mp4")
RESULTS_DIR = Path("current-doc/vd-spike/VD-SPIKE-A")
GOLDEN_DIR = Path("current-doc/vd-spike/golden-frames")

# ─── Data classes ────────────────────────────────────────────────────


@dataclass
class FrameInfo:
    index: int
    timestamp_sec: float
    path: Path
    dhash: imagehash.ImageHash | None = None
    hamming_from_prev: int = 0


@dataclass
class ExtractionResult:
    strategy: str
    frame_count: int
    frames: list[FrameInfo] = field(default_factory=list)
    elapsed_sec: float = 0.0


@dataclass
class Rect:
    x1: int
    y1: int
    x2: int
    y2: int


# ─── FFmpeg extraction ───────────────────────────────────────────────


def ffmpeg_scene_detect(
    video: Path, threshold: float, output_dir: Path
) -> list[FrameInfo]:
    """Extract frames using FFmpeg scene change detection."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / "scene_%04d.jpg")

    # Use showinfo to capture timestamps
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-vf",
        f"select=gt(scene\\,{threshold}),showinfo",
        "-vsync",
        "vfr",
        "-q:v",
        "2",
        pattern,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )

    # Parse timestamps from showinfo output
    timestamps: list[float] = []
    for line in result.stderr.splitlines():
        if "pts_time:" in line:
            for part in line.split():
                if part.startswith("pts_time:"):
                    timestamps.append(float(part.split(":")[1]))

    frames: list[FrameInfo] = []
    for i, jpg in enumerate(sorted(output_dir.glob("scene_*.jpg"))):
        ts = timestamps[i] if i < len(timestamps) else 0.0
        frames.append(FrameInfo(index=i, timestamp_sec=ts, path=jpg))

    return frames


def ffmpeg_fps_extract(video: Path, fps: float, output_dir: Path) -> list[FrameInfo]:
    """Extract frames at fixed fps."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / "fps_%04d.jpg")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-vf",
        f"fps={fps}",
        "-q:v",
        "2",
        pattern,
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    frames: list[FrameInfo] = []
    interval = 1.0 / fps
    for i, jpg in enumerate(sorted(output_dir.glob("fps_*.jpg"))):
        frames.append(
            FrameInfo(
                index=i,
                timestamp_sec=i * interval,
                path=jpg,
            )
        )

    return frames


def combined_extract(
    video: Path,
    scene_threshold: float,
    min_interval_sec: float,
    output_dir: Path,
) -> list[FrameInfo]:
    """Scene detection + fill gaps with minimum interval frames."""
    scene_dir = output_dir / "scene"
    interval_dir = output_dir / "interval"

    scene_frames = ffmpeg_scene_detect(video, scene_threshold, scene_dir)
    interval_fps = 1.0 / min_interval_sec
    interval_frames = ffmpeg_fps_extract(video, interval_fps, interval_dir)

    # Merge by timestamp, dedup within tolerance
    all_timestamps: dict[float, FrameInfo] = {}
    tolerance = 1.5  # seconds

    for f in scene_frames:
        all_timestamps[f.timestamp_sec] = f

    for f in interval_frames:
        # Only add if no scene frame within tolerance
        is_dup = any(abs(f.timestamp_sec - st) < tolerance for st in all_timestamps)
        if not is_dup:
            all_timestamps[f.timestamp_sec] = f

    merged = sorted(all_timestamps.values(), key=lambda f: f.timestamp_sec)
    for i, f in enumerate(merged):
        f.index = i

    return merged


# ─── dHash dedup ─────────────────────────────────────────────────────


def compute_dhashes(
    frames: list[FrameInfo],
    hash_size: int = 16,
    mask_rect: Rect | None = None,
) -> list[FrameInfo]:
    """Compute dHash for each frame, optionally masking a region."""
    for f in frames:
        img = Image.open(f.path)
        if mask_rect is not None:
            # Convert to numpy, apply mask, convert back
            arr = np.array(img)
            arr[mask_rect.y1 : mask_rect.y2, mask_rect.x1 : mask_rect.x2] = 128
            img = Image.fromarray(arr)
        f.dhash = imagehash.dhash(img, hash_size=hash_size)
    return frames


def dhash_dedup(frames: list[FrameInfo], threshold: int) -> list[FrameInfo]:
    """Keep only frames where Hamming distance from previous > threshold."""
    if not frames:
        return []

    result = [frames[0]]
    result[0].hamming_from_prev = 999  # always keep first

    for f in frames[1:]:
        assert f.dhash is not None
        assert result[-1].dhash is not None
        dist = f.dhash - result[-1].dhash
        f.hamming_from_prev = dist
        if dist > threshold:
            result.append(f)

    return result


def dhash_dedup_with_cooldown(
    frames: list[FrameInfo],
    threshold: int,
    cooldown_sec: float = 4.0,
    continuous_count: int = 3,
) -> list[FrameInfo]:
    """dHash dedup with cooldown logic for continuous changes (live coding).

    If `continuous_count` consecutive frames exceed threshold,
    enter "coding mode" and wait for `cooldown_sec` of stability
    before capturing.
    """
    if not frames:
        return []

    result = [frames[0]]
    frames[0].hamming_from_prev = 999

    consecutive_changes = 0
    coding_mode = False
    last_stable_time = frames[0].timestamp_sec

    for f in frames[1:]:
        assert f.dhash is not None
        assert result[-1].dhash is not None
        dist = f.dhash - result[-1].dhash
        f.hamming_from_prev = dist

        if dist > threshold:
            consecutive_changes += 1
            if consecutive_changes >= continuous_count:
                coding_mode = True
            if not coding_mode:
                result.append(f)
                consecutive_changes = 0
        else:
            if coding_mode:
                time_stable = f.timestamp_sec - last_stable_time
                if time_stable >= cooldown_sec:
                    result.append(f)
                    coding_mode = False
                    consecutive_changes = 0
            last_stable_time = f.timestamp_sec
            consecutive_changes = 0

    return result


# ─── PiP detection via temporal diff ─────────────────────────────────


def detect_pip_zones(
    frames: list[FrameInfo],
    width: int,
    height: int,
) -> dict[str, float]:
    """Detect PiP camera zone via temporal motion analysis.

    Compare consecutive frames, measure mean pixel change per zone.
    PiP zone = zone with consistently highest motion.
    """
    # Define 8 candidate zones (4 corners + 4 edge centers)
    zone_w = width // 4
    zone_h = height // 3
    zones: dict[str, Rect] = {
        "top_left": Rect(0, 0, zone_w, zone_h),
        "top_right": Rect(width - zone_w, 0, width, zone_h),
        "bottom_left": Rect(0, height - zone_h, zone_w, height),
        "bottom_right": Rect(width - zone_w, height - zone_h, width, height),
        "top_center": Rect(width // 4, 0, 3 * width // 4, zone_h),
        "bottom_center": Rect(width // 4, height - zone_h, 3 * width // 4, height),
        "left_center": Rect(0, height // 3, zone_w, 2 * height // 3),
        "right_center": Rect(width - zone_w, height // 3, width, 2 * height // 3),
    }

    # Accumulate motion per zone across frame pairs
    zone_motion_total: dict[str, float] = {z: 0.0 for z in zones}
    pairs_count = 0

    # Sample up to 30 frame pairs
    sample_indices = list(range(0, min(len(frames), 60), 2))

    for i in sample_indices:
        if i + 1 >= len(frames):
            break

        img1 = cv2.imread(str(frames[i].path))
        img2 = cv2.imread(str(frames[i + 1].path))
        if img1 is None or img2 is None:
            continue

        diff = cv2.absdiff(img1, img2)
        gray_diff = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)

        for name, rect in zones.items():
            zone_slice = gray_diff[rect.y1 : rect.y2, rect.x1 : rect.x2]
            zone_motion_total[name] += float(np.mean(zone_slice))

        pairs_count += 1

    if pairs_count == 0:
        return {}

    # Average motion per zone
    zone_avg: dict[str, float] = {
        z: v / pairs_count for z, v in zone_motion_total.items()
    }

    return zone_avg


def get_pip_mask(
    zone_motion: dict[str, float],
    width: int,
    height: int,
    confidence_threshold: float = 0.3,
) -> Rect | None:
    """Determine PiP mask from zone motion analysis.

    Returns mask Rect if a zone has significantly higher motion
    than others (PiP detected), or None if no PiP.
    """
    if not zone_motion:
        return None

    sorted_zones = sorted(zone_motion.items(), key=lambda x: x[1], reverse=True)
    best_name, best_motion = sorted_zones[0]
    second_motion = sorted_zones[1][1] if len(sorted_zones) > 1 else 0

    confidence = (best_motion - second_motion) / (best_motion + 1e-6)

    print("\n  PiP zone detection:")
    for name, motion in sorted_zones[:4]:
        marker = " ← BEST" if name == best_name else ""
        print(f"    {name}: {motion:.2f}{marker}")
    print(f"  Confidence: {confidence:.3f} (threshold: {confidence_threshold})")

    if confidence < confidence_threshold or best_motion < 1.0:
        print("  Result: No PiP detected (low confidence or low motion)")
        return None

    # Build mask rect from zone name
    zone_w = width // 4
    zone_h = height // 3
    zone_rects: dict[str, Rect] = {
        "top_left": Rect(0, 0, zone_w, zone_h),
        "top_right": Rect(width - zone_w, 0, width, zone_h),
        "bottom_left": Rect(0, height - zone_h, zone_w, height),
        "bottom_right": Rect(width - zone_w, height - zone_h, width, height),
        "top_center": Rect(width // 4, 0, 3 * width // 4, zone_h),
        "bottom_center": Rect(width // 4, height - zone_h, 3 * width // 4, height),
        "left_center": Rect(0, height // 3, zone_w, 2 * height // 3),
        "right_center": Rect(width - zone_w, height // 3, width, 2 * height // 3),
    }
    mask = zone_rects.get(best_name)
    print(f"  Result: PiP at {best_name} → mask {mask}")
    return mask


# ─── Main ────────────────────────────────────────────────────────────


def main() -> None:
    print("=" * 70)
    print("VD-SPIKE-A: Frame Sampling + PiP Tracking")
    print("=" * 70)

    assert VIDEO_PATH.exists(), f"Video not found: {VIDEO_PATH}"

    # Get video info
    cap = cv2.VideoCapture(str(VIDEO_PATH))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps
    cap.release()

    print(f"\nVideo: {width}x{height}, {fps:.1f} fps, {duration:.1f}s")
    res_status = "PASS" if width >= 1280 else "WARN: low resolution"
    print(f"Min frame width check: {width} >= 1280: {res_status}")

    results: dict[str, object] = {
        "video": {
            "resolution": f"{width}x{height}",
            "fps": fps,
            "duration_sec": round(duration, 1),
            "total_frames": total_frames,
        },
        "extraction": {},
        "dhash": {},
        "pip": {},
    }

    # ── Step 1: FFmpeg extraction strategies ──────────────────────
    print("\n" + "─" * 70)
    print("STEP 1: FFmpeg extraction strategies")
    print("─" * 70)

    with tempfile.TemporaryDirectory(prefix="vd_spike_") as tmpdir:
        tmp = Path(tmpdir)

        # Strategy 1: Scene detection with various thresholds
        scene_results: dict[str, int] = {}
        for thresh in [0.01, 0.02, 0.03, 0.05, 0.1, 0.3]:
            t0 = time.time()
            frames = ffmpeg_scene_detect(VIDEO_PATH, thresh, tmp / f"scene_{thresh}")
            elapsed = time.time() - t0
            scene_results[str(thresh)] = len(frames)
            n = len(frames)
            print(f"  scene(thresh={thresh}) → {n} frames ({elapsed:.1f}s)")

        # Strategy 2: Fixed fps
        fps_results: dict[str, int] = {}
        for rate in [0.5, 1.0, 2.0]:
            t0 = time.time()
            frames = ffmpeg_fps_extract(VIDEO_PATH, rate, tmp / f"fps_{rate}")
            elapsed = time.time() - t0
            fps_results[str(rate)] = len(frames)
            print(f"  fps={rate} → {len(frames)} frames ({elapsed:.1f}s)")

        # Strategy 3: Combined (scene 0.02 + min interval 30s)
        t0 = time.time()
        combined_frames = combined_extract(
            VIDEO_PATH,
            scene_threshold=0.02,
            min_interval_sec=30.0,
            output_dir=tmp / "combined",
        )
        elapsed = time.time() - t0
        n = len(combined_frames)
        print(f"  combined(scene=0.02, interval=30s) → {n} ({elapsed:.1f}s)")

        results["extraction"] = {
            "scene_detect": scene_results,
            "fixed_fps": fps_results,
            "combined": len(combined_frames),
        }

        # ── Step 2: PiP detection ─────────────────────────────────
        print("\n" + "─" * 70)
        print("STEP 2: PiP detection via temporal diff")
        print("─" * 70)

        # Use fps=1 frames for PiP detection (more samples)
        pip_frames = ffmpeg_fps_extract(VIDEO_PATH, 1.0, tmp / "pip_detect")
        zone_motion = detect_pip_zones(pip_frames, width, height)
        pip_mask = get_pip_mask(zone_motion, width, height)

        results["pip"] = {
            "zone_motion": {k: round(v, 3) for k, v in zone_motion.items()},
            "detected_zone": None if pip_mask is None else "detected",
            "mask": None
            if pip_mask is None
            else {
                "x1": pip_mask.x1,
                "y1": pip_mask.y1,
                "x2": pip_mask.x2,
                "y2": pip_mask.y2,
            },
        }

        # ── Step 3: dHash tuning ──────────────────────────────────
        print("\n" + "─" * 70)
        print("STEP 3: dHash tuning (on fps=0.5 extraction)")
        print("─" * 70)

        # Use fps=0.5 as base for dHash testing (318 frames)
        base_frames = ffmpeg_fps_extract(VIDEO_PATH, 0.5, tmp / "dhash_base")
        print(f"  Base frames: {len(base_frames)}")

        dhash_results: dict[str, dict[str, dict[str, int]]] = {}

        for hash_size in [8, 12, 16]:
            max_dist = hash_size * hash_size  # max Hamming distance
            dhash_results[str(hash_size)] = {}

            for mask_label, mask in [("no_mask", None), ("pip_mask", pip_mask)]:
                if mask_label == "pip_mask" and pip_mask is None:
                    continue

                compute_dhashes(base_frames, hash_size=hash_size, mask_rect=mask)
                dhash_results[str(hash_size)][mask_label] = {}

                for pct in [5, 10, 15, 20]:
                    thresh = int(max_dist * pct / 100)
                    deduped = dhash_dedup(base_frames, threshold=thresh)
                    key = f"{pct}% (dist>{thresh})"
                    dhash_results[str(hash_size)][mask_label][key] = len(deduped)

            print(f"\n  hash_size={hash_size} (max_dist={max_dist}):")
            for mask_label, thresholds in dhash_results[str(hash_size)].items():
                print(f"    [{mask_label}]")
                for key, count in thresholds.items():
                    print(f"      {key}: {count} unique frames")

        results["dhash"] = dhash_results

        # ── Step 4: Cooldown test ─────────────────────────────────
        print("\n" + "─" * 70)
        print("STEP 4: Cooldown logic test")
        print("─" * 70)

        hash_size = 16
        max_dist = hash_size * hash_size
        threshold_pct = 10
        thresh = int(max_dist * threshold_pct / 100)

        compute_dhashes(base_frames, hash_size=hash_size, mask_rect=pip_mask)
        no_cooldown = dhash_dedup(base_frames, threshold=thresh)
        with_cooldown = dhash_dedup_with_cooldown(
            base_frames,
            threshold=thresh,
            cooldown_sec=4.0,
            continuous_count=3,
        )

        print(f"  hash_size={hash_size}, threshold={threshold_pct}% (dist>{thresh})")
        print(f"  Without cooldown: {len(no_cooldown)} frames")
        print(f"  With cooldown (4s, 3 consecutive): {len(with_cooldown)} frames")

        cooldown_results = {
            "params": {
                "hash_size": hash_size,
                "threshold_pct": threshold_pct,
                "threshold_dist": thresh,
                "cooldown_sec": 4.0,
                "continuous_count": 3,
            },
            "without_cooldown": len(no_cooldown),
            "with_cooldown": len(with_cooldown),
        }
        results["cooldown"] = cooldown_results

        # ── Step 5: Generate golden frames ────────────────────────
        print("\n" + "─" * 70)
        print("STEP 5: Generate golden frames")
        print("─" * 70)

        # Golden frames: fps=1.0 for finer granularity, hash_size=16, threshold=5%
        # Lower threshold to get more representative frames for Spike B
        golden_hash_size = 16
        golden_thresh_pct = 5
        golden_thresh = int(
            golden_hash_size * golden_hash_size * golden_thresh_pct / 100
        )
        golden_source = ffmpeg_fps_extract(VIDEO_PATH, 1.0, tmp / "golden_src")
        compute_dhashes(golden_source, hash_size=golden_hash_size, mask_rect=pip_mask)
        golden = dhash_dedup(golden_source, threshold=golden_thresh)

        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        # Clear old golden frames
        for old in GOLDEN_DIR.glob("*.jpg"):
            old.unlink()

        manifest: list[dict[str, object]] = []
        for i, f in enumerate(golden):
            dest = GOLDEN_DIR / f"golden_{i:03d}_{f.timestamp_sec:.1f}s.jpg"
            dest.write_bytes(f.path.read_bytes())
            manifest.append(
                {
                    "index": i,
                    "timestamp_sec": round(f.timestamp_sec, 1),
                    "hamming_from_prev": int(f.hamming_from_prev),
                    "filename": dest.name,
                }
            )

        print(f"  Golden frames saved: {len(golden)} → {GOLDEN_DIR}/")
        ts_preview = [f"{m['timestamp_sec']}s" for m in manifest[:10]]
        suffix = "..." if len(manifest) > 10 else ""
        print(f"  Timestamps: {ts_preview}{suffix}")

        # Save manifest
        manifest_path = GOLDEN_DIR / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

        results["golden_frames"] = {
            "count": len(golden),
            "config": {
                "extraction": "fps=1.0",
                "hash_size": golden_hash_size,
                "threshold_pct": golden_thresh_pct,
                "threshold_dist": golden_thresh,
                "pip_mask": results["pip"]["mask"],
            },
        }

    # ── Write results ─────────────────────────────────────────────
    results_json = RESULTS_DIR / "results.json"
    results_json.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nResults saved to {results_json}")

    # ── Generate RESULTS.md ───────────────────────────────────────
    generate_report(results)

    print("\n" + "=" * 70)
    print("SPIKE A COMPLETE")
    print("=" * 70)


def generate_report(results: dict[str, object]) -> None:
    """Generate RESULTS.md from collected data."""
    r = results
    video = r["video"]
    extraction = r["extraction"]
    dhash = r["dhash"]
    cooldown = r["cooldown"]
    golden = r["golden_frames"]
    pip_data = r["pip"]

    lines: list[str] = []
    a = lines.append

    a("# VD-SPIKE-A: Results — Frame Sampling + PiP Tracking")
    a("")
    a("**Дата:** 2026-03-31")
    res = video["resolution"]
    vfps = video["fps"]
    dur = video["duration_sec"]
    tf = video["total_frames"]
    a(f"**Відео:** {res}, {vfps} fps, {dur}s ({tf} frames)")
    a("")

    # Extraction
    a("## 1. FFmpeg Extraction Strategies")
    a("")
    a("### Scene Detection")
    a("")
    a("| Threshold | Frames |")
    a("|---|---|")
    for thresh, count in extraction["scene_detect"].items():
        a(f"| {thresh} | {count} |")
    a("")

    a("### Fixed FPS")
    a("")
    a("| FPS | Frames |")
    a("|---|---|")
    for fps_val, count in extraction["fixed_fps"].items():
        a(f"| {fps_val} | {count} |")
    a("")
    a(f"### Combined (scene=0.02 + interval=30s): **{extraction['combined']} frames**")
    a("")

    # PiP
    a("## 2. PiP Detection")
    a("")
    if pip_data["mask"]:
        m = pip_data["mask"]
        a(f"**PiP знайдено:** mask ({m['x1']}, {m['y1']}) → ({m['x2']}, {m['y2']})")
    else:
        a("**PiP не знайдено** (motion рівномірний по зонах або занизький)")
    a("")
    a("| Zone | Avg Motion |")
    a("|---|---|")
    for zone, motion in sorted(
        pip_data["zone_motion"].items(), key=lambda x: x[1], reverse=True
    ):
        a(f"| {zone} | {motion} |")
    a("")

    # dHash
    a("## 3. dHash Tuning")
    a("")
    a("Base: fps=0.5 extraction (318 frames)")
    a("")
    for hash_size, masks in dhash.items():
        a(f"### hash_size={hash_size}")
        a("")
        for mask_label, thresholds in masks.items():
            a(f"**{mask_label}:**")
            a("")
            a("| Threshold | Unique Frames |")
            a("|---|---|")
            for key, count in thresholds.items():
                a(f"| {key} | {count} |")
            a("")

    # Cooldown
    a("## 4. Cooldown Logic")
    a("")
    cp = cooldown["params"]
    hs = cp["hash_size"]
    tp = cp["threshold_pct"]
    td = cp["threshold_dist"]
    a(f"hash_size={hs}, threshold={tp}% (dist>{td})")
    a("")
    a(f"- **Without cooldown:** {cooldown['without_cooldown']} frames")
    cs = cp["cooldown_sec"]
    cc = cp["continuous_count"]
    wc = cooldown["with_cooldown"]
    a(f"- **With cooldown** ({cs}s, {cc} consecutive): {wc} frames")
    a("")

    # Golden
    a("## 5. Golden Frames")
    a("")
    gc = golden["config"]
    a(f"- **Кількість:** {golden['count']}")
    a(f"- **Extraction:** {gc['extraction']}")
    a(f"- **hash_size:** {gc['hash_size']}")
    a(f"- **Threshold:** {gc['threshold_pct']}% (dist>{gc['threshold_dist']})")
    a(f"- **PiP mask:** {'Yes' if gc['pip_mask'] else 'No'}")
    a(f"- **Шлях:** `{GOLDEN_DIR}/`")
    a("")

    # Recommendations
    a("## 6. Рекомендації для Production")
    a("")
    a("*(Заповнюється після аналізу результатів)*")
    a("")

    report_path = RESULTS_DIR / "RESULTS.md"
    report_path.write_text("\n".join(lines))
    print(f"Report saved to {report_path}")


if __name__ == "__main__":
    main()
