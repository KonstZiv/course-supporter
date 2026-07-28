"""LLM ladder configuration loader (KD16).

Loads ``ladders_*.yaml`` files from a directory at startup,
validates each against the Pydantic schema, and merges them into a
single mapping keyed by stage name. Cross-file stage-name
uniqueness is enforced.

Sister module to :mod:`course_supporter.llm.registry`; this is the
KD16 replacement for the legacy ``actions`` / ``strategies`` model.
That legacy model remains in place but unused on any production path as of
2026-07-19; its removal is undated, scope tracked as DD-20-A.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from course_supporter.llm.registry import Capability, ModelRegistryConfig

# Strict input-config models: typo in a YAML key fails validation
# instead of silently shadowing a real field.
_FORBID = ConfigDict(extra="forbid")


class LadderEntry(BaseModel):
    """One step in a stage's fallback ladder.

    The router walks ladder entries in order; ``provider`` must match
    a registered provider name from
    :mod:`course_supporter.llm.providers`.

    Optional per-rung overrides:

    * ``reasoning`` — vendor-specific reasoning-mode kwargs propagated
      to :class:`LLMRequest.reasoning` and through to the provider.
      Example: ``{"exclude": True}`` for Qwen3-VL via DashScope so the
      non-reasoning VL model does not accidentally receive a reasoning
      directive that would silently degrade output structure.
    * ``max_output_tokens`` — per-rung floor for the provider's
      ``max_tokens`` kwarg. ``None`` preserves the provider's class-level
      default (e.g. ``DashScopeProvider.default_max_output_tokens``).
    """

    model_config = _FORBID

    provider: str
    model: str
    reasoning: dict[str, Any] | None = None
    max_output_tokens: int | None = None


class StageConfig(BaseModel):
    """Per-stage routing configuration.

    Attributes:
        prompt_ref: Markdown prompt path resolved relative to the
            backend repo CWD (e.g. ``"prompts/example/v1.md"``). The
            actual file read happens in
            :mod:`course_supporter.llm.prompt_loader_md`.
        requires: Capabilities every ladder rung's model must declare
            (TASK-2.4.23). Empty default keeps existing stages without
            constraint; non-empty values are checked at startup by
            :func:`validate_ladders_against_registry`. Mirrors
            ``ActionConfig.requires`` in the registry.
        ladder: Ordered fallback ladder; at least one entry.
        input_budget_ratio: Optional opt-in input-budget check
            (Phase 3.2.3-pre, KD10 «Token budget policy»). When set
            (float in ``(0.0, 1.0]``), the router estimates input size
            via :func:`course_supporter.llm.token_budget.estimate_tokens`
            and skips a rung if the estimate exceeds
            ``ratio * max_context`` of that rung's model from the
            registry. ``None`` (default) preserves byte-identical
            existing behavior — no estimation, no skip. The router
            does NOT translate "no rung passes" into a domain-level
            message ("restructure your course"); that translation
            lives at the callsite (Phase 3.2.3a methodist agent).
            Numeric policy value (e.g. 0.5 from KD10) is owned by the
            caller's stage YAML, NOT hard-coded in the router.
    """

    model_config = _FORBID

    prompt_ref: str
    requires: list[Capability] = Field(default_factory=list)
    ladder: list[LadderEntry] = Field(min_length=1)
    input_budget_ratio: float | None = Field(default=None, gt=0.0, le=1.0)


class LadderFile(BaseModel):
    """Per-file YAML container.

    Each ``ladders_*.yaml`` deserialises into a ``LadderFile``;
    multiple files are merged into a single :class:`LadderConfig`
    by :func:`load_ladder_config`.
    """

    model_config = _FORBID

    stages: dict[str, StageConfig] = Field(default_factory=dict)


class LadderConfig(BaseModel):
    """Merged view of all loaded ``ladders_*.yaml`` files.

    Stage names are globally unique across files; the loader raises
    ``ValueError`` if two files declare the same stage name.
    """

    stages: dict[str, StageConfig] = Field(default_factory=dict)

    def get_stage(self, name: str) -> StageConfig:
        """Look up a stage by name.

        Raises:
            KeyError: if the stage is not registered.
        """
        if name not in self.stages:
            raise KeyError(f"Unknown stage: '{name}'")
        return self.stages[name]


def load_ladder_config(directory: Path) -> LadderConfig:
    """Load and merge ``ladders_*.yaml`` files from ``directory``.

    Args:
        directory: Directory containing ``ladders_*.yaml`` files.
            Files are discovered via glob and processed in sorted
            order for deterministic merge.

    Returns:
        Merged :class:`LadderConfig`. If no ``ladders_*.yaml`` files
        match the glob, the returned config has an empty ``stages``
        mapping (any ``get_stage`` call raises ``KeyError``).

    Raises:
        FileNotFoundError: if ``directory`` does not exist.
        ValueError: if YAML parsing fails, schema validation fails,
            or two files declare the same stage name.
    """
    if not directory.exists():
        raise FileNotFoundError(f"Ladder config directory not found: {directory}")

    merged: dict[str, StageConfig] = {}
    origins: dict[str, Path] = {}

    for path in sorted(directory.glob("ladders_*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Failed to parse ladder config '{path}': {exc}") from exc

        try:
            ladder_file = LadderFile.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"Invalid ladder config '{path}': {exc}") from exc

        for stage_name, stage_cfg in ladder_file.stages.items():
            if stage_name in origins:
                raise ValueError(
                    f"Duplicate stage name '{stage_name}' "
                    f"declared in '{origins[stage_name]}' and '{path}'"
                )
            merged[stage_name] = stage_cfg
            origins[stage_name] = path

    return LadderConfig(stages=merged)


def validate_ladders_against_registry(
    cfg: LadderConfig, registry: ModelRegistryConfig
) -> None:
    """Cross-check every ladder rung against the model registry (TASK-2.4.23).

    Four invariants enforced fail-fast at startup:

    * **K — membership:** every ``rung.model`` must exist in
      ``registry.models``. A typo / rename / drift between
      ``ladders_*.yaml`` and ``external_services.yaml`` would otherwise
      surface only on the first runtime hit through the affected rung.
    * **Q-axis1 — capability:** every capability declared in
      ``stage.requires`` must appear in ``model.capabilities`` for each
      rung. Catches text-only models accidentally placed in a
      vision-required stage (and similar mismatches).
    * **Input-budget (Phase 3.2.3-pre):** if a stage declares
      ``input_budget_ratio``, every rung's model in the registry MUST
      have a non-None ``max_context``. Otherwise the runtime
      ratio-vs-context check at the StageRouter loop body would
      degrade to "always skip" or "always pass" silently — depending
      on guard wiring. Fail-fast at config-time so a missing
      registry entry surfaces as a startup error, not as a misrouted
      production hit.
    * **Reasoning translatability (P6):** every rung carrying a
      non-``None`` ``reasoning`` form is checked against its provider's
      connector via ``LLMProvider.supports_reasoning`` — the capability
      declaration owned by the provider module (no dialect knowledge is
      copied here). A form the connector cannot translate would be
      SILENTLY IGNORED on the wire (STEP-0 probe); refusing it at boot
      turns a deploy-time typo into a startup error rather than a
      pay-for-reasoning-we-can't-see production hit.

    Errors are aggregated and raised as a single ``ValueError`` so a
    multi-typo config surfaces every problem at once. Mirrors the
    pattern in :meth:`ModelRegistryConfig.validate_and_flatten` for
    action chains.

    Raises:
        ValueError: if any rung references an unknown model, lacks a
            capability declared in ``stage.requires``, (when the stage
            declares ``input_budget_ratio``) lacks ``max_context`` in the
            registry, OR carries a ``reasoning`` form its provider's
            connector cannot translate.
    """
    errors: list[str] = []

    # Deferred import keeps ``ladder_config`` free of the provider SDK graph at
    # module load and avoids an import cycle; by validation time (app / worker
    # startup) the providers are already imported. ``PROVIDER_REGISTRY`` maps a
    # provider name → its class, so the reasoning check stays provider-agnostic
    # and the dialect knowledge lives only in the provider modules (P6).
    from course_supporter.llm.providers import PROVIDER_REGISTRY

    for stage_name, stage in cfg.stages.items():
        for i, entry in enumerate(stage.ladder):
            # P6 — reasoning-form translatability. A rung whose ``reasoning``
            # form its provider's connector cannot translate must fail the boot
            # rather than no-op silently on the wire. Runs independently of the
            # membership check below so a rung with two faults reports both.
            if entry.reasoning is not None:
                provider_cls = PROVIDER_REGISTRY.get(entry.provider)
                if provider_cls is None or not provider_cls.supports_reasoning(
                    entry.reasoning
                ):
                    errors.append(
                        f"Stage '{stage_name}' rung {i} provider "
                        f"'{entry.provider}' model '{entry.model}' declares a "
                        f"reasoning form its connector cannot translate: "
                        f"{entry.reasoning!r}"
                    )

            if entry.model not in registry.models:
                errors.append(
                    f"Stage '{stage_name}' rung {i} "
                    f"references unknown model: '{entry.model}'"
                )
                continue

            model = registry.models[entry.model]
            missing = set(stage.requires) - set(model.capabilities)
            if missing:
                errors.append(
                    f"Stage '{stage_name}' rung {i} model '{entry.model}' "
                    f"lacks required capabilities: {sorted(missing)}"
                )

            if stage.input_budget_ratio is not None and model.max_context is None:
                errors.append(
                    f"Stage '{stage_name}' rung {i} model '{entry.model}' "
                    f"has no max_context in the registry "
                    f"(required by input_budget_ratio="
                    f"{stage.input_budget_ratio})"
                )

    if errors:
        raise ValueError(
            "Ladder validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )
