"""Subprocess wrappers for the video media toolchain (Phase 2.4 task 2.4.2).

Thin, pipeline-agnostic adapters over the external binaries the video
ingestion edges (Krok 1-2) need: yt-dlp (URL video-stream download),
ffprobe (container metadata), ffmpeg (audio-track extraction). Each
function shells out and maps failure onto the task 2.4.2 taxonomy (D3):

* :class:`~course_supporter.ingestion.base.UnsupportedFormatError` —
  *constraint* failures (size/duration cap, corrupt/unsupported
  container).
* :class:`~course_supporter.ingestion.base.ProcessingError` —
  *operational* failures (download fail, ffmpeg extract fail, missing
  binary, timeout).

No ``SourceDocument`` / ORM / Redis knowledge lives here — pure value
in, value out. The pure parsing helper :func:`_parse_probe` is
unit-tested directly; the subprocess orchestration runs for real only
in the ``requires_ffmpeg`` integration test. Isolated from the
``course_supporter.vd`` module (task 2.4.1 acceptance #3; ``vd/`` removed in
task 2.4.9A).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import structlog

from course_supporter.ingestion.base import (
    CategorisedProcessingError,
    ProcessingError,
    UnsupportedFormatError,
)
from course_supporter.ingestion.video_pipeline.schemas import VideoFileMetadata
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.security.policies import AUTHORED_POLICY

logger = structlog.get_logger()

# The authored policy's video DOWNLOAD cap (5 GB) -- the ceiling on a video
# fetched from a source URL, distinct from the tighter upload cap the create
# route enforces (ARC.md §9). Enforced worker-side for the URL path
# because the HTTP ``file.size`` guard does not reach yt-dlp downloads (D2).
# Both layers — yt-dlp ``--max-filesize`` and the post-download check — derive
# from this byte value (passing bytes to yt-dlp also avoids the ``5G`` unit
# ambiguity). ``max_video_download_bytes`` is Optional on ``ContextPolicy``;
# the authored policy always sets it, and the fallback keeps the type ``int``
# with a safe default.
_MAX_VIDEO_DOWNLOAD_BYTES: int = (
    AUTHORED_POLICY.max_video_download_bytes or 5 * 1024 * 1024 * 1024
)

# yt-dlp download (URL path). No per-video duration/size is known before the
# download (ffprobe runs *after* it), so the budget is worst-case: the 5 GB
# size cap at a conservatively low 1 MiB/s, clamped to ``[FLOOR, CAP]``. A
# large file over a slow link then finishes instead of dying at the
# dev-calibrated flat 600 s (task 3.3c-B). CAP stays below
# ``worker_job_timeout`` (21600 s) so the download alone cannot consume the
# whole job budget.
_DOWNLOAD_TIMEOUT_FLOOR_SEC = 600.0
_DOWNLOAD_MIN_THROUGHPUT_BPS = 1024 * 1024  # 1 MiB/s assumed worst-case bandwidth
_DOWNLOAD_TIMEOUT_CAP_SEC = 10800.0
_DOWNLOAD_TIMEOUT_SEC = min(
    max(
        _DOWNLOAD_TIMEOUT_FLOOR_SEC,
        _MAX_VIDEO_DOWNLOAD_BYTES / _DOWNLOAD_MIN_THROUGHPUT_BPS,
    ),
    _DOWNLOAD_TIMEOUT_CAP_SEC,
)
_PROBE_TIMEOUT_SEC = 30.0

# Audio extraction (Krok 2). The original flat 600 s was dev-calibrated and
# runs thin on the 2-vCPU prod worker for a 150-min module: the full mp3
# re-encode (``-vn``, audio-only — no video decode) is ~15-25x realtime, so
# ~360-600 s wall-clock at the 150-min cap. The timeout is now duration-
# proportional via :func:`audio_extract_timeout_for` (same shape as
# frame-extract, ``_proportional_timeout``): FLOOR preserves fail-fast for a
# stuck short clip; BUDGET (0.2 s wall per 1 s audio) allots ~3x the conservative
# worst-case; CAP is the 150-min budget and stays far below ``worker_job_timeout``.
_AUDIO_EXTRACT_TIMEOUT_FLOOR_SEC = 600.0
_AUDIO_EXTRACT_REALTIME_BUDGET = 0.2  # wall-clock budget per second of audio
_AUDIO_EXTRACT_TIMEOUT_CAP_SEC = 1800.0

# Frame extraction (Krok 3). The original flat 900 s was calibrated on dev
# hardware (M2) and times long videos out on the 2-vCPU prod worker before
# the full-resolution decode — whose wall-clock scales ~linearly with video
# duration — can finish. The timeout is now duration-proportional via
# :func:`frame_extract_timeout_for`: FLOOR preserves fail-fast for a
# genuinely-stuck short video; CAP keeps the step under half of
# ``worker_job_timeout`` so the rest of the pipeline (STT, cv2 dedup, Pass 1/2
# LLM) still has budget for any in-contract (<=150 min) video. The CAP is a
# ceiling for pathological out-of-contract inputs only — an in-contract video
# stays far below it (task 3.3c-B).
_FRAME_EXTRACT_TIMEOUT_FLOOR_SEC = 900.0
_FRAME_EXTRACT_REALTIME_BUDGET = 1.0  # wall-clock budget per second of video
_FRAME_EXTRACT_TIMEOUT_CAP_SEC = 10800.0
_SINGLE_FRAME_TIMEOUT_SEC = 30.0

# Bytes of stderr surfaced in error messages — the *tail*, since ffmpeg /
# yt-dlp print version/config banners first and the actual error last.
_ERR_TAIL = 1024


def _proportional_timeout(
    duration_sec: float, *, floor: float, budget: float, cap: float
) -> float:
    """Duration-proportional wall-clock timeout, clamped to ``[floor, cap]``.

    The shared shape for the ffmpeg media-stage timeouts (frame extraction,
    audio extraction): scale the budget with the source duration so a stage
    that runs longer on slower hardware is not killed mid-work, while
    ``floor`` keeps fail-fast for a stuck short input and ``cap`` bounds
    pathological out-of-contract durations. Per-stage constants differ; the
    formula is one.

    >>> _proportional_timeout(60.0, floor=900.0, budget=1.0, cap=10800.0)
    900.0
    >>> _proportional_timeout(9000.0, floor=900.0, budget=1.0, cap=10800.0)
    9000.0
    """
    return min(max(floor, duration_sec * budget), cap)


def frame_extract_timeout_for(duration_sec: float) -> float:
    """Return a duration-proportional frame-extraction timeout (task 3.3c-B).

    Scales the ffmpeg frame-extraction budget with the video duration,
    clamped to ``[FLOOR, CAP]``. ``FLOOR`` (900 s) keeps fail-fast for a
    genuinely-stuck short video; ``CAP`` (10800 s, half of
    ``worker_job_timeout``) bounds pathological out-of-contract inputs so
    the remaining pipeline retains budget.

    Args:
        duration_sec: Source video duration in seconds (from ffprobe).

    Returns:
        Timeout in seconds for the bulk ``ffmpeg -vf fps=...`` extraction.

    >>> frame_extract_timeout_for(60.0)  # 1-min video clamps to FLOOR
    900.0
    >>> frame_extract_timeout_for(9000.0)  # 150-min video: linear budget
    9000.0
    >>> frame_extract_timeout_for(36000.0)  # 10-h video clamps to CAP
    10800.0
    """
    return _proportional_timeout(
        duration_sec,
        floor=_FRAME_EXTRACT_TIMEOUT_FLOOR_SEC,
        budget=_FRAME_EXTRACT_REALTIME_BUDGET,
        cap=_FRAME_EXTRACT_TIMEOUT_CAP_SEC,
    )


def audio_extract_timeout_for(duration_sec: float) -> float:
    """Return a duration-proportional audio-extraction timeout (task M1+M2).

    Sibling of :func:`frame_extract_timeout_for` for the ``-vn`` mp3
    re-encode. Audio-only encode is ~15-25x realtime, so ``BUDGET`` (0.2 s
    wall per 1 s audio) allots ~3x the conservative worst-case; ``FLOOR``
    (600 s) preserves fail-fast for a short clip; ``CAP`` (1800 s) is the
    150-min budget and stays far below ``worker_job_timeout``.

    Args:
        duration_sec: Source video duration in seconds (from ffprobe).

    Returns:
        Timeout in seconds for the ``ffmpeg -vn`` audio extraction.

    >>> audio_extract_timeout_for(60.0)  # 1-min clamps to FLOOR
    600.0
    >>> audio_extract_timeout_for(3240.0)  # 54-min: proportional budget
    648.0
    >>> audio_extract_timeout_for(9000.0)  # 150-min clamps to CAP
    1800.0
    """
    return _proportional_timeout(
        duration_sec,
        floor=_AUDIO_EXTRACT_TIMEOUT_FLOOR_SEC,
        budget=_AUDIO_EXTRACT_REALTIME_BUDGET,
        cap=_AUDIO_EXTRACT_TIMEOUT_CAP_SEC,
    )


async def _run(cmd: list[str], *, timeout_sec: float) -> tuple[int, bytes, bytes]:
    """Run ``cmd`` to completion, returning ``(returncode, stdout, stderr)``.

    Raises :class:`ProcessingError` when the binary is missing or the
    command exceeds ``timeout_sec`` (the process is killed first).
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ProcessingError(f"Required binary not found: {cmd[0]} ({exc}).") from exc

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ProcessingError(
            f"Command timed out after {timeout_sec:.0f}s: {cmd[0]}."
        ) from None

    returncode = proc.returncode if proc.returncode is not None else 1
    return returncode, out, err


