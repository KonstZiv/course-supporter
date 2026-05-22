"""Pre-LLM scene detection — frame sampling + dedup + segmentation (Krok 3).

Algorithmic (zero LLM): from a video, extract frames at a fixed fps,
detect a Picture-in-Picture (talking-head) corner, drop visually
redundant frames via 5-metric tiered voting, fill long coverage gaps,
and group the survivors into scenes via a 3-gate boundary test —
emitting a :class:`DetectionResult` (scenes with nested kept frames +
the PiP box).

This is a *fresh* implementation of the knowledge in
``course_supporter.vd.frame_sampler`` (R0.2: that mature cv2/imagehash
module is the **specification**, not code to import/copy — the new
namespace stays isolated so ``vd/`` can be deleted wholesale in task
2.4.9). Thresholds (:class:`SamplingParams`) are the vd-derived defaults,
empirically calibrated on real video during the vd era; a full
re-calibration sweep is deferred to DD-2.4-B (trigger: production
code-slide loss).

Two perf refinements over the reference: optical-flow coherence is
computed on a downscaled grayscale (scale-tolerant; only at boundary
candidates), and frame decodes are cached (read-once) rather than
re-``imread`` per comparison.

PiP responsibility split: this step *detects* the box and returns it;
the output JPEGs stay **raw** (unmasked). The mask is applied internally
only to the dedup/boundary *metrics* (so a moving talking-head does not
fake a scene change). Pass 1 (Krok 4) applies the mask to the frame
itself when building the vision call.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

import cv2
import imagehash
import numpy as np
import structlog
from PIL import Image

from course_supporter.ingestion.base import ProcessingError
from course_supporter.ingestion.video_pipeline import media
from course_supporter.ingestion.video_pipeline.schemas import (
    ChangeClass,
    DetectionResult,
    PiPMask,
    SampledFrame,
    Scene,
)

if TYPE_CHECKING:
    from pathlib import Path

    from course_supporter.ingestion.video_pipeline.schemas import VideoFileMetadata

logger = structlog.get_logger()


@dataclass(frozen=True)
class SamplingParams:
    """Frame-sampling + dedup + scene-boundary thresholds (Krok 3, internal).

    vd-derived defaults — empirically calibrated on real video during the
    vd era (R0.2 knowledge), ported verbatim. Per KD8/KD-2.3-X these are
    for the *video* source_type only; a full recalibration sweep is
    deferred to DD-2.4-B. Kept out of the public schema (encapsulation —
    the spike tunes thresholds, the ``DetectionResult`` contract is
    stable).
    """

    fps: float = 0.5
    hash_size: int = 16
    gap_fill_max_sec: float = 15.0

    # Scene boundary 3-gate (ALL three required; protects against
    # false-positive BOUNDARY from rotation/scroll/gesture).
    scene_boundary_dhash: float = 0.20
    scene_boundary_color_hist: float = 0.20
    scene_boundary_flow_coherence: float = 0.6
    scene_boundary_time_gap: float = 10.0

    # Tier 1: a single strong signal → unconditional keep.
    tier1_dhash: float = 0.15
    tier1_pixel_diff: float = 0.10

    # Tier 2: per-metric vote thresholds; keep if >= min_votes agree.
    vote_dhash: float = 0.03
    vote_pixel_diff: float = 0.02
    vote_color_hist: float = 0.15
    vote_ssim: float = 0.95  # SSIM *below* this votes "changed".
    vote_edge_diff: float = 0.05
    min_votes: int = 2

    pixel_noise_floor: int = 25  # intensity delta below this is noise
    medium_change_dhash: float = 0.10  # MEDIUM vs LOW change_class split

    # Downscaled optical-flow target (longest side, px) — flow coherence
    # is scale-tolerant, so this is a free speedup at boundary candidates.
    flow_downscale_max: int = 480


_DEFAULT_PARAMS = SamplingParams()
_DECODE_CACHE_SIZE = 8  # bounds memory: ~prev+cur working set, sequential access

# Named constants for vd-ported magic numbers (behaviour unchanged).
_PIP_MASK_GRAY = 128  # neutral grey written over a PiP region before metrics
_FLOW_MIN_MAGNITUDE_PX = 1.0  # per-pixel flow magnitude that counts as motion
_FLOW_MIN_SIGNIFICANT_PX = 100  # min moving pixels for a meaningful flow reading
_PIP_MIN_ZONE_MOTION = 1.0  # min mean zone motion to treat a zone as a PiP candidate


class _Rect(NamedTuple):
    """Pixel rectangle (x1, y1, x2, y2)."""

    x1: int
    y1: int
    x2: int
    y2: int


@dataclass
class _Entry:
    """Intermediate per-frame data threaded between sub-steps."""

    path: Path
    timestamp_sec: float
    dhash: Any = None  # imagehash.ImageHash; lazily filled for gap-fill frames


# ── decode cache (read-once; masked once per run) ──────────────────────


class _DecodeCache:
    """Bounded read-once cache of PiP-masked BGR frames + derived gray.

    The mask is constant per run, so masking is applied once at decode
    (cheaper than copying per comparison). Bounded LRU keeps memory flat
    for the sequential prev/cur access pattern.
    """

    def __init__(self, mask: _Rect | None, maxsize: int = _DECODE_CACHE_SIZE) -> None:
        self._mask = mask
        self._maxsize = maxsize
        self._bgr: OrderedDict[Path, Any] = OrderedDict()
        self._gray: OrderedDict[Path, Any] = OrderedDict()

    def bgr(self, path: Path) -> Any:
        cached = self._bgr.get(path)
        if cached is not None:
            self._bgr.move_to_end(path)
            return cached
        img = cv2.imread(str(path))
        if img is not None and self._mask is not None:
            m = self._mask
            img[m.y1 : m.y2, m.x1 : m.x2] = _PIP_MASK_GRAY
        self._bgr[path] = img
        if len(self._bgr) > self._maxsize:
            self._bgr.popitem(last=False)
        return img

    def gray(self, path: Path) -> Any:
        cached = self._gray.get(path)
        if cached is not None:
            self._gray.move_to_end(path)
            return cached
        bgr = self.bgr(path)
        gray = None if bgr is None else cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        self._gray[path] = gray
        if len(self._gray) > self._maxsize:
            self._gray.popitem(last=False)
        return gray


# ── per-frame metrics ──────────────────────────────────────────────────


class _FrameMetrics(NamedTuple):
    dhash_dist: float
    pixel_diff: float
    color_hist: float
    ssim: float
    edge_diff: float


def _compute_dhash(gray: Any, hash_size: int) -> Any:
    """dHash of an (already PiP-masked) grayscale frame via imagehash.

    Takes grayscale (reused from the decode cache, which derives it for
    the other metrics) — ``imagehash.dhash`` converts to ``"L"``
    internally, so feeding gray skips a redundant BGR→RGB conversion.
    cv2 ``BGR2GRAY`` and PIL ``convert("L")`` share the ITU-R 601 luma, so
    the coarse 17px gradient hash is unchanged.
    """
    return imagehash.dhash(Image.fromarray(gray), hash_size=hash_size)


def _compute_ssim(gray1: Any, gray2: Any, *, win_size: int = 11) -> float:
    """Mean SSIM between two grayscale frames (Gaussian-window, no skimage)."""
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    g1 = gray1.astype(np.float64)
    g2 = gray2.astype(np.float64)
    mu1 = cv2.GaussianBlur(g1, (win_size, win_size), 1.5)
    mu2 = cv2.GaussianBlur(g2, (win_size, win_size), 1.5)
    sigma1_sq = cv2.GaussianBlur(g1 * g1, (win_size, win_size), 1.5) - mu1 * mu1
    sigma2_sq = cv2.GaussianBlur(g2 * g2, (win_size, win_size), 1.5) - mu2 * mu2
    sigma12 = cv2.GaussianBlur(g1 * g2, (win_size, win_size), 1.5) - mu1 * mu2
    num = (2 * mu1 * mu2 + c1) * (2 * sigma12 + c2)
    # c1/c2 are the SSIM stability constants — they already floor den at
    # ~c1*c2 (~380), so it is never near zero. The epsilon is purely
    # belt-and-suspenders against float-variance pathology / NaN.
    den = (mu1 * mu1 + mu2 * mu2 + c1) * (sigma1_sq + sigma2_sq + c2) + 1e-12
    return float(np.mean(num / den))


def _color_hist_distance(bgr1: Any, bgr2: Any) -> float:
    """Bhattacharyya distance between BGR colour histograms."""
    if bgr1 is None or bgr2 is None:
        return 1.0
    bins = [8, 8, 8]
    rng = [0, 256, 0, 256, 0, 256]
    h1 = cv2.calcHist([bgr1], [0, 1, 2], None, bins, rng)
    h2 = cv2.calcHist([bgr2], [0, 1, 2], None, bins, rng)
    cv2.normalize(h1, h1)
    cv2.normalize(h2, h2)
    return float(cv2.compareHist(h1, h2, cv2.HISTCMP_BHATTACHARYYA))


def _flow_coherence(gray1: Any, gray2: Any, *, downscale_max: int) -> float:
    """Optical-flow coherence in [0, 1] (1 = coherent motion, 0 = chaotic).

    Computed on a downscaled grayscale — flow coherence (a cosine
    similarity of motion angles) is scale-tolerant, so this is a free
    speedup vs full-res Farneback (DD-2.4-B behavioural-equivalence note).
    """
    if gray1 is None or gray2 is None:
        return 0.0
    g1, g2 = _downscale(gray1, downscale_max), _downscale(gray2, downscale_max)
    no_flow: Any = None  # cv2 stub rejects a bare None literal for `flow`
    flow = cv2.calcOpticalFlowFarneback(g1, g2, no_flow, 0.5, 3, 15, 3, 5, 1.2, 0)
    mag, ang = cv2.cartToPolar(flow[..., 0], flow[..., 1])
    significant = mag > _FLOW_MIN_MAGNITUDE_PX
    if int(np.count_nonzero(significant)) < _FLOW_MIN_SIGNIFICANT_PX:
        return 0.0
    sig_angles = ang[significant]
    mean_angle = float(
        np.arctan2(np.mean(np.sin(sig_angles)), np.mean(np.cos(sig_angles)))
    )
    return max(float(np.mean(np.cos(sig_angles - mean_angle))), 0.0)


def _downscale(gray: Any, max_side: int) -> Any:
    """Downscale a grayscale frame so its longest side <= ``max_side``."""
    h, w = gray.shape[:2]
    longest = max(h, w)
    if longest <= max_side:
        return gray
    scale = max_side / longest
    return cv2.resize(gray, (round(w * scale), round(h * scale)))


def _compare_frames(
    cache: _DecodeCache,
    prev: _Entry,
    cur: _Entry,
    p: SamplingParams,
) -> _FrameMetrics:
    """All 5 dedup metrics between two frames (PiP-masked, cached decode)."""
    max_bits = p.hash_size * p.hash_size
    if prev.dhash is None or cur.dhash is None:
        dhash_dist = 1.0  # undecodable frame → treat as changed (keep, safe)
    else:
        dhash_dist = float((prev.dhash - cur.dhash) / max_bits)

    bgr1, bgr2 = cache.bgr(prev.path), cache.bgr(cur.path)
    if bgr1 is None or bgr2 is None:
        return _FrameMetrics(dhash_dist, 1.0, 1.0, 0.0, 1.0)
    gray1, gray2 = cache.gray(prev.path), cache.gray(cur.path)

    abs_diff = cv2.absdiff(gray1, gray2)
    changed = int(np.count_nonzero(abs_diff > p.pixel_noise_floor))
    pixel_diff = changed / (gray1.shape[0] * gray1.shape[1])

    color_hist = _color_hist_distance(bgr1, bgr2)
    ssim = _compute_ssim(gray1, gray2)

    edges1, edges2 = cv2.Canny(gray1, 50, 150), cv2.Canny(gray2, 50, 150)
    edge_changed = int(np.count_nonzero(cv2.absdiff(edges1, edges2)))
    max_edges = max(int(np.count_nonzero(edges1)), int(np.count_nonzero(edges2)), 1)
    edge_diff = edge_changed / max_edges

    return _FrameMetrics(dhash_dist, pixel_diff, color_hist, ssim, edge_diff)


# ── PiP detection ──────────────────────────────────────────────────────


def _build_zones(width: int, height: int) -> dict[str, _Rect]:
    """Eight candidate zones: 4 corners + 4 edge centres."""
    zw, zh = width // 4, height // 3
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
    """Detect a PiP overlay via temporal motion per zone (highest-motion wins)."""
    zones = _build_zones(width, height)
    zone_totals: dict[str, float] = dict.fromkeys(zones, 0.0)
    pairs = 0

    step = max(1, len(frame_paths) // (max_pairs * 2))
    indices = list(range(0, len(frame_paths) - 1, step))[:max_pairs]
    for i in indices:
        img1, img2 = (
            cv2.imread(str(frame_paths[i])),
            cv2.imread(str(frame_paths[i + 1])),
        )
        if img1 is None or img2 is None:
            continue
        gray = cv2.cvtColor(cv2.absdiff(img1, img2), cv2.COLOR_BGR2GRAY)
        for name, rect in zones.items():
            zone_totals[name] += float(
                np.mean(gray[rect.y1 : rect.y2, rect.x1 : rect.x2])
            )
        pairs += 1

    if pairs == 0:
        return None
    zone_avg = {z: v / pairs for z, v in zone_totals.items()}
    ranked = sorted(zone_avg.items(), key=lambda x: x[1], reverse=True)
    best_name, best_motion = ranked[0]
    second_motion = ranked[1][1] if len(ranked) > 1 else 0.0
    confidence = (best_motion - second_motion) / (best_motion + 1e-6)

    if confidence < confidence_threshold or best_motion < _PIP_MIN_ZONE_MOTION:
        return None
    r = zones[best_name]
    return PiPMask(
        x=r.x1,
        y=r.y1,
        width=r.x2 - r.x1,
        height=r.y2 - r.y1,
        confidence=round(confidence, 4),
    )


# ── dedup voting ───────────────────────────────────────────────────────


def _dedup_voting(
    entries: list[_Entry],
    p: SamplingParams,
    mask: _Rect | None,
) -> list[_Entry]:
    """Drop redundant frames vs the last *kept* frame (tiered voting).

    Tier 1: a single strong signal (dHash or pixel_diff) → keep. Tier 2:
    >= ``min_votes`` of the 5 metrics vote "changed" → keep (catches
    subtle code-slide changes a single metric misses).
    """
    if not entries:
        return []
    cache = _DecodeCache(mask)
    kept = [entries[0]]
    for cur in entries[1:]:
        m = _compare_frames(cache, kept[-1], cur, p)
        if m.dhash_dist > p.tier1_dhash or m.pixel_diff > p.tier1_pixel_diff:
            kept.append(cur)
            continue
        votes = (
            (m.dhash_dist > p.vote_dhash)
            + (m.pixel_diff > p.vote_pixel_diff)
            + (m.color_hist > p.vote_color_hist)
            + (m.ssim < p.vote_ssim)
            + (m.edge_diff > p.vote_edge_diff)
        )
        if votes >= p.min_votes:
            kept.append(cur)
    return kept


# ── gap fill (async — ffmpeg single-frame) ─────────────────────────────


async def _fill_gaps(
    entries: list[_Entry],
    video_path: Path,
    gap_dir: Path,
    p: SamplingParams,
) -> list[_Entry]:
    """Re-extract frames where a post-dedup gap exceeds ``gap_fill_max_sec``."""
    if len(entries) < 2:
        return list(entries)
    result: list[_Entry] = []
    fill_idx = 0
    for i, entry in enumerate(entries):
        result.append(entry)
        if i + 1 >= len(entries):
            break
        gap = entries[i + 1].timestamp_sec - entry.timestamp_sec
        if gap <= p.gap_fill_max_sec:
            continue
        n_fill = max(1, round(gap / p.gap_fill_max_sec) - 1)
        sub = gap / (n_fill + 1)
        for j in range(1, n_fill + 1):
            ts = entry.timestamp_sec + sub * j
            fpath = gap_dir / f"fill_{fill_idx:03d}_{round(ts)}s.jpg"
            if await media.extract_single_frame(video_path, ts, fpath):
                result.append(_Entry(path=fpath, timestamp_sec=round(ts, 1)))
                fill_idx += 1
            else:
                logger.warning("video_gap_fill_failed", timestamp=ts)
    result.sort(key=lambda e: e.timestamp_sec)
    return result


# ── scene segmentation (3-gate boundary + change_class) ────────────────


def _segment_scenes(
    entries: list[_Entry],
    p: SamplingParams,
    mask: _Rect | None,
) -> list[Scene]:
    """Assign ``change_class`` (3-gate boundary) and group into scenes.

    Boundary requires ALL of: dHash > threshold AND colour histogram
    changed AND optical-flow incoherent — plus a time gap forces one.
    The 3 gates suppress false BOUNDARYs (rotation/scroll/gesture →
    fake scene + costly Pass 1 anchor).
    """
    if not entries:
        return []
    cache = _DecodeCache(mask)
    max_bits = p.hash_size * p.hash_size

    for entry in entries:
        if entry.dhash is None:
            gray = cache.gray(entry.path)
            entry.dhash = (
                _compute_dhash(gray, p.hash_size) if gray is not None else None
            )

    classes: list[ChangeClass] = []
    for i, entry in enumerate(entries):
        if i == 0:
            classes.append(ChangeClass.FIRST)
            continue
        prev = entries[i - 1]
        time_gap = entry.timestamp_sec - prev.timestamp_sec
        dist = (
            float((prev.dhash - entry.dhash) / max_bits)
            if prev.dhash is not None and entry.dhash is not None
            else 1.0
        )
        classes.append(_classify(prev, entry, dist, time_gap, p, cache))

    return _group_scenes(entries, classes)


def _classify(
    prev: _Entry,
    cur: _Entry,
    dist: float,
    time_gap: float,
    p: SamplingParams,
    cache: _DecodeCache,
) -> ChangeClass:
    """3-gate boundary decision for a single consecutive pair."""
    if time_gap > p.scene_boundary_time_gap:
        return ChangeClass.BOUNDARY
    if dist <= p.scene_boundary_dhash:
        return ChangeClass.MEDIUM if dist > p.medium_change_dhash else ChangeClass.LOW
    # Gate 1 (dHash) passed → gate 2 (colour).
    if _color_hist_distance(cache.bgr(prev.path), cache.bgr(cur.path)) <= (
        p.scene_boundary_color_hist
    ):
        return ChangeClass.MEDIUM  # colours same → motion, not a new scene
    # Gate 3 (flow): coherent motion (rotation/scroll) → same scene.
    coherence = _flow_coherence(
        cache.gray(prev.path),
        cache.gray(cur.path),
        downscale_max=p.flow_downscale_max,
    )
    if coherence > p.scene_boundary_flow_coherence:
        return ChangeClass.MEDIUM
    return ChangeClass.BOUNDARY  # all three gates passed


def _group_scenes(entries: list[_Entry], classes: list[ChangeClass]) -> list[Scene]:
    """Split frames into scenes at each BOUNDARY; nest frames per scene."""
    starts = [0] + [
        i for i in range(1, len(entries)) if classes[i] == ChangeClass.BOUNDARY
    ]
    scenes: list[Scene] = []
    for s_idx, start in enumerate(starts):
        end = starts[s_idx + 1] - 1 if s_idx + 1 < len(starts) else len(entries) - 1
        frames = [
            SampledFrame(
                frame_position_ms=round(entries[k].timestamp_sec * 1000),
                change_class=classes[k],
                frame_path=entries[k].path,
            )
            for k in range(start, end + 1)
        ]
        scenes.append(
            Scene(
                scene_id=s_idx,
                start_ms=round(entries[start].timestamp_sec * 1000),
                end_ms=round(entries[end].timestamp_sec * 1000),
                frames=frames,
            )
        )
    return scenes


# ── orchestration ──────────────────────────────────────────────────────


def _mask_rect(pip_mask: PiPMask | None) -> _Rect | None:
    if pip_mask is None:
        return None
    return _Rect(
        pip_mask.x,
        pip_mask.y,
        pip_mask.x + pip_mask.width,
        pip_mask.y + pip_mask.height,
    )


def _resolution(file_metadata: VideoFileMetadata, first_frame: Path) -> tuple[int, int]:
    """Resolution from ffprobe metadata (Krok 1), falling back to a frame."""
    parts = file_metadata.resolution.split("x")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        return int(parts[0]), int(parts[1])
    img = cv2.imread(str(first_frame))
    if img is None:
        raise ProcessingError(f"Cannot read extracted frame {first_frame.name}.")
    h, w = img.shape[:2]
    return int(w), int(h)


def _pip_and_dedup(
    raw_paths: list[Path],
    p: SamplingParams,
    width: int,
    height: int,
    interval: float,
) -> tuple[PiPMask | None, list[_Entry]]:
    """Sync block: PiP detect + dHash + dedup voting (run off the loop)."""
    pip_mask = _detect_pip(raw_paths, width, height)
    mask = _mask_rect(pip_mask)
    cache = _DecodeCache(mask)
    entries: list[_Entry] = []
    for i, path in enumerate(raw_paths):
        gray = cache.gray(path)
        dhash = _compute_dhash(gray, p.hash_size) if gray is not None else None
        entries.append(
            _Entry(path=path, timestamp_sec=round(i * interval, 2), dhash=dhash)
        )
    deduped = _dedup_voting(entries, p, mask)
    return pip_mask, deduped


async def detect(
    video_path: Path,
    file_metadata: VideoFileMetadata,
    frames_dir: Path,
    *,
    params: SamplingParams | None = None,
) -> DetectionResult:
    """Krok 3 — extract → PiP detect → dedup → gap fill → scene segment.

    JPEGs land in ``frames_dir`` (processor-owned tempdir, cleaned with
    the rest). Heavy cv2 work runs off the event loop via
    ``asyncio.to_thread``; gap fill is async (ffmpeg single-frame).
    """
    p = params or _DEFAULT_PARAMS
    raw_paths = await media.extract_frames_fps(video_path, p.fps, frames_dir / "frames")
    width, height = _resolution(file_metadata, raw_paths[0])
    interval = 1.0 / p.fps

    pip_mask, deduped = await asyncio.to_thread(
        _pip_and_dedup, raw_paths, p, width, height, interval
    )
    filled = await _fill_gaps(deduped, video_path, frames_dir / "gap_fill", p)
    scenes = await asyncio.to_thread(_segment_scenes, filled, p, _mask_rect(pip_mask))

    logger.info(
        "video_detection_done",
        raw_frames=len(raw_paths),
        kept_frames=sum(len(s.frames) for s in scenes),
        scenes=len(scenes),
        pip=pip_mask is not None,
    )
    return DetectionResult(scenes=scenes, pip_mask=pip_mask)
