"""Tests for application configuration."""

import pytest
from pydantic import ValidationError

from course_supporter.config import Environment, Settings


class TestSettings:
    """Test Settings model validation and computed fields."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Settings loads with all defaults (no env vars needed)."""
        monkeypatch.delenv("ENVIRONMENT", raising=False)
        s = Settings(
            _env_file=None,
        )
        assert s.environment == Environment.DEVELOPMENT
        assert s.postgres_user == "course_supporter"
        assert s.is_dev is True
        assert s.is_prod is False

    def test_database_url_assembly(self) -> None:
        """Database URL is correctly assembled from components."""
        s = Settings(
            postgres_user="user",
            postgres_password="pass",  # type: ignore[arg-type]
            postgres_host="db.example.com",
            postgres_port=5433,
            postgres_db="mydb",
            _env_file=None,
        )
        assert s.database_url == (
            "postgresql+psycopg://user:pass@db.example.com:5433/mydb"
        )

    def test_secret_str_not_exposed(self) -> None:
        """API keys are not exposed in repr or string conversion."""
        s = Settings(
            gemini_api_key="super-secret-key",  # type: ignore[arg-type]
            _env_file=None,
        )
        repr_str = repr(s)
        assert "super-secret-key" not in repr_str
        assert s.gemini_api_key is not None
        assert s.gemini_api_key.get_secret_value() == "super-secret-key"

    def test_api_keys_optional(self) -> None:
        """All API keys are optional by default."""
        s = Settings(_env_file=None)
        assert s.gemini_api_key is None
        assert s.anthropic_api_key is None
        assert s.openai_api_key is None
        assert s.deepseek_api_key is None

    def test_environment_enum(self) -> None:
        """Environment accepts valid values."""
        s = Settings(environment="production", _env_file=None)  # type: ignore[arg-type]
        assert s.is_prod is True
        assert s.is_dev is False

    def test_invalid_environment(self) -> None:
        """Invalid environment value raises ValidationError."""
        with pytest.raises(ValidationError):
            Settings(environment="invalid", _env_file=None)  # type: ignore[arg-type]

    def test_invalid_port(self) -> None:
        """Non-integer port raises ValidationError."""
        with pytest.raises(ValidationError):
            Settings(postgres_port="not-a-number", _env_file=None)  # type: ignore[arg-type]

    def test_deepseek_base_url_default(self) -> None:
        """DeepSeek base URL has correct default."""
        s = Settings(_env_file=None)
        assert s.deepseek_base_url == "https://api.deepseek.com"

    def test_testing_environment(self) -> None:
        """Testing environment flag works."""
        s = Settings(environment="testing", _env_file=None)  # type: ignore[arg-type]
        assert s.is_testing is True
        assert s.is_dev is False
