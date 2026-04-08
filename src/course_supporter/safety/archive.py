"""Safe archive extraction with decompression bomb protection."""

from __future__ import annotations

import gzip
import zipfile
from pathlib import Path

import structlog

from course_supporter.config import get_settings
from course_supporter.models.safety import FileContent, SubmissionContent

logger = structlog.get_logger()

# Text-like extensions that we can read as source code / text
_TEXT_EXTENSIONS = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".java",
        ".c",
        ".cpp",
        ".cs",
        ".sql",
        ".md",
        ".txt",
        ".html",
        ".htm",
        ".ipynb",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".cfg",
        ".ini",
        ".xml",
        ".csv",
        ".sh",
        ".bash",
        ".css",
        ".jsx",
        ".tsx",
        ".rb",
        ".go",
        ".rs",
        ".kt",
        ".swift",
        ".r",
        ".R",
        ".scala",
        ".h",
        ".hpp",
    }
)


class ArchiveExtractionError(Exception):
    """Raised when archive extraction fails or violates safety limits."""


def _read_text_file(path: Path) -> str:
    """Read a file as text, trying common encodings."""
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def _is_text_file(filename: str) -> bool:
    """Check if a filename has a text-like extension."""
    return Path(filename).suffix.lower() in _TEXT_EXTENSIONS


async def extract_submission_content(file_path: Path) -> SubmissionContent:
    """Extract text content from a submission file or archive.

    Supports:
    - Single text files (read directly)
    - .zip archives (extract with bomb protection)
    - .gz files (decompress single file)

    Limits are configurable via environment variables.

    Args:
        file_path: Path to the submission file.

    Returns:
        SubmissionContent with extracted file contents.

    Raises:
        ArchiveExtractionError: If extraction fails or limits exceeded.
    """
    suffix = file_path.suffix.lower()

    if suffix == ".zip":
        return _extract_zip(file_path)
    if suffix == ".gz":
        return _extract_gz(file_path)
    return _read_single_file(file_path)


def _read_single_file(file_path: Path) -> SubmissionContent:
    """Read a single text file."""
    content = _read_text_file(file_path)
    fc = FileContent(
        filename=file_path.name,
        content=content,
        size=len(content.encode("utf-8")),
    )
    return SubmissionContent(files=[fc], total_size=fc.size)


def _extract_zip(file_path: Path) -> SubmissionContent:
    """Extract a ZIP archive with decompression bomb protection."""
    settings = get_settings()
    max_bytes = settings.safety_archive_max_uncompressed_mb * 1024 * 1024
    max_files = settings.safety_archive_max_files
    max_nesting = settings.safety_archive_max_nesting

    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            # Check for nested archives
            for info in zf.infolist():
                nested_suffix = Path(info.filename).suffix.lower()
                if (
                    nested_suffix in (".zip", ".gz", ".tar", ".bz2", ".xz")
                    and max_nesting < 2
                ):
                    msg = (
                        f"Nested archive detected: {info.filename}. "
                        f"Max nesting depth: {max_nesting}"
                    )
                    raise ArchiveExtractionError(msg)

            infos = [i for i in zf.infolist() if not i.is_dir()]

            if len(infos) > max_files:
                msg = (
                    f"Archive contains {len(infos)} files, maximum allowed: {max_files}"
                )
                raise ArchiveExtractionError(msg)

            # Check total uncompressed size
            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed > max_bytes:
                msg = (
                    f"Archive uncompressed size ({total_uncompressed} bytes) "
                    f"exceeds limit ({max_bytes} bytes)"
                )
                raise ArchiveExtractionError(msg)

            files: list[FileContent] = []
            total_size = 0

            for info in infos:
                if not _is_text_file(info.filename):
                    logger.debug(
                        "skipping_non_text_file",
                        filename=info.filename,
                    )
                    continue

                raw = zf.read(info.filename)
                total_size += len(raw)

                if total_size > max_bytes:
                    msg = (
                        f"Cumulative extracted size ({total_size} bytes) "
                        f"exceeds limit ({max_bytes} bytes)"
                    )
                    raise ArchiveExtractionError(msg)

                # Decode text
                for encoding in ("utf-8", "utf-8-sig", "latin-1"):
                    try:
                        text = raw.decode(encoding)
                        break
                    except (UnicodeDecodeError, ValueError):
                        continue
                else:
                    text = raw.decode("utf-8", errors="replace")

                files.append(
                    FileContent(
                        filename=info.filename,
                        content=text,
                        size=len(raw),
                    )
                )

            return SubmissionContent(files=files, total_size=total_size)

    except zipfile.BadZipFile as exc:
        msg = f"Invalid ZIP file: {exc}"
        raise ArchiveExtractionError(msg) from exc


def _extract_gz(file_path: Path) -> SubmissionContent:
    """Decompress a .gz file (single file compression)."""
    settings = get_settings()
    max_bytes = settings.safety_archive_max_uncompressed_mb * 1024 * 1024

    try:
        with gzip.open(file_path, "rb") as f:
            raw = f.read(max_bytes + 1)

        if len(raw) > max_bytes:
            msg = f"Decompressed size exceeds limit ({max_bytes} bytes)"
            raise ArchiveExtractionError(msg)

        # Determine inner filename (strip .gz)
        inner_name = file_path.stem
        for encoding in ("utf-8", "utf-8-sig", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except (UnicodeDecodeError, ValueError):
                continue
        else:
            text = raw.decode("utf-8", errors="replace")

        fc = FileContent(filename=inner_name, content=text, size=len(raw))
        return SubmissionContent(files=[fc], total_size=fc.size)

    except gzip.BadGzipFile as exc:
        msg = f"Invalid gzip file: {exc}"
        raise ArchiveExtractionError(msg) from exc
