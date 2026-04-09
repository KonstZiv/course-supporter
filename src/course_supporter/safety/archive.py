"""Safe archive extraction with decompression bomb protection."""

from __future__ import annotations

import gzip
import stat
import zipfile
from pathlib import Path, PurePosixPath

import anyio
import structlog

from course_supporter.config import get_settings
from course_supporter.models.safety import FileContent, SubmissionContent
from course_supporter.safety.exceptions import (
    ArchiveBombError,
    SecurityWarning,
    SymlinkViolationError,
)

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


def _decode_bytes(raw: bytes) -> str:
    """Decode raw bytes to text, trying common encodings."""
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return raw.decode("utf-8", errors="replace")


def _read_text_file(path: Path) -> str:
    """Read a file as text, trying common encodings."""
    return _decode_bytes(path.read_bytes())


def _is_text_file(filename: str) -> bool:
    """Check if a filename has a text-like extension."""
    return Path(filename).suffix.lower() in _TEXT_EXTENSIONS


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    """Check if a ZIP entry is a symbolic link (Unix external attributes)."""
    return stat.S_ISLNK(info.external_attr >> 16)


def _sanitize_filename(raw_name: str) -> str | None:
    """Sanitize an archive entry filename, stripping path traversal.

    Returns None if the name is unsafe and should be skipped entirely
    (e.g. resolves to empty or tries absolute path).
    """
    parts = PurePosixPath(raw_name).parts
    # Strip leading "/" and all ".." components
    clean_parts = [p for p in parts if p not in ("/", "..")]
    if not clean_parts:
        return None
    return str(PurePosixPath(*clean_parts))


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
    if await anyio.Path(file_path).is_symlink():
        msg = f"Refusing to process symlink: {file_path.name}"
        raise SymlinkViolationError(
            msg,
            details={"filename": file_path.name, "target": str(file_path)},
        )

    suffix = file_path.suffix.lower()

    if suffix == ".zip":
        return await anyio.to_thread.run_sync(_extract_zip, file_path)
    if suffix == ".gz":
        return await anyio.to_thread.run_sync(_extract_gz, file_path)
    return await anyio.to_thread.run_sync(_read_single_file, file_path)


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
            warnings: list[SecurityWarning] = []

            # Check for nested archives
            for info in zf.infolist():
                nested_suffix = PurePosixPath(info.filename).suffix.lower()
                if (
                    nested_suffix in (".zip", ".gz", ".tar", ".bz2", ".xz")
                    and max_nesting < 2
                ):
                    msg = (
                        f"Nested archive detected: {info.filename}. "
                        f"Max nesting depth: {max_nesting}"
                    )
                    raise ArchiveBombError(
                        msg,
                        details={
                            "nested_file": info.filename,
                            "max_nesting": max_nesting,
                        },
                    )

            # Filter symlinks and collect warnings
            all_entries = [i for i in zf.infolist() if not i.is_dir()]
            infos: list[zipfile.ZipInfo] = []
            for entry in all_entries:
                if _is_zip_symlink(entry):
                    warnings.append(
                        SecurityWarning(
                            violation_type="symlink",
                            message=f"Symlink entry skipped: {entry.filename}",
                            raw_filename=entry.filename,
                        )
                    )
                    continue
                infos.append(entry)

            if len(infos) > max_files:
                msg = (
                    f"Archive contains {len(infos)} files, maximum allowed: {max_files}"
                )
                raise ArchiveBombError(
                    msg,
                    details={
                        "file_count": len(infos),
                        "max_files": max_files,
                    },
                )

            # Check total uncompressed size
            total_uncompressed = sum(i.file_size for i in infos)
            if total_uncompressed > max_bytes:
                msg = (
                    f"Archive uncompressed size ({total_uncompressed} bytes) "
                    f"exceeds limit ({max_bytes} bytes)"
                )
                raise ArchiveBombError(
                    msg,
                    details={
                        "uncompressed_bytes": total_uncompressed,
                        "max_bytes": max_bytes,
                    },
                )

            files: list[FileContent] = []
            total_size = 0

            for info in infos:
                safe_name = _sanitize_filename(info.filename)
                if safe_name is None:
                    warnings.append(
                        SecurityWarning(
                            violation_type="path_traversal",
                            message=(f"Entry resolves to empty path: {info.filename}"),
                            raw_filename=info.filename,
                        )
                    )
                    continue

                if safe_name != info.filename:
                    warnings.append(
                        SecurityWarning(
                            violation_type="path_traversal",
                            message=(
                                f"Path traversal sanitized: "
                                f"{info.filename} -> {safe_name}"
                            ),
                            raw_filename=info.filename,
                            filename=safe_name,
                        )
                    )

                if not _is_text_file(safe_name):
                    logger.debug(
                        "skipping_non_text_file",
                        filename=safe_name,
                    )
                    continue

                raw = zf.read(info.filename)
                total_size += len(raw)

                if total_size > max_bytes:
                    msg = (
                        f"Cumulative extracted size ({total_size} bytes) "
                        f"exceeds limit ({max_bytes} bytes)"
                    )
                    raise ArchiveBombError(
                        msg,
                        details={
                            "cumulative_bytes": total_size,
                            "max_bytes": max_bytes,
                        },
                    )

                text = _decode_bytes(raw)

                files.append(
                    FileContent(
                        filename=safe_name,
                        content=text,
                        size=len(raw),
                    )
                )

            return SubmissionContent(
                files=files,
                total_size=total_size,
                security_warnings=warnings,
            )

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
            raise ArchiveBombError(
                msg,
                details={
                    "decompressed_bytes": len(raw),
                    "max_bytes": max_bytes,
                },
            )

        # Determine inner filename (strip .gz)
        inner_name = file_path.stem
        text = _decode_bytes(raw)

        fc = FileContent(filename=inner_name, content=text, size=len(raw))
        return SubmissionContent(files=[fc], total_size=fc.size)

    except gzip.BadGzipFile as exc:
        msg = f"Invalid gzip file: {exc}"
        raise ArchiveExtractionError(msg) from exc
