"""Auth scope registry — loaded from config/auth.yaml.

The registry defines two policy objects:

* ``scopes`` — authorization markers. A scope is a name + description;
  an APIKey is permitted to perform operations whose endpoint requires
  one of the key's scopes.
* ``plans`` — named bundles of per-scope rate limits. Each APIKey points
  to a plan via ``api_keys.plan_id``. At request time the matched
  scope's limit is resolved from the key's plan.

Rate limits are intentionally NOT stored in the database: they are
policy, not per-key data, and belong in version-controlled config.
Per-key overrides can be layered on later if the need arises.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, model_validator


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


class AuthRegistryConfig(BaseModel):
    """Top-level auth config (config/auth.yaml)."""

    scopes: dict[str, ScopeConfig]
    default_plan: str
    plans: dict[str, dict[str, int]]

    @model_validator(mode="after")
    def _validate_plans(self) -> AuthRegistryConfig:
        """Enforce invariants: default plan exists, plans cover all scopes.

        Fails fast at startup if YAML is inconsistent, so drift is
        impossible to deploy.
        """
        if self.default_plan not in self.plans:
            raise ValueError(
                f"default_plan '{self.default_plan}' not found in plans: "
                f"{sorted(self.plans)}"
            )

        scope_names = set(self.scopes)
        for plan_name, limits in self.plans.items():
            plan_scopes = set(limits)
            missing = scope_names - plan_scopes
            extra = plan_scopes - scope_names
            if missing:
                raise ValueError(
                    f"Plan '{plan_name}' is missing rate limits for scopes: "
                    f"{sorted(missing)}"
                )
            if extra:
                raise ValueError(
                    f"Plan '{plan_name}' defines limits for unknown scopes: "
                    f"{sorted(extra)}"
                )
            for scope_name, limit in limits.items():
                if limit <= 0:
                    raise ValueError(
                        f"Plan '{plan_name}' has non-positive limit for "
                        f"scope '{scope_name}': {limit}"
                    )
        return self

    def limit_for(self, plan_id: str, scope: str) -> int:
        """Resolve rate limit for a (plan_id, scope) pair.

        Unknown plan_id falls back to default_plan — keeps runtime
        resilient to stale DB rows after a plan rename.
        """
        plan = self.plans.get(plan_id) or self.plans[self.default_plan]
        return plan[scope]


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


_DEFAULT_AUTH_CONFIG_PATH = Path("config/auth.yaml")


@lru_cache(maxsize=1)
def get_auth_registry() -> AuthRegistryConfig:
    """Return the process-wide singleton registry.

    Parses YAML once per process. Call ``get_auth_registry.cache_clear()``
    in tests that need to reload with a different config.
    """
    return load_auth_registry(_DEFAULT_AUTH_CONFIG_PATH)
