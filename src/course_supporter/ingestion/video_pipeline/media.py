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
in the ``requires_ffmpeg`` integration test. Isolated from the legacy
``course_supporter.vd`` module (task 2.4.1 acceptance #3).
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import TYPE_CHECKING, Any

import structlog

from course_supporter.ingestion.base import ProcessingError, UnsupportedFormatError
from course_supporter.ingestion.video_pipeline.schemas import VideoFileMetadata
from course_supporter.security.policies import AUTHORED_POLICY

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger()

# Single source of truth: the authored upload policy's video size cap
# (5 GB). Enforced worker-side for the URL path because the HTTP
# ``file.size`` guard does not reach yt-dlp downloads (D2). Both layers —
# yt-dlp ``--max-filesize`` and the post-download check — derive from this
# byte value (passing bytes to yt-dlp also avoids the ``5G`` unit
# ambiguity). ``max_video_size_bytes`` is Optional on ``ContextPolicy``;
# the authored policy always sets it, and the fallback keeps the type
# ``int`` with a safe default.
_MAX_VIDEO_SIZE_BYTES: int = (
    AUTHORED_POLICY.max_video_size_bytes or 5 * 1024 * 1024 * 1024
)

_DOWNLOAD_TIMEOUT_SEC = 600.0
_PROBE_TIMEOUT_SEC = 30.0
_EXTRACT_TIMEOUT_SEC = 600.0

# Bytes of stderr surfaced in error messages — the *tail*, since ffmpeg /
# yt-dlp print version/config banners first and the actual error last.
_ERR_TAIL = 1024


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
        str(_MAX_VIDEO_SIZE_BYTES),
        "--no-playlist",
        "--quiet",
        "--no-warnings",
        "-o",
        out_template,
        url,
    ]
    rc, _out, err = await _run(cmd, timeout_sec=_DOWNLOAD_TIMEOUT_SEC)
    if rc != 0:
        raise ProcessingError(
            f"yt-dlp download failed (code {rc}) for {url}: "
            f"{err.decode(errors='replace')[-_ERR_TAIL:]}"
        )
    # Filesystem inspection off the event loop (ASYNC240).
    return await asyncio.to_thread(_resolve_download, dest_dir, url)


def _resolve_download(dest_dir: Path, url: str) -> Path:
    """Locate the merged yt-dlp output and enforce the 5 GB cap (sync)."""
    candidates = [p for p in dest_dir.glob("source.*") if p.is_file()]
    if not candidates:
        raise ProcessingError(f"yt-dlp produced no output file for {url}.")
    video_path = max(candidates, key=lambda p: p.stat().st_size)

    size = video_path.stat().st_size
    if size > _MAX_VIDEO_SIZE_BYTES:
        raise UnsupportedFormatError(
            f"Downloaded video ({size / 1024**3:.2f} GB) exceeds maximum "
            f"{_MAX_VIDEO_SIZE_BYTES // 1024**3} GB."
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


async def extract_audio(video_path: Path, dest_dir: Path) -> Path:
    """Extract the audio track to mono 16 kHz mp3 via ffmpeg (Krok 2).

    Output format aligns with the ElevenLabs Scribe core: the provider
    derives its multipart content-type from the file extension
    (``guess_content_type`` → ``.mp3`` = ``audio/mpeg``); mono/16 kHz is
    a free, Scribe-compatible STT canonical (the core imposes no
    sample-rate constraint). Failure → ``ProcessingError`` (operational).
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
        timeout_sec=_EXTRACT_TIMEOUT_SEC,
    )
    if rc != 0:
        raise ProcessingError(
            f"ffmpeg audio extraction failed (code {rc}) on "
            f"{video_path.name}: {err.decode(errors='replace')[-_ERR_TAIL:]}"
        )
    if not await asyncio.to_thread(audio_path.exists):
        raise ProcessingError(f"ffmpeg produced no audio output for {video_path.name}.")
    return audio_path