async def download_video(url: str, dest_dir: Path) -> Path:
    """Download a video *stream* from ``url`` via yt-dlp (Krok 1, URL path).

    Downloads best video+audio merged into an mp4 container (NOT
    audio-only — the legacy ``WhisperVideoProcessor`` audio-only download
    is explicitly not the model, R0.2). Host allowlist is best-effort
    (D2): an unsupported/private host surfaces as a yt-dlp non-zero exit
    → ``ProcessingError``. The 5 GB cap is enforced both via
    ``--max-filesize`` and a post-download size check.
    """
    out_template = str(dest_dir / "source.%(ext)s")
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-f",
        "bv*+ba/b",
        "--merge-output-format",
        "mp4",
        "--max-filesize",
        str(_MAX_VIDEO_DOWNLOAD_BYTES),
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "-o",
        out_template,
        url,
    ]
    rc, _out, err = await _run(cmd, timeout_sec=_DOWNLOAD_TIMEOUT_SEC)
    if rc != 0:
        raise CategorisedProcessingError(
            ErrorCategory.EXTERNAL_SOURCE_UNAVAILABLE,
            f"yt-dlp download failed (code {rc}) for {url}: "
            f"{err.decode(errors='replace')[-_ERR_TAIL:]}",
        )
    # Filesystem inspection off the event loop (ASYNC240).
    return await asyncio.to_thread(_resolve_download, dest_dir, url)


