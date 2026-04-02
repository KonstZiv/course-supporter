"""Frame sampling, multi-metric deduplication, PiP detection, scene segmentation.

Pipeline steps:
1. FFmpeg fps extraction -> temp JPEG files (async subprocess).
2. PiP detection via temporal diff across 8 candidate zones.
3. Multi-metric dedup (5 metrics, tiered voting).
4. Gap filling (no gap > ``gap_fill_max_sec``).
5. Scene boundary detection -> ``Scene`` grouping.
6. Return ``FrameSamplingResult``.

Metrics used for dedup voting:
- dHash: structural gradient hash (coarse shape changes).
- pixel_diff: fraction of pixels changed above noise floor.
- color_hist: Bhattacharyya distance of colour histograms.
- SSIM: structural similarity index (perceptual).
- edge_diff: change in Canny edge map.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

import structlog

from course_supporter.vd.schemas import (
    ChangeClass,
    FrameSamplingResult,
    FrameSource,
    PiPMask,
    SampledFrame,
    SamplingParams,
    Scene,
)

if TYPE_CHECKING:
    import imagehash

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Internal data structures
# ---------------------------------------------------------------------------


class _Rect(NamedTuple):
    """Pixel-coordinate rectangle (x1, y1, x2, y2)."""

    x1: int
    y1: int
    x2: int
    y2: int


class _FrameEntry(TypedDict, total=False):
    """Intermediate frame data passed between pipeline steps."""

    path: Path
    timestamp: float
    dhash: Any  # imagehash.ImageHash at runtime
    is_fill: bool


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------


async def _ffmpeg_extract_fps(
    video: Path,
    fps: float,
    output_dir: Path,
    *,
    timeout_sec: float = 300.0,
) -> list[Path]:
    """Extract frames at fixed *fps* into *output_dir* via FFmpeg.

    Returns sorted list of JPEG paths.
    """
    output_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    pattern = str(output_dir / "frame_%06d.jpg")

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-i",
        str(video),
        "-vf",
        f"fps={fps}",
        "-q:v",
        "2",
        pattern,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await asyncio.wait_for(
        process.communicate(),
        timeout=timeout_sec,
    )

    if process.returncode != 0:
        err_detail = stderr.decode()[:500]
        msg = f"FFmpeg fps extraction failed (code {process.returncode}): {err_detail}"
        raise RuntimeError(msg)

    return sorted(output_dir.glob("frame_*.jpg"))  # noqa: ASYNC240


async def _ffmpeg_extract_single(
    video: Path,
    timestamp_sec: float,
    output: Path,
    *,
    timeout_sec: float = 30.0,
) -> bool:
    """Extract a single frame at *timestamp_sec*."""
    output.parent.mkdir(parents=True, exist_ok=True)

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-ss",
        f"{timestamp_sec:.2f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(output),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
    except TimeoutError:
        process.kill()
        return False

    return process.returncode == 0 and output.exists()  # noqa: ASYNC240


async def _get_video_resolution(video: Path) -> tuple[int, int]:
    """Return (width, height) of the video using ffprobe."""
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "csv=p=0:s=x",
        str(video),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    parts = stdout.decode().strip().split("x")
    return int(parts[0]), int(parts[1])


# ---------------------------------------------------------------------------
# Per-frame hashing
# ---------------------------------------------------------------------------


def _compute_dhash(
    img_path: Path,
    hash_size: int,
    mask: _Rect | None,
) -> imagehash.ImageHash:
    """Compute dHash for an image, optionally masking a PiP region."""
    import imagehash as _imagehash
    import numpy as _np
    from PIL import Image as _Image

    img: _Image.Image = _Image.open(img_path)
    if mask is not None:
        arr = _np.array(img)
        arr[mask.y1 : mask.y2, mask.x1 : mask.x2] = 128
        img = _Image.fromarray(arr)
    return _imagehash.dhash(img, hash_size=hash_size)


# ---------------------------------------------------------------------------
# Multi-metric frame comparison
# ---------------------------------------------------------------------------


class _FrameMetrics(NamedTuple):
    """Comparison result between two frames (5 independent signals)."""

    dhash_dist: float  # 0-1, normalised Hamming
    pixel_diff: float  # 0-1, fraction of changed pixels
    color_hist: float  # 0-1, Bhattacharyya distance
    ssim: float  # 0-1, 1 = identical
    edge_diff: float  # 0-1, relative change in edge pixels


def _color_hist_distance(
    path1: Path,
    path2: Path,
    mask: _Rect | None,
) -> float:
    """Bhattacharyya distance between colour histograms of two frames."""
    import cv2

    img1 = cv2.imread(str(path1))
    img2 = cv2.imread(str(path2))
    if img1 is None or img2 is None:
        return 1.0

    if mask is not None:
        img1[mask.y1 : mask.y2, mask.x1 : mask.x2] = 128
        img2[mask.y1 : mask.y2, mask.x1 : mask.x2] = 128

    hist1 = cv2.calcHist(
        [img1],
        [0, 1, 2],
        None,
        [8, 8, 8],
        [0, 256, 0, 256, 0, 256],
    )
    hist2 = cv2.calcHist(
        [img2],
        [0, 1, 2],
        None,
        [8, 8, 8],
        [0, 256, 0, 256, 0, 256],
    )
    cv2.normalize(hist1, hist1)
    cv2.normalize(hist2, hist2)
    return float(cv2.compareHist(hist1, hist2, cv2.HISTCMP_BHATTACHARYYA))


def _flow_coherence(
    path1: Path,
    path2: Path,
    mask: _Rect | None,
) -> float:
    """Optical flow coherence between two frames.

    Returns a value in [0, 1] where 1 means all moving pixels go in
    the same direction (rotation, scroll, pan) and 0 means chaotic or
    no motion (content replacement).

    Uses Farneback dense optical flow.
    """
    import cv2
    import numpy as _np

    img1 = cv2.imread(str(path1))
    img2 = cv2.imread(str(path2))
    if img1 is None or img2 is None:
        return 0.0

    gray1: Any = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    gray2: Any = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)

    if mask is not None:
        gray1[mask.y1 : mask.y2, mask.x1 : mask.x2] = 128
        gray2[mask.y1 : mask.y2, mask.x1 : mask.x2] = 128

    no_flow: Any = None
    flow: Any = cv2.calcOpticalFlowFarneback(
        gray1,
        gray2,
        no_flow,
        pyr_scale=0.5,
        levels=3,
        winsize=15,
        iterations=3,
        poly_n=5,
        poly_sigma=1.2,
        flags=0,
    )

    mag, ang = cv2.cartToPolar(flow[..., 0], flow[..., 1])

    # Only consider pixels with significant movement (>1px)
    significant = mag > 1.0
    n_sig = int(_np.count_nonzero(significant))
    if n_sig < 100:
        return 0.0  # no meaningful motion

    # Coherence: mean cosine similarity of angles to the mean angle.
    # Perfectly coherent motion → all angles equal → coherence = 1.
    sig_angles = ang[significant]
    mean_angle = float(
        _np.arctan2(
            _np.mean(_np.sin(sig_angles)),
            _np.mean(_np.cos(sig_angles)),
        )
    )
    coherence = float(_np.mean(_np.cos(sig_angles - mean_angle)))
    return max(coherence, 0.0)


def _compare_frames(
    path_prev: Path,
    path_cur: Path,
    hash_size: int,
    dhash_prev: Any,
    dhash_cur: Any,
    mask: _Rect | None,
    *,
    pixel_noise_floor: int = 25,
) -> _FrameMetrics:
    """Compute all 5 metrics between two frames."""
    import cv2
    import numpy as _np

    # --- dHash (pre-computed) ---
    max_bits = hash_size * hash_size
    dhash_dist = float((dhash_prev - dhash_cur) / max_bits)

    # --- Load images ---
    img1 = cv2.imread(str(path_prev))
    img2 = cv2.imread(str(path_cur))
    if img1 is None or img2 is None:
        return _FrameMetrics(dhash_dist, 1.0, 1.0, 0.0, 1.0)

    # Apply PiP mask to both
    if mask is not None:
        img1[mask.y1 : mask.y2, mask.x1 : mask.x2] = 128
        img2[mask.y1 : mask.y2, mask.x1 : mask.x2] = 128

    gray1 = cv2.cvtColor(img1, cv2.COLOR_BGR2GRAY)
    gray2 = cv2.cvtColor(img2, cv2.COLOR_BGR2GRAY)

    # --- Pixel diff ---
    abs_diff = cv2.absdiff(gray1, gray2)
    changed = int(_np.count_nonzero(abs_diff > pixel_noise_floor))
    total = gray1.shape[0] * gray1.shape[1]
    pixel_diff = changed / total

    # --- Color histogram (Bhattacharyya) ---
    hist1 = cv2.calcHist(
        [img1],
        [0, 1, 2],
        None,
        [8, 8, 8],
        [0, 256, 0, 256, 0, 256],
    )
    hist2 = cv2.calcHist(
        [img2],
        [0, 1, 2],
        None,
        [8, 8, 8],
        [0, 256, 0, 256, 0, 256],
    )
    cv2.normalize(hist1, hist1)
    cv2.normalize(hist2, hist2)
    color_hist = float(
        cv2.compareHist(hist1, hist2, cv2.HISTCMP_BHATTACHARYYA),
    )

    # --- SSIM (simplified, window-based) ---
    ssim = _compute_ssim(gray1, gray2)

    # --- Edge diff (Canny) ---
    edges1 = cv2.Canny(gray1, 50, 150)
    edges2 = cv2.Canny(gray2, 50, 150)
    edge_diff_map = cv2.absdiff(edges1, edges2)
    edge_changed = int(_np.count_nonzero(edge_diff_map))
    max_edges = max(
        int(_np.count_nonzero(edges1)),
        int(_np.count_nonzero(edges2)),
        1,
    )
    edge_diff = edge_changed / max_edges

    return _FrameMetrics(dhash_dist, pixel_diff, color_hist, ssim, edge_diff)


def _compute_ssim(
    gray1: Any,
    gray2: Any,
    *,
    k1: float = 0.01,
    k2: float = 0.03,
    win_size: int = 11,
) -> float:
    """Compute mean SSIM between two grayscale images."""
    import cv2
    import numpy as _np

    c1 = (k1 * 255) ** 2
    c2 = (k2 * 255) ** 2

    g1 = gray1.astype(_np.float64)
    g2 = gray2.astype(_np.float64)

    mu1 = cv2.GaussianBlur(g1, (win_size, win_size), 1.5)
    mu2 = cv2.GaussianBlur(g2, (win_size, win_size), 1.5)

    sigma1_sq = cv2.GaussianBlur(g1 * g1, (win_size, win_size), 1.5) - mu1 * mu1
    sigma2_sq = cv2.GaussianBlur(g2 * g2, (win_size, win_size), 1.5) - mu2 * mu2
    sigma12 = cv2.GaussianBlur(g1 * g2, (win_size, win_size), 1.5) - mu1 * mu2

    num = (2 * mu1 * mu2 + c1) * (2 * sigma12 + c2)
    den = (mu1 * mu1 + mu2 * mu2 + c1) * (sigma1_sq + sigma2_sq + c2)

    ssim_map = num / den
    return float(_np.mean(ssim_map))


# ---------------------------------------------------------------------------
# PiP detection via temporal diff
# ---------------------------------------------------------------------------


def _build_zones(width: int, height: int) -> dict[str, _Rect]:
    """Eight candidate zones: 4 corners + 4 edge centres."""
    zw = width // 4
    zh = height // 3
    return {
        "top_left": _Rect(0, 0, zw, zh),
        "top_right": _Rect(width - zw, 0, width, zh),
        "bottom_left": _Rect(0, height - zh, zw, height),
        "bottom_right": _Rect(width - zw, height - zh, width, height),
        "top_center": _Rect(width // 4, 0, 3 * width // 4, zh),
        "bottom_center": _Rect(width // 4, height - zh, 3 * width // 4, height),
        "left_center": _Rect(0, height // 3, zw, 2 * height // 3),
        "right_center": _Rect(width - zw, height // 3, width, 2 * height // 3),
    }


def _detect_pip(
    frame_paths: list[Path],
    width: int,
    height: int,
    *,
    max_pairs: int = 30,
    confidence_threshold: float = 0.3,
) -> PiPMask | None:
    """Detect PiP overlay via temporal motion analysis.

    Compares consecutive frames, measures mean pixel change per zone.
    The zone with consistently highest motion is likely a PiP camera.
    """
    import cv2
    import numpy as _np

    zones = _build_zones(width, height)
    zone_totals: dict[str, float] = {z: 0.0 for z in zones}
    pairs = 0

    step = max(1, len(frame_paths) // (max_pairs * 2))
    indices = list(range(0, len(frame_paths) - 1, step))[:max_pairs]

    for i in indices:
        img1 = cv2.imread(str(frame_paths[i]))
        img2 = cv2.imread(str(frame_paths[i + 1]))
        if img1 is None or img2 is None:
            continue

        diff = cv2.absdiff(img1, img2)
        gray: Any = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)

        for name, rect in zones.items():
            region = gray[rect.y1 : rect.y2, rect.x1 : rect.x2]
            zone_totals[name] += float(_np.mean(region))
        pairs += 1

    if pairs == 0:
        return None

    zone_avg = {z: v / pairs for z, v in zone_totals.items()}
    sorted_zones = sorted(zone_avg.items(), key=lambda x: x[1], reverse=True)
    best_name, best_motion = sorted_zones[0]
    second_motion = sorted_zones[1][1] if len(sorted_zones) > 1 else 0.0

    confidence = (best_motion - second_motion) / (best_motion + 1e-6)

    logger.info(
        "pip_detection",
        best_zone=best_name,
        best_motion=round(best_motion, 2),
        confidence=round(confidence, 3),
    )

    if confidence < confidence_threshold or best_motion < 1.0:
        return None

    rect = zones[best_name]
    return PiPMask(
        x=rect.x1,
        y=rect.y1,
        width=rect.x2 - rect.x1,
        height=rect.y2 - rect.y1,
        confidence=round(confidence, 4),
    )


# ---------------------------------------------------------------------------
# FrameSampler
# ---------------------------------------------------------------------------


class FrameSampler:
    """Extract, deduplicate, and segment video frames.

    All spike-proven defaults are baked into ``SamplingParams``.
    """

    def __init__(self, params: SamplingParams | None = None) -> None:
        self.params = params or SamplingParams()

    async def sample(
        self,
        video_path: Path,
        output_dir: Path,
    ) -> FrameSamplingResult:
        """Run the full frame-sampling pipeline.

        Args:
            video_path: Path to video file.
            output_dir: Directory for extracted frame JPEGs.
                        Created if it does not exist.  Caller is responsible
                        for cleanup.

        Returns:
            ``FrameSamplingResult`` with frames, scenes, PiP mask, etc.
        """
        p = self.params

        # 0. Video resolution
        width, height = await _get_video_resolution(video_path)
        logger.info(
            "frame_sampler_start",
            video=str(video_path),
            resolution=f"{width}x{height}",
        )

        # 1. FFmpeg fps extraction
        raw_dir = output_dir / "raw"
        raw_paths = await _ffmpeg_extract_fps(video_path, p.fps, raw_dir)
        interval = 1.0 / p.fps
        logger.info("ffmpeg_extracted", frame_count=len(raw_paths))

        # 2. PiP detection (reuse raw frames to avoid second extraction)
        pip_mask = _detect_pip(
            raw_paths,
            width,
            height,
            confidence_threshold=0.3,
        )

        mask_rect = (
            _Rect(
                pip_mask.x,
                pip_mask.y,
                pip_mask.x + pip_mask.width,
                pip_mask.y + pip_mask.height,
            )
            if pip_mask
            else None
        )

        if pip_mask:
            logger.info(
                "pip_mask_applied",
                x=pip_mask.x,
                y=pip_mask.y,
                w=pip_mask.width,
                h=pip_mask.height,
            )

        # 3. Compute dHash + multi-metric dedup
        raw_entries = self._compute_hashes(
            raw_paths,
            interval,
            p.hash_size,
            mask_rect,
        )
        deduped = self._dedup_voting(raw_entries, p, mask_rect)
        logger.info("dedup_voting", before=len(raw_entries), after=len(deduped))

        # 4. Gap fill
        gap_dir = output_dir / "gap_fill"
        filled = await self._fill_gaps(
            deduped,
            video_path,
            gap_dir,
            p,
            mask_rect,
        )
        logger.info("gap_fill", before=len(deduped), after=len(filled))

        # 5. Scene segmentation
        frames, scenes = self._segment_scenes(filled, p, mask_rect)
        logger.info("scene_segmentation", frames=len(frames), scenes=len(scenes))

        return FrameSamplingResult(
            frames=frames,
            scenes=scenes,
            pip_mask=pip_mask,
            video_resolution=(width, height),
            sampling_params=p,
            complete=True,
        )

    # -- internal steps ----------------------------------------------------

    @staticmethod
    def _compute_hashes(
        paths: list[Path],
        interval: float,
        hash_size: int,
        mask: _Rect | None,
    ) -> list[_FrameEntry]:
        """Compute dHash for each raw extracted frame."""
        entries: list[_FrameEntry] = []
        for i, path in enumerate(paths):
            h = _compute_dhash(path, hash_size, mask)
            entries.append(
                {
                    "path": path,
                    "timestamp": round(i * interval, 2),
                    "dhash": h,
                }
            )
        return entries

    @staticmethod
    def _dedup_voting(
        entries: list[_FrameEntry],
        p: SamplingParams,
        mask: _Rect | None,
    ) -> list[_FrameEntry]:
        """Multi-metric dedup with tiered voting.

        Each frame is compared to the last *kept* frame using 5
        independent metrics.  A frame is kept if:

        - **Tier 1:** any single strong signal (dHash > 15% or
          pixel_diff > 10%) — obvious scene change.
        - **Tier 2:** at least ``min_votes`` metrics vote "changed".

        This catches subtle changes (code highlighting, incremental
        assembly, partial UI updates) that dHash alone would miss.
        """
        if not entries:
            return []

        result = [entries[0]]

        for entry in entries[1:]:
            prev = result[-1]
            metrics = _compare_frames(
                prev["path"],
                entry["path"],
                p.hash_size,
                prev["dhash"],
                entry["dhash"],
                mask,
                pixel_noise_floor=p.pixel_noise_floor,
            )

            # Tier 1: strong signal — keep unconditionally
            if (
                metrics.dhash_dist > p.tier1_dhash
                or metrics.pixel_diff > p.tier1_pixel_diff
            ):
                result.append(entry)
                continue

            # Tier 2: voting — keep if enough metrics agree
            votes = (
                (metrics.dhash_dist > p.vote_dhash)
                + (metrics.pixel_diff > p.vote_pixel_diff)
                + (metrics.color_hist > p.vote_color_hist)
                + (metrics.ssim < p.vote_ssim)
                + (metrics.edge_diff > p.vote_edge_diff)
            )
            if votes >= p.min_votes:
                result.append(entry)

        return result

    async def _fill_gaps(
        self,
        entries: list[_FrameEntry],
        video_path: Path,
        gap_dir: Path,
        p: SamplingParams,
        mask: _Rect | None,
    ) -> list[_FrameEntry]:
        """Insert frames where gap exceeds *gap_fill_max_sec*."""
        if len(entries) < 2:
            return list(entries)

        result: list[_FrameEntry] = []
        fill_idx = 0

        for i, entry in enumerate(entries):
            result.append(entry)
            if i + 1 >= len(entries):
                break

            ts_cur = entry["timestamp"]
            ts_next = entries[i + 1]["timestamp"]
            gap = ts_next - ts_cur

            if gap <= p.gap_fill_max_sec:
                continue

            n_fill = max(1, round(gap / p.gap_fill_max_sec) - 1)
            interval = gap / (n_fill + 1)

            for j in range(1, n_fill + 1):
                ts = ts_cur + interval * j
                ts_int = round(ts)
                fname = f"fill_{fill_idx:03d}_{ts_int}s.jpg"
                fpath = gap_dir / fname

                ok = await _ffmpeg_extract_single(video_path, ts, fpath)
                if not ok:
                    logger.warning("gap_fill_failed", timestamp=ts)
                    continue

                h = _compute_dhash(fpath, p.hash_size, mask)
                result.append(
                    {
                        "path": fpath,
                        "timestamp": round(ts, 1),
                        "dhash": h,
                        "is_fill": True,
                    }
                )
                fill_idx += 1

        result.sort(key=lambda e: e["timestamp"])
        return result

    @staticmethod
    def _segment_scenes(
        entries: list[_FrameEntry],
        p: SamplingParams,
        mask: _Rect | None,
    ) -> tuple[list[SampledFrame], list[Scene]]:
        """Assign change classes, segment into scenes, build schema objects.

        Scene boundary requires passing three gates:

        1. **dHash** — structure changed (>20%).
        2. **Colour histogram** — colours changed too (not just motion).
        3. **Optical flow** — no coherent motion detected (not rotation
           or scrolling where colours change because new parts appear).

        Time gaps >``scene_boundary_time_gap`` force a boundary
        regardless of visual similarity.
        """
        if not entries:
            return [], []

        max_bits = p.hash_size * p.hash_size

        # -- pass 1: compute distances and change classes --
        enriched: list[dict[str, object]] = []
        for i, entry in enumerate(entries):
            path = entry["path"]
            ts = entry["timestamp"]
            h: Any = entry["dhash"]
            is_fill = bool(entry.get("is_fill", False))

            if i == 0:
                dist = 0.0
                time_gap = 0.0
                cc = ChangeClass.FIRST
            else:
                prev_h: Any = entries[i - 1]["dhash"]
                prev_ts = entries[i - 1]["timestamp"]
                dist = (h - prev_h) / max_bits
                time_gap = ts - prev_ts

                if time_gap > p.scene_boundary_time_gap:
                    # Long gap → always a boundary
                    cc = ChangeClass.BOUNDARY
                elif dist > p.scene_boundary_dhash:
                    # Gate 1 passed (dHash). Check gate 2 (colour).
                    color_dist = _color_hist_distance(
                        entries[i - 1]["path"],
                        entry["path"],
                        mask,
                    )
                    if color_dist <= p.scene_boundary_color_hist:
                        # Colours same → motion (rotation with
                        # same palette, gestures, camera shake)
                        cc = ChangeClass.MEDIUM
                    else:
                        # Gate 2 passed. Check gate 3 (flow).
                        coherence = _flow_coherence(
                            entries[i - 1]["path"],
                            entry["path"],
                            mask,
                        )
                        if coherence > p.scene_boundary_flow_coherence:
                            # Coherent motion (rotation showing
                            # new colours, scrolling) → same scene
                            cc = ChangeClass.MEDIUM
                        else:
                            # All three gates passed → real boundary
                            cc = ChangeClass.BOUNDARY
                elif dist > 0.10:
                    cc = ChangeClass.MEDIUM
                else:
                    cc = ChangeClass.LOW

            ts_int = round(ts)
            source = FrameSource.GAP_FILL if is_fill else FrameSource.GOLDEN
            frame_id = f"{'fill' if is_fill else 'frame'}_{i:03d}_{ts_int}s"
            filename = path.name

            enriched.append(
                {
                    "frame_id": frame_id,
                    "filename": filename,
                    "timestamp_sec": round(ts, 2),
                    "dhash": str(h),
                    "dhash_dist": round(dist, 4),
                    "time_gap": round(time_gap, 1),
                    "change_class": cc,
                    "source": source,
                }
            )

        # -- pass 2: segment into scenes --
        scene_starts: list[int] = [0]
        for i in range(1, len(enriched)):
            if enriched[i]["change_class"] == ChangeClass.BOUNDARY:
                scene_starts.append(i)

        scenes: list[Scene] = []
        for s_idx, start in enumerate(scene_starts):
            if s_idx + 1 < len(scene_starts):
                end = scene_starts[s_idx + 1] - 1
            else:
                end = len(enriched) - 1

            scene_frame_ids: list[str] = []
            for k in range(start, end + 1):
                enriched[k]["scene_id"] = s_idx
                scene_frame_ids.append(str(enriched[k]["frame_id"]))

            scenes.append(
                Scene(
                    scene_id=s_idx,
                    frame_ids=scene_frame_ids,
                    start_sec=float(enriched[start]["timestamp_sec"]),  # type: ignore[arg-type]
                    end_sec=float(enriched[end]["timestamp_sec"]),  # type: ignore[arg-type]
                )
            )

        frames = [
            SampledFrame(
                frame_id=str(e["frame_id"]),
                filename=str(e["filename"]),
                timestamp_sec=float(e["timestamp_sec"]),  # type: ignore[arg-type]
                scene_id=int(e["scene_id"]),  # type: ignore[call-overload]
                source=e["source"],
                dhash=str(e["dhash"]),
                dhash_dist=float(e["dhash_dist"]),  # type: ignore[arg-type]
                time_gap=float(e["time_gap"]),  # type: ignore[arg-type]
                change_class=e["change_class"],
            )
            for e in enriched
        ]

        return frames, scenes
