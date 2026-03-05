"""Auth scope registry — loaded from config/auth.yaml."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel


class AuthScope(StrEnum):
    """Known API scopes.

    Values match keys in config/auth.yaml.
    StrEnum allows direct use as string in scope checks.
    """

    PREP = "prep"
    CHECK = "check"


class ScopeConfig(BaseModel):
    """Metadata for a single scope."""

    description: str
    rate_limit_field: str


class AuthRegistryConfig(BaseModel):
    """Top-level auth config (config/auth.yaml)."""

    scopes: dict[str, ScopeConfig]


def load_auth_registry(config_path: Path) -> AuthRegistryConfig:
    """Load and validate auth registry from YAML.

    Raises:
        FileNotFoundError: if YAML file doesn't exist.
        ValueError: if YAML parsing or validation fails.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Auth config not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse auth config '{config_path}': {e}") from e
    return AuthRegistryConfig.model_validate(raw)
