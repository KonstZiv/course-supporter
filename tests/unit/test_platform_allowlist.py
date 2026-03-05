"""Tests for platform allowlist (S3-006)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from course_supporter.api.upload_validation import (
    PlatformRegistryConfig,
    check_platform,
    load_platform_registry,
)


class TestLoadPlatformRegistry:
    def test_loads_real_config(self) -> None:
        config = load_platform_registry(Path("config/platforms.yaml"))
        assert "video" in config.platforms
        assert "web" in config.platforms
        assert "youtube.com" in config.platforms["video"].verified

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_platform_registry(tmp_path / "missing.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(": : :")
        with pytest.raises(ValueError, match="Failed to parse"):
            load_platform_registry(bad)


class TestCheckPlatform:
    """check_platform returns warning for unverified, None for verified."""

    @pytest.fixture(autouse=True)
    def _patch_registry(self) -> None:  # type: ignore[return]
        registry = PlatformRegistryConfig.model_validate(
            {
                "platforms": {
                    "video": {"verified": ["youtube.com", "youtu.be"]},
                    "web": {"verified": ["github.com", "wikipedia.org"]},
                }
            }
        )
        with patch(
            "course_supporter.api.upload_validation.get_platform_registry",
            return_value=registry,
        ):
            yield

    def test_verified_youtube(self) -> None:
        assert check_platform("video", "https://www.youtube.com/watch?v=abc") is None

    def test_verified_youtu_be(self) -> None:
        assert check_platform("video", "https://youtu.be/abc") is None

    def test_verified_subdomain(self) -> None:
        assert check_platform("web", "https://en.wikipedia.org/wiki/Test") is None

    def test_unverified_video(self) -> None:
        result = check_platform("video", "https://dailymotion.com/video/123")
        assert result is not None
        assert "not in the verified list" in result
        assert "dailymotion.com" in result

    def test_unverified_web(self) -> None:
        result = check_platform("web", "https://medium.com/article")
        assert result is not None
        assert "medium.com" in result

    def test_no_allowlist_for_type(self) -> None:
        assert check_platform("presentation", "https://example.com/f.pdf") is None

    def test_no_allowlist_for_text(self) -> None:
        assert check_platform("text", "https://example.com/doc.md") is None

    def test_verified_github(self) -> None:
        assert check_platform("web", "https://github.com/user/repo") is None
