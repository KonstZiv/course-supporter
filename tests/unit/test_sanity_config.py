"""Tests for the sanity gate config loader (sprint-mentor T7).

Locks the load-time invariant (confidence_threshold strictly in (0, 1]) and that
the shipped ``config/sanity.yaml`` actually loads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from course_supporter.homework.sanity_config import (
    SanityConfig,
    load_sanity_config,
)

_SHIPPED = Path("config/sanity.yaml")


class TestShippedConfig:
    def test_shipped_config_loads(self) -> None:
        cfg = load_sanity_config(_SHIPPED)
        assert isinstance(cfg, SanityConfig)
        # Conservative gate (ratified A3): high floor, only obvious garbage.
        assert 0.0 < cfg.confidence_threshold <= 1.0
        assert cfg.confidence_threshold >= 0.5

    def test_missing_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_sanity_config(Path("config/does_not_exist.yaml"))


class TestThresholdInvariant:
    @pytest.mark.parametrize("bad", [0.0, -0.1, 1.5])
    def test_threshold_outside_unit_interval_rejected(self, bad: float) -> None:
        with pytest.raises(ValueError):
            SanityConfig(confidence_threshold=bad)

    def test_valid_threshold_accepted(self) -> None:
        assert SanityConfig(confidence_threshold=0.9).confidence_threshold == 0.9

    def test_extra_keys_forbidden(self) -> None:
        with pytest.raises(ValueError):
            SanityConfig.model_validate({"confidence_threshold": 0.85, "unexpected": 1})