def _resolve_download(dest_dir: Path, url: str) -> Path:
    """Locate the merged yt-dlp output and enforce the 5 GB cap (sync)."""
    candidates = [p for p in dest_dir.glob("source.*") if p.is_file()]
    if not candidates:
        raise CategorisedProcessingError(
            ErrorCategory.EXTERNAL_SOURCE_UNAVAILABLE,
            f"yt-dlp produced no output file for {url}.",
        )
    video_path = max(candidates, key=lambda p: p.stat().st_size)

    size = video_path.stat().st_size
    if size > _MAX_VIDEO_DOWNLOAD_BYTES:
        raise UnsupportedFormatError(
            f"Downloaded video ({size / 1024**3:.2f} GB) exceeds maximum "
            f"{_MAX_VIDEO_DOWNLOAD_BYTES // 1024**3} GB."
        )
    return video_path


async def probe_metadata(path: Path) -> VideoFileMetadata:
    """Probe container metadata via ffprobe (Krok 1, §1 ``file_metadata``).

    Returns duration_ms / codec / resolution. A corrupt or non-video
    container surfaces as ``UnsupportedFormatError`` (constraint, D3).
    """
    rc, out, err = await _run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout_sec=_PROBE_TIMEOUT_SEC,
    )
    if rc != 0:
        raise UnsupportedFormatError(
            f"ffprobe failed (code {rc}) on {path.name}: "
            f"{err.decode(errors='replace')[-_ERR_TAIL:]}"
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise UnsupportedFormatError(
            f"ffprobe returned invalid JSON for {path.name}: {exc}."
        ) from exc
    return _parse_probe(data, source_name=path.name)


def _parse_probe(data: dict[str, Any], *, source_name: str) -> VideoFileMetadata:
    """Map ffprobe ``-show_format -show_streams`` JSON to ``VideoFileMetadata``.

    Pure (no I/O) so it can be unit-tested with recorded ffprobe output.
    Raises ``UnsupportedFormatError`` when there is no video stream or
    no derivable duration (corrupt / unsupported container).
    """
    streams = data.get("streams") or []
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    if not video_streams:
        raise UnsupportedFormatError(
            f"No video stream found in {source_name} "
            f"(unsupported or corrupt container)."
        )
    vs = video_streams[0]
    fmt = data.get("format") or {}

    duration_s = fmt.get("duration") or vs.get("duration")
    if duration_s is None:
        raise UnsupportedFormatError(
            f"Could not determine duration for {source_name} "
            f"(corrupt or unsupported container)."
        )
    duration_ms = round(float(duration_s) * 1000)

    codec = str(vs.get("codec_name") or "unknown")
    width, height = vs.get("width"), vs.get("height")
    resolution = f"{width}x{height}" if width and height else "unknown"

    return VideoFileMetadata(
        duration_ms=duration_ms,
        codec=codec,
        resolution=resolution,
    )


# DD-3.3c-I-B intake duration probes. Run BEFORE enqueue so the admission
# gate can sum pending video-hours already in the queue. Metadata-only: the
# full video download still happens later in ``step_1_ingest``; these only
# resolve the duration cheaply (ffprobe over the already-local upload bytes;
# yt-dlp resolving the URL's metadata WITHOUT downloading the stream).
_INTAKE_METADATA_TIMEOUT_SEC = 60.0


async def probe_intake_duration_sec(url: str) -> float:
    """Resolve a remote video's duration (seconds) via yt-dlp, no download.

    Runs ``yt-dlp --dump-json --skip-download`` and reads the ``duration``
    field. A private / unavailable / unsupported URL (yt-dlp non-zero exit),
    invalid JSON, or a response without a numeric duration (e.g. a live
    stream) raises :class:`UnsupportedFormatError` so the caller surfaces a
    clear intake rejection instead of a 500.
    """
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--dump-json",
        "--skip-download",
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        url,
    ]
    rc, out, err = await _run(cmd, timeout_sec=_INTAKE_METADATA_TIMEOUT_SEC)
    if rc != 0:
        raise UnsupportedFormatError(
            f"yt-dlp metadata probe failed (code {rc}) for {url}: "
            f"{err.decode(errors='replace')[-_ERR_TAIL:]}"
        )
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise UnsupportedFormatError(
            f"yt-dlp returned invalid metadata JSON for {url}: {exc}."
        ) from exc
    duration = data.get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        raise UnsupportedFormatError(
            f"yt-dlp could not determine a duration for {url} "
            f"(live stream or unsupported)."
        )
    return float(duration)


