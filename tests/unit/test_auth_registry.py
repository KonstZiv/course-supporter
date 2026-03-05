"""Tests for auth scope registry (S3-005)."""

from __future__ import annotations

from pathlib import Path

import pytest

from course_supporter.auth.registry import (
    AuthRegistryConfig,
    AuthScope,
    load_auth_registry,
)


class TestAuthScope:
    """AuthScope StrEnum tests."""

    def test_values(self) -> None:
        assert AuthScope.PREP == "prep"
        assert AuthScope.CHECK == "check"

    def test_usable_as_string(self) -> None:
        assert AuthScope.PREP in {"prep", "check"}
        assert f"scope:{AuthScope.CHECK}" == "scope:check"


class TestLoadAuthRegistry:
    """load_auth_registry tests."""

    def test_loads_real_config(self) -> None:
        """Loads config/auth.yaml successfully."""
        config = load_auth_registry(Path("config/auth.yaml"))
        assert "prep" in config.scopes
        assert "check" in config.scopes
        assert config.scopes["prep"].description
        assert config.scopes["check"].rate_limit_field == "rate_limit_check"

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_auth_registry(tmp_path / "missing.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(": : :")
        with pytest.raises(ValueError, match="Failed to parse"):
            load_auth_registry(bad)

    def test_validation_error(self, tmp_path: Path) -> None:
        from pydantic import ValidationError

        bad = tmp_path / "bad.yaml"
        bad.write_text("scopes:\n  prep:\n    wrong_field: x\n")
        with pytest.raises(ValidationError):
            load_auth_registry(bad)


class TestAuthRegistryConfig:
    """Pydantic model validation."""

    def test_valid(self) -> None:
        config = AuthRegistryConfig.model_validate(
            {
                "scopes": {
                    "prep": {
                        "description": "Prep ops",
                        "rate_limit_field": "rate_limit_prep",
                    },
                }
            }
        )
        assert config.scopes["prep"].description == "Prep ops"
