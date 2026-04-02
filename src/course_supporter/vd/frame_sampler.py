"""Frame sampling, dHash deduplication, PiP detection, and scene segmentation.

Async-first port of ``scripts/spike_frame_sampling.py`` and the ``step_frames``
function from ``scripts/spike_vd_pipeline.py``.

Pipeline steps:
1. FFmpeg fps extraction -> temp JPEG files (async subprocess).
2. PiP detection via temporal diff across 8 candidate zones.
3. dHash computation (with optional PiP mask) -> deduplication.
4. Gap filling (no gap > ``gap_fill_max_sec``).
5. Scene boundary detection -> ``Scene`` grouping.
6. Return ``FrameSamplingResult``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
# Internal rectangle helper (x1, y1, x2, y2 — pixel coords)
# ---------------------------------------------------------------------------


class _Rect:
    __slots__ = ("x1", "x2", "y1", "y2")

    def __init__(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2


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
# dHash helpers
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


def _normalised_distance(
    h1: imagehash.ImageHash,
    h2: imagehash.ImageHash,
    hash_size: int,
) -> float:
    """Normalised Hamming distance in [0, 1]."""
    max_bits = hash_size * hash_size
    return float((h1 - h2) / max_bits)


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

        # 3. Compute dHash + dedup with cooldown
        raw_entries = self._compute_hashes(raw_paths, interval, p.hash_size, mask_rect)
        deduped = self._dedup_with_cooldown(raw_entries, p)
        logger.info("dhash_dedup", before=len(raw_entries), after=len(deduped))

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
        frames, scenes = self._segment_scenes(filled, p)
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
    ) -> list[dict[str, object]]:
        """Compute dHash for each raw extracted frame.

        Returns a list of dicts with path, timestamp, dhash.
        """
        entries: list[dict[str, object]] = []
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
    def _dedup_with_cooldown(
        entries: list[dict[str, object]],
        p: SamplingParams,
    ) -> list[dict[str, object]]:
        """dHash dedup with cooldown for continuous changes (live coding).

        Algorithm:
        - Frames exceeding the dHash threshold are "unstable" (visually
          different from the last kept frame).
        - Isolated unstable frames are kept (slide transitions, new content).
        - If ``pip_cooldown_count`` consecutive unstable frames are seen,
          "coding mode" activates — all subsequent frames are skipped
          until a stable frame appears after ``pip_cooldown_sec`` since
          the last kept frame.
        """
        if not entries:
            return []

        max_bits = p.hash_size * p.hash_size
        threshold = int(max_bits * p.dhash_threshold)

        result = [entries[0]]
        consecutive = 0
        coding_mode = False
        last_added_ts = float(entries[0]["timestamp"])  # type: ignore[arg-type]

        for entry in entries[1:]:
            h_cur: Any = entry["dhash"]
            h_prev: Any = result[-1]["dhash"]
            dist = h_cur - h_prev
            ts = float(entry["timestamp"])  # type: ignore[arg-type]

            if dist > threshold:
                consecutive += 1
                if consecutive >= p.pip_cooldown_count:
                    coding_mode = True
                if not coding_mode:
                    result.append(entry)
                    last_added_ts = ts
            else:
                consecutive = 0
                if coding_mode and ts - last_added_ts >= p.pip_cooldown_sec:
                    result.append(entry)
                    last_added_ts = ts
                    coding_mode = False

        return result

    async def _fill_gaps(
        self,
        entries: list[dict[str, object]],
        video_path: Path,
        gap_dir: Path,
        p: SamplingParams,
        mask: _Rect | None,
    ) -> list[dict[str, object]]:
        """Insert frames where gap exceeds *gap_fill_max_sec*."""
        if len(entries) < 2:
            return list(entries)

        result: list[dict[str, object]] = []
        fill_idx = 0

        for i, entry in enumerate(entries):
            result.append(entry)
            if i + 1 >= len(entries):
                break

            ts_cur = float(entry["timestamp"])  # type: ignore[arg-type]
            ts_next = float(entries[i + 1]["timestamp"])  # type: ignore[arg-type]
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

        result.sort(key=lambda e: float(e["timestamp"]))  # type: ignore[arg-type]
        return result

    @staticmethod
    def _segment_scenes(
        entries: list[dict[str, object]],
        p: SamplingParams,
    ) -> tuple[list[SampledFrame], list[Scene]]:
        """Assign change classes, segment into scenes, build schema objects."""
        if not entries:
            return [], []

        max_bits = p.hash_size * p.hash_size

        # -- pass 1: compute distances and change classes --
        enriched: list[dict[str, object]] = []
        for i, entry in enumerate(entries):
            path: Path = entry["path"]  # type: ignore[assignment]
            ts = float(entry["timestamp"])  # type: ignore[arg-type]
            h: Any = entry["dhash"]
            is_fill = bool(entry.get("is_fill", False))

            if i == 0:
                dist = 0.0
                time_gap = 0.0
                cc = ChangeClass.FIRST
            else:
                prev_h: Any = entries[i - 1]["dhash"]
                prev_ts = float(entries[i - 1]["timestamp"])  # type: ignore[arg-type]
                dist = (h - prev_h) / max_bits
                time_gap = ts - prev_ts
                is_boundary = (
                    dist > p.scene_boundary_dhash
                    or time_gap > p.scene_boundary_time_gap
                )
                if is_boundary:
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
