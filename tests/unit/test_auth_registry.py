"""Tests for auth scope/plan registry."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from course_supporter.auth.registry import (
    AuthRegistryConfig,
    AuthScope,
    ScopeConfig,
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
        """config/auth.yaml parses and passes validation."""
        config = load_auth_registry(Path("config/auth.yaml"))

        assert "prep" in config.scopes
        assert "check" in config.scopes
        assert config.scopes["prep"].description
        assert config.default_plan in config.plans
        basic = config.plans[config.default_plan]
        assert basic["prep"] > 0
        assert basic["check"] > 0

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_auth_registry(tmp_path / "missing.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(": : :")
        with pytest.raises(ValueError, match="Failed to parse"):
            load_auth_registry(bad)


class TestAuthRegistryValidation:
    """Invariants enforced at model construction time."""

    def _make(
        self,
        *,
        scopes: dict[str, ScopeConfig] | None = None,
        default_plan: str = "basic",
        plans: dict[str, dict[str, int]] | None = None,
    ) -> AuthRegistryConfig:
        return AuthRegistryConfig(
            scopes=scopes
            or {
                "prep": ScopeConfig(description="prep"),
                "check": ScopeConfig(description="check"),
            },
            default_plan=default_plan,
            plans=plans or {"basic": {"prep": 60, "check": 300}},
        )

    def test_valid(self) -> None:
        config = self._make()
        assert config.plans["basic"]["prep"] == 60

    def test_default_plan_must_exist(self) -> None:
        with pytest.raises(ValidationError, match="default_plan 'missing'"):
            self._make(default_plan="missing")

    def test_plan_must_cover_every_scope(self) -> None:
        with pytest.raises(ValidationError, match="missing rate limits"):
            self._make(plans={"basic": {"prep": 60}})  # check missing

    def test_plan_cannot_define_unknown_scope(self) -> None:
        with pytest.raises(ValidationError, match="unknown scopes"):
            self._make(
                plans={"basic": {"prep": 60, "check": 300, "phantom": 10}},
            )

    def test_limit_must_be_positive(self) -> None:
        with pytest.raises(ValidationError, match="non-positive limit"):
            self._make(plans={"basic": {"prep": 0, "check": 300}})


class TestLimitFor:
    """Runtime lookup helper."""

    def test_known_plan_and_scope(self) -> None:
        config = AuthRegistryConfig(
            scopes={
                "prep": ScopeConfig(description="prep"),
                "check": ScopeConfig(description="check"),
            },
            default_plan="basic",
            plans={
                "basic": {"prep": 60, "check": 300},
                "pro": {"prep": 600, "check": 3000},
            },
        )
        assert config.limit_for("pro", "prep") == 600

    def test_unknown_plan_falls_back_to_default(self) -> None:
        """Stale plan_id in DB falls back to default_plan — no runtime error."""
        config = AuthRegistryConfig(
            scopes={
                "prep": ScopeConfig(description="prep"),
                "check": ScopeConfig(description="check"),
            },
            default_plan="basic",
            plans={"basic": {"prep": 60, "check": 300}},
        )
        assert config.limit_for("removed_plan", "prep") == 60
