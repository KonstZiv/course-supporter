"""Shared utilities for STT providers."""

from iso639 import Language, LanguageNotFoundError

# STT request timeout (task M1). The flat ``httpx.Timeout(120.0)`` on the
# provider clients was dev-calibrated on short clips and times out the whole
# synchronous transcription POST for a long module on the prod path (no
# client-side chunking — the entire file is one request; ElevenLabs Scribe
# parallelises internally). The timeout is now duration-proportional from the
# probed AUDIO duration: FLOOR (120 s) is the live-proven short-clip ceiling;
# BUDGET (0.1 s wall per 1 s audio) is generous over Scribe's sub-linear
# parallel processing; CAP (1800 s) bounds the 150-min worst case and stays
# far below ``worker_job_timeout``.
_STT_TIMEOUT_FLOOR_SEC = 120.0
_STT_TIMEOUT_BUDGET = 0.1  # wall-clock budget per second of audio
_STT_TIMEOUT_CAP_SEC = 1800.0


def stt_timeout_for(duration_sec: float | None) -> float:
    """Return a duration-proportional STT request timeout in seconds (task M1).

    A known ``duration_sec`` scales linearly, clamped to ``[FLOOR, CAP]``.
    ``None`` (the caller could not probe the duration — e.g. the pure-audio
    pipeline, where duration is only known *post*-STT) returns ``CAP``: the
    conservative 150-min-contract ceiling, so a long audio is not killed
    mid-transcription rather than fail-fast on the dev-flat floor.

    >>> stt_timeout_for(60.0)  # 1-min clip clamps to FLOOR
    120.0
    >>> stt_timeout_for(9000.0)  # 150-min: proportional budget
    900.0
    >>> stt_timeout_for(None)  # duration unknown -> conservative CAP
    1800.0
    """
    if duration_sec is None:
        return _STT_TIMEOUT_CAP_SEC
    return min(
        max(_STT_TIMEOUT_FLOOR_SEC, duration_sec * _STT_TIMEOUT_BUDGET),
        _STT_TIMEOUT_CAP_SEC,
    )


def guess_content_type(suffix: str) -> str:
    """Map file extension to MIME type for audio files."""
    mapping: dict[str, str] = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }
    return mapping.get(suffix.lower(), "application/octet-stream")


def iso639_1_to_3(code: str) -> str:
    """Convert ISO 639-1 code (e.g. 'uk') to ISO 639-3 (e.g. 'ukr').

    Returns the original code unchanged if conversion fails.
    """
    try:
        result: str = Language.from_part1(code).part3
        return result
    except LanguageNotFoundError:
        return code


def iso639_3_to_1(code: str) -> str | None:
    """Convert ISO 639-3 code (e.g. 'ukr') to ISO 639-1 (e.g. 'uk').

    Returns None if the language has no ISO 639-1 code.
    """
    try:
        lang = Language.from_part3(code)
        return lang.part1 or None
    except LanguageNotFoundError:
        return None
