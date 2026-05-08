"""Shared file upload and URL validation utilities.

Provides filename extension parsing helper for callers outside the
canonical KD14 pipeline (e.g. homework's own quick-fail check at
``api/routes/homework.py:134``) plus the URL platform allowlist
(separate concern from KD14 file-upload validation per
:mod:`course_supporter.security.policies` lines 9-13).

Phase 1.2 C3 (KD14 adoption in production): the legacy hand-rolled
``ALLOWED_EXTENSIONS`` source-type → frozenset whitelist was removed
from this module. Authored upload validation now invokes
:func:`course_supporter.security.run_stage1` against
:data:`course_supporter.security.AUTHORED_POLICY` directly at the
``api/routes/documents.py`` callsites (8-extension behavioral drift
documented in POST-MERGE-NOTES "Ratified whitelist drift" section).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel


def file_extension(filename: str | None) -> str:
    """Extract lowercase file extension from filename.

    Args:
        filename: Original filename or None.

    Returns:
        Extension with leading dot (e.g. ".pdf") or empty string.
    """
    if not filename or "." not in filename:
        return ""
    return "." + filename.rsplit(".", maxsplit=1)[-1].lower()


# ── Platform allowlist ──


class PlatformSourceConfig(BaseModel):
    """Verified domains for a single source_type."""

    verified: list[str] = []


class PlatformRegistryConfig(BaseModel):
    """Top-level platform config (config/platforms.yaml)."""

    platforms: dict[str, PlatformSourceConfig]


_registry: PlatformRegistryConfig | None = None


def load_platform_registry(config_path: Path) -> PlatformRegistryConfig:
    """Load and validate platform registry from YAML.

    Raises:
        FileNotFoundError: if YAML file doesn't exist.
        ValueError: if YAML parsing or validation fails.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Platform config not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse platform config '{config_path}': {e}") from e
    return PlatformRegistryConfig.model_validate(raw)


def get_platform_registry(
    config_path: Path = Path("config/platforms.yaml"),
) -> PlatformRegistryConfig:
    """Return cached platform registry (singleton)."""
    global _registry
    if _registry is None:
        _registry = load_platform_registry(config_path)
    return _registry


def check_platform(source_type: str, source_url: str) -> str | None:
    """Check if URL domain is in the verified allowlist.

    Args:
        source_type: Material type (video, web, etc.).
        source_url: URL to check.

    Returns:
        Warning message if platform is unverified, None if verified
        or source_type has no allowlist.
    """
    registry = get_platform_registry()
    source_config = registry.platforms.get(source_type)
    if source_config is None:
        return None

    parsed = urlparse(source_url)
    domain = parsed.hostname or ""

    for verified in source_config.verified:
        if domain == verified or domain.endswith(f".{verified}"):
            return None

    return (
        f"Platform '{domain}' is not in the verified list for "
        f"'{source_type}'. Processing may fail or produce poor results."
    )
