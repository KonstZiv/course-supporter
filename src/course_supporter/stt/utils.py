"""Shared utilities for STT providers."""

from iso639 import Language, LanguageNotFoundError


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