async def probe_intake_duration_from_bytes(
    content: bytes, *, filename: str = "upload"
) -> float:
    """Resolve an uploaded video's duration (seconds) via ffprobe on bytes.

    ffprobe needs a seekable input to read the container index, so the
    already-in-memory upload bytes are written to a temp file and handed to
    :func:`probe_metadata` (reusing its corrupt/non-video handling). The
    temp file is removed afterwards. Filesystem work runs off the event loop
    (ASYNC240). Raises :class:`UnsupportedFormatError` on a corrupt or
    non-video container.
    """
    suffix = Path(filename).suffix or ".bin"

    def _write_temp() -> Path:
        fd, name = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        return Path(name)

    tmp = await asyncio.to_thread(_write_temp)
    try:
        metadata = await probe_metadata(tmp)
    finally:
        await asyncio.to_thread(tmp.unlink, missing_ok=True)
    return metadata.duration_ms / 1000


async def extract_audio(
    video_path: Path,
    dest_dir: Path,
    *,
    timeout_sec: float = _AUDIO_EXTRACT_TIMEOUT_FLOOR_SEC,
) -> Path:
    """Extract the audio track to mono 16 kHz mp3 via ffmpeg (Krok 2).

    Output format aligns with the ElevenLabs Scribe core: the provider
    derives its multipart content-type from the file extension
    (``guess_content_type`` → ``.mp3`` = ``audio/mpeg``); mono/16 kHz is
    a free, Scribe-compatible STT canonical (the core imposes no
    sample-rate constraint). Failure → ``ProcessingError`` (operational).

    Args:
        video_path: Local source video.
        dest_dir: Processor-owned tempdir for the extracted mp3.
        timeout_sec: Wall-clock ceiling for the ``-vn`` re-encode. The
            caller derives a duration-proportional value via
            :func:`audio_extract_timeout_for`; the default keeps the
            dev-flat FLOOR for callers without a probed duration.
    """
    audio_path = dest_dir / "audio.mp3"
    rc, _out, err = await _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "mp3",
            str(audio_path),
        ],
        timeout_sec=timeout_sec,
    )
    if rc != 0:
        raise ProcessingError(
            f"ffmpeg audio extraction failed (code {rc}) on "
            f"{video_path.name}: {err.decode(errors='replace')[-_ERR_TAIL:]}"
        )
    if not await asyncio.to_thread(audio_path.exists):
        raise ProcessingError(f"ffmpeg produced no audio output for {video_path.name}.")
    return audio_path


