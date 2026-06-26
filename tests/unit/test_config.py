"""Tests for application configuration."""

from datetime import time

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
        s = Settings(
            environment="production",  # type: ignore[arg-type]
            portal_session_secret="prod-secret-override",  # type: ignore[arg-type]
            _env_file=None,
        )
        assert s.is_prod is True
        assert s.is_dev is False

    def test_portal_secret_default_rejected_in_prod(self) -> None:
        """Production + the dev portal-session secret default → fail fast."""
        with pytest.raises(ValidationError, match="portal_session_secret"):
            Settings(environment="production", _env_file=None)  # type: ignore[arg-type]

    def test_portal_secret_override_ok_in_prod(self) -> None:
        """Production + an overridden portal-session secret loads fine."""
        s = Settings(
            environment="production",  # type: ignore[arg-type]
            portal_session_secret="a-strong-prod-secret",  # type: ignore[arg-type]
            _env_file=None,
        )
        assert s.is_prod is True

    def test_portal_secret_default_ok_in_dev(self) -> None:
        """Development tolerates the dev default (no override required)."""
        s = Settings(environment="development", _env_file=None)  # type: ignore[arg-type]
        assert s.is_dev is True

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


class TestWorkerSettings:
    """Test worker-related settings fields."""

    def test_worker_defaults(self) -> None:
        s = Settings(_env_file=None)
        assert s.worker_max_jobs == 1
        assert s.worker_job_timeout == 21600
        assert s.worker_max_tries == 3

    def test_worker_window_defaults(self) -> None:
        s = Settings(_env_file=None)
        assert s.worker_heavy_window_start == time(2, 0)
        assert s.worker_heavy_window_end == time(6, 30)
        assert s.worker_heavy_window_enabled is False
        assert s.worker_heavy_window_tz == "UTC"
        assert s.worker_immediate_override is True

    def test_worker_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORKER_MAX_JOBS", "5")
        monkeypatch.setenv("WORKER_JOB_TIMEOUT", "600")
        monkeypatch.setenv("WORKER_MAX_TRIES", "1")
        monkeypatch.setenv("WORKER_HEAVY_WINDOW_START", "01:00")
        monkeypatch.setenv("WORKER_HEAVY_WINDOW_END", "05:00")
        monkeypatch.setenv("WORKER_HEAVY_WINDOW_ENABLED", "true")
        monkeypatch.setenv("WORKER_HEAVY_WINDOW_TZ", "Europe/Kyiv")
        monkeypatch.setenv("WORKER_IMMEDIATE_OVERRIDE", "false")
        s = Settings(_env_file=None)
        assert s.worker_max_jobs == 5
        assert s.worker_job_timeout == 600
        assert s.worker_max_tries == 1
        assert s.worker_heavy_window_start == time(1, 0)
        assert s.worker_heavy_window_end == time(5, 0)
        assert s.worker_heavy_window_enabled is True
        assert s.worker_heavy_window_tz == "Europe/Kyiv"
        assert s.worker_immediate_override is False

    def test_invalid_timezone_rejected(self) -> None:
        with pytest.raises(ValidationError, match="Invalid timezone"):
            Settings(
                worker_heavy_window_tz="Not/A/Timezone",
                _env_file=None,
            )

    def test_redis_url_default(self) -> None:
        s = Settings(_env_file=None)
        assert s.redis_url == "redis://localhost:6379/0"

    def test_redis_url_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("REDIS_URL", "redis://redis:6379/1")
        s = Settings(_env_file=None)
        assert s.redis_url == "redis://redis:6379/1"


class TestIntakeSettings:
    """Intake admission / queue-lifetime knobs (DD-3.3c-I)."""

    def test_intake_defaults(self) -> None:
        """All five intake knobs default to the 2-vCPU profile values."""
        s = Settings(_env_file=None)
        assert s.intake_admission_max_pending_video_hours == 60.0
        assert s.intake_drain_coefficient == 1.4
        assert s.intake_job_expires_hours == 96.0
        assert s.intake_hint_max_hours == 84.0
        assert s.intake_hint_check_after_hours == 12.0

    def test_intake_knobs_are_float(self) -> None:
        """Intake knobs are floats so capacity tuning needs no code change."""
        s = Settings(_env_file=None)
        assert isinstance(s.intake_admission_max_pending_video_hours, float)
        assert isinstance(s.intake_drain_coefficient, float)
        assert isinstance(s.intake_job_expires_hours, float)
        assert isinstance(s.intake_hint_max_hours, float)
        assert isinstance(s.intake_hint_check_after_hours, float)

    def test_expires_extra_ms_conversion(self) -> None:
        """Default expiry converts to the ARQ expires_extra_ms (ms) we wire."""
        s = Settings(_env_file=None)
        assert s.intake_job_expires_ms == 345_600_000
        assert s.intake_job_expires_ms == int(s.intake_job_expires_hours * 3_600_000)

    def test_invariant_passes_with_defaults(self) -> None:
        """Defaults satisfy expires (96) >= admission (60) x drain (1.4) = 84."""
        s = Settings(_env_file=None)
        worst_case = (
            s.intake_admission_max_pending_video_hours * s.intake_drain_coefficient
        )
        assert s.intake_job_expires_hours >= worst_case

    def test_invariant_violation_raises_at_load(self) -> None:
        """expires below admission x drain fails fast at settings load."""
        with pytest.raises(ValidationError, match="intake_job_expires_hours"):
            Settings(
                intake_admission_max_pending_video_hours=60.0,
                intake_drain_coefficient=1.4,
                intake_job_expires_hours=10.0,  # 10 < 60 * 1.4 = 84
                _env_file=None,
            )

    def test_invariant_error_names_both_numbers(self) -> None:
        """The fail-fast message names both knobs so the operator knows them."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(
                intake_admission_max_pending_video_hours=100.0,
                intake_drain_coefficient=1.4,
                intake_job_expires_hours=96.0,  # 96 < 100 * 1.4 = 140
                _env_file=None,
            )
        message = str(exc_info.value)
        assert "intake_job_expires_hours" in message
        assert "intake_admission_max_pending_video_hours" in message
        assert "intake_drain_coefficient" in message

    def test_invariant_boundary_equal_passes(self) -> None:
        """Exactly meeting the bound (expires == admission x drain) is valid."""
        s = Settings(
            intake_admission_max_pending_video_hours=60.0,
            intake_drain_coefficient=1.4,
            intake_job_expires_hours=84.0,  # 84 == 60 * 1.4
            _env_file=None,
        )
        assert s.intake_job_expires_hours == 84.0