async def extract_frames_fps(
    video_path: Path,
    fps: float,
    dest_dir: Path,
    *,
    timeout_sec: float = _FRAME_EXTRACT_TIMEOUT_FLOOR_SEC,
) -> list[Path]:
    """Extract frames at a fixed ``fps`` into ``dest_dir`` as JPEGs (Krok 3).

    Returns the sorted JPEG paths. ffmpeg failure or zero frames →
    ``ProcessingError`` (operational; an undecodable stream yields no
    frames). Filesystem inspection runs off the event loop (ASYNC240).

    Args:
        video_path: Source video file.
        fps: Output sampling rate (frames per second).
        dest_dir: Target directory for the JPEG sequence.
        timeout_sec: Wall-clock ceiling for the bulk ffmpeg decode. The
            caller passes a duration-proportional value via
            :func:`frame_extract_timeout_for`; the default keeps the
            historical fail-fast FLOOR for callers without a duration
            (task 3.3c-B).
    """
    await asyncio.to_thread(dest_dir.mkdir, parents=True, exist_ok=True)
    pattern = str(dest_dir / "frame_%06d.jpg")
    rc, _out, err = await _run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vf",
            f"fps={fps}",
            "-q:v",
            "2",
            pattern,
        ],
        timeout_sec=timeout_sec,
    )
    if rc != 0:
        raise ProcessingError(
            f"ffmpeg frame extraction failed (code {rc}) on "
            f"{video_path.name}: {err.decode(errors='replace')[-_ERR_TAIL:]}"
        )
    frames = await asyncio.to_thread(_sorted_glob, dest_dir, "frame_*.jpg")
    if not frames:
        raise ProcessingError(
            f"ffmpeg extracted no frames from {video_path.name} "
            f"(empty or undecodable stream)."
        )
    return frames


async def extract_single_frame(
    video_path: Path,
    timestamp_sec: float,
    out_path: Path,
) -> bool:
    """Extract one frame at ``timestamp_sec`` (Krok 3 gap fill).

    Best-effort: returns ``False`` (rather than raising) on ffmpeg
    failure or timeout, so a single missed gap-fill frame does not abort
    the whole pipeline (mirrors the reference behaviour).
    """
    await asyncio.to_thread(out_path.parent.mkdir, parents=True, exist_ok=True)
    try:
        rc, _out, _err = await _run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{timestamp_sec:.2f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(out_path),
            ],
            timeout_sec=_SINGLE_FRAME_TIMEOUT_SEC,
        )
    except ProcessingError:
        return False
    return rc == 0 and await asyncio.to_thread(out_path.exists)


def _sorted_glob(directory: Path, pattern: str) -> list[Path]:
    """Sorted glob (sync helper run off the event loop)."""
    return sorted(p for p in directory.glob(pattern) if p.is_file())
