"""Unified external service registry: providers, models, actions, routing.

Loaded from config/external_services.yaml at startup, validated by Pydantic.
Extensible: new action/model/strategy = YAML edit, no code changes.

STTRouter uses get_chain() to obtain an ordered list of ModelConfig
objects with .provider, .model_id, .estimate_cost(); StageRouter reads the
flattened ``models`` map for per-rung cost + max_tokens.

The catalog is versioned (BIL-C): its ``catalog`` block names a version and the
fingerprint of the price data that version stands for, and :func:`load_registry`
refuses a file whose price data no longer matches the fingerprint — a price
cannot change without a new version. Every register row stores the version and
the unit prices it was priced at (``llm/pricing.py``). Time-of-day tariffs are
data (``tariffs``), not code.
"""

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, Field, PositiveInt, field_validator, model_validator

from course_supporter.cost_kinds import TariffWindow


class Capability(StrEnum):
    """Capabilities a model can have.

    ``JSON_MODE`` and ``SCHEMA_STRICT`` replace the former
    ``structured_output`` label (task 09a) with two facts the router acts on:

    * ``JSON_MODE`` — the model's connector can switch the provider's JSON
      mode on, without a schema. Stages whose output must be JSON require it.
    * ``SCHEMA_STRICT`` — the provider holds a strict JSON Schema on the wire.
      Granted only to models measured to accept AND follow one; the router
      sends a stage's schema to these and plain JSON mode to the rest.
    """

    VISION = "vision"
    JSON_MODE = "json_mode"
    SCHEMA_STRICT = "schema_strict"
    LONG_CONTEXT = "long_context"


class ProviderModelConfig(BaseModel):
    """Single model within a provider definition.

    ``cost_per_1k_in`` / ``cost_per_1k_out`` default to ``None`` = "price not
    named", deliberately not ``0.0``: an explicit zero is a real price (free
    tier, local model), and a default of zero made a forgotten price read as
    one. The startup ladder check refuses a rung whose model has no named
    price (``ladder_config.validate_ladders_against_registry``).

    The BIL-C fields are optional, and empty means "the rule does not apply":

    * ``cost_per_1k_in_cached`` — the price of input tokens the provider served
      from its cache. Empty: cached tokens are priced at the cache-miss rate and
      the row is ``estimated``.
    * ``price_effective_from`` — the listed prices are in force from this date
      (an introductory discount runs until then). Before it, rows are
      ``estimated``.
    * ``price_holds_to_input_tokens`` — the listed prices hold for prompts up to
      this many input tokens (a higher tier above). Above it, rows are
      ``estimated``.
    * ``billing_granularity_sec`` — speech-to-text: the provider bills audio in
      whole multiples of this many seconds. Empty: the sum is ``estimated``.
    * ``tariff`` — the name of a time-of-day tariff in ``tariffs``; the prices
      above are then the tariff's base (off-peak) prices.
    """

    id: str
    capabilities: list[Capability] = []
    max_context: int | None = None
    max_output_tokens: int | None = None
    unit_type: str = "tokens"
    cost_per_1k_in: float | None = None
    cost_per_1k_out: float | None = None
    cost_per_1k_in_cached: float | None = None
    cost_per_minute: float | None = None
    price_effective_from: date | None = None
    price_holds_to_input_tokens: PositiveInt | None = None
    billing_granularity_sec: PositiveInt | None = None
    tariff: str | None = None
    local: bool = False


class ProviderConfig(BaseModel):
    """Provider with its models."""

    type: str  # llm, transcription, scraping
    models: list[ProviderModelConfig]


class StrategyConfig(BaseModel):
    """Named strategy with provider preference."""

    providers: list[str]
    fallback: bool = True


class ActionConfig(BaseModel):
    """Action with strategy reference and model chains."""

    strategy: str = "default"
    requires: list[Capability] = []
    chain: dict[str, list[str]] = {}


# ── Time-of-day tariffs (BIL-C, decision 3) ──


class Weekday(StrEnum):
    """A day of the week, as the catalog spells it."""

    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


_WEEKDAYS = list(Weekday)  # index = datetime.weekday()
_MINUTES_PER_DAY = 24 * 60


def _minutes(clock: str) -> int:
    hours, _, minutes = clock.partition(":")
    return int(hours) * 60 + int(minutes)


class PeakHours(BaseModel):
    """When a tariff's peak runs: these weekdays, these hour spans.

    Each span is ``"HH:MM-HH:MM"``, half-open: the start minute is peak, the end
    minute is not (``"01:00-04:00"`` covers 01:00:00 up to 03:59:59).
    """

    weekdays: list[Weekday]
    hours: list[str]

    @field_validator("hours")
    @classmethod
    def _spans_are_well_formed(cls, hours: list[str]) -> list[str]:
        for span in hours:
            start, sep, end = span.partition("-")
            try:
                start_min, end_min = _minutes(start), _minutes(end)
            except ValueError:
                start_min = end_min = -1
            if not sep or not 0 <= start_min < end_min <= _MINUTES_PER_DAY:
                raise ValueError(f"peak hour span must be 'HH:MM-HH:MM': {span!r}")
        return hours

    def spans(self) -> list[tuple[int, int]]:
        """The spans as ``(start, end)`` minutes since midnight, end exclusive."""
        result = []
        for span in self.hours:
            start, _, end = span.partition("-")
            result.append((_minutes(start), _minutes(end)))
        return result


class TariffConfig(BaseModel):
    """A time-of-day tariff: a peak window and how much dearer the peak is.

    ``catalog_prices: off_peak`` says the prices of the models bound to this
    tariff are the off-peak prices; a peak call pays them times
    ``peak_multiplier``. It is the only base the catalog uses, and it is
    spelled out so that a reader of the YAML does not have to guess.
    """

    timezone: str = "UTC"
    catalog_prices: Literal["off_peak"]
    peak_multiplier: Decimal = Field(gt=0)
    peak: PeakHours

    @field_validator("timezone")
    @classmethod
    def _timezone_exists(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value

    def window_at(self, instant: datetime) -> TariffWindow:
        """Peak or off-peak at ``instant`` (timezone-aware)."""
        if instant.tzinfo is None:
            raise ValueError("a tariff window needs a timezone-aware instant")
        local = instant.astimezone(ZoneInfo(self.timezone))
        if _WEEKDAYS[local.weekday()] not in self.peak.weekdays:
            return TariffWindow.OFF_PEAK
        minute = local.hour * 60 + local.minute
        for start, end in self.peak.spans():
            if start <= minute < end:
                return TariffWindow.PEAK
        return TariffWindow.OFF_PEAK

    def multiplier(self, window: TariffWindow) -> Decimal:
        """What the catalog price is multiplied by in ``window``."""
        return self.peak_multiplier if window is TariffWindow.PEAK else Decimal(1)


class CatalogInfo(BaseModel):
    """The catalog's version and the fingerprint of the price data it names.

    ``pricing_sha256`` is :meth:`ModelRegistryConfig.pricing_fingerprint` of the
    file as it was when the version was given. :func:`load_registry` recomputes
    it and refuses a mismatch: a price changed without a new version would make
    the version on every register row a lie.
    """

    version: str = Field(min_length=1, max_length=32)
    pricing_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


# ── Flattened model config (per-model cost + limits lookup) ──


class CostPer1K(BaseModel):
    """Cost per 1000 tokens in USD."""

    input: float
    output: float


def _exact(value: float | None) -> Decimal | None:
    """The YAML number as the decimal it was written as, not its binary twin.

    ``repr`` of a float is the shortest string that reads back to it, which for
    a price typed in the catalog is the price as typed (``0.00022``).
    """
    return None if value is None else Decimal(repr(value))


class ModelPrices(BaseModel):
    """A model's catalog prices as exact decimals (BIL-C, decision 4).

    The register prices calls from these; :class:`CostPer1K` stays the float
    form the money ceilings estimate with.
    """

    in_per_1k: Decimal | None = None
    in_cached_per_1k: Decimal | None = None
    out_per_1k: Decimal | None = None
    per_minute: Decimal | None = None


class ModelConfig(BaseModel):
    """Flattened per-model configuration.

    Built from provider + model data during registry validation.
    Provides .model_id, .provider, .unit_type, and .estimate_cost();
    consumed by StageRouter (cost + limits) and STTRouter (chain).

    ``cost_per_1k`` is ``None`` unless BOTH token prices are named: half a
    price cannot cost a call, so it is treated as no price at all. ``prices``
    carries the same numbers as exact decimals, for the register; the pricing
    rules (``tariff`` and the three optional fields below it) are read by
    :mod:`course_supporter.llm.pricing`.
    """

    model_id: str = ""
    provider: str = ""
    capabilities: list[Capability] = []
    max_context: int | None = None
    max_output_tokens: int | None = None
    unit_type: str = "tokens"
    cost_per_1k: CostPer1K | None = None
    cost_per_minute: float | None = None
    prices: ModelPrices = ModelPrices()
    tariff: TariffConfig | None = None
    price_effective_from: date | None = None
    price_holds_to_input_tokens: int | None = None
    billing_granularity_sec: int | None = None
    local: bool = False

    def estimate_cost(self, tokens_in: int, tokens_out: int) -> float | None:
        """Calculate cost in USD for given token counts.

        Returns ``None`` when the model has no named token price — "cost
        unknown", which cost reporting excludes, never a fabricated zero.
        """
        if self.cost_per_1k is None:
            return None
        return (
            tokens_in * self.cost_per_1k.input / 1000
            + tokens_out * self.cost_per_1k.output / 1000
        )


# ── Top-level registry ──


class ModelRegistryConfig(BaseModel):
    """Unified external service registry.

    Parses the providers/strategies/actions YAML structure and
    flattens provider models into a dict[model_id, ModelConfig]
    for per-model cost + limit lookup.

    Validates that:
    - All models referenced in action chains exist
    - All models in chains have required capabilities
    - Every action chain has at least a 'default' entry
    """

    providers: dict[str, ProviderConfig]
    strategies: dict[str, StrategyConfig] = {}
    actions: dict[str, ActionConfig]
    # BIL-C. Optional on the model so a registry built in code (tests) needs
    # neither; :func:`load_registry` requires the block in a file and checks it.
    catalog: CatalogInfo | None = None
    tariffs: dict[str, TariffConfig] = {}

    # Flattened lookup (populated in validator)
    models: dict[str, ModelConfig] = {}
    # Routing (populated in validator for get_chain compat)
    routing: dict[str, dict[str, list[str]]] = {}

    @model_validator(mode="after")
    def validate_and_flatten(self) -> "ModelRegistryConfig":
        """Flatten provider models and validate action chains."""
        errors: list[str] = []

        # 1. Flatten providers → models dict
        for provider_name, provider in self.providers.items():
            for pm in provider.models:
                tariff = None
                if pm.tariff is not None:
                    tariff = self.tariffs.get(pm.tariff)
                    if tariff is None:
                        errors.append(
                            f"Model '{pm.id}' names unknown tariff: '{pm.tariff}'"
                        )
                self.models[pm.id] = ModelConfig(
                    model_id=pm.id,
                    provider=provider_name,
                    capabilities=pm.capabilities,
                    max_context=pm.max_context,
                    max_output_tokens=pm.max_output_tokens,
                    unit_type=pm.unit_type,
                    cost_per_1k=(
                        CostPer1K(input=pm.cost_per_1k_in, output=pm.cost_per_1k_out)
                        if pm.cost_per_1k_in is not None
                        and pm.cost_per_1k_out is not None
                        else None
                    ),
                    cost_per_minute=pm.cost_per_minute,
                    prices=ModelPrices(
                        in_per_1k=_exact(pm.cost_per_1k_in),
                        in_cached_per_1k=_exact(pm.cost_per_1k_in_cached),
                        out_per_1k=_exact(pm.cost_per_1k_out),
                        per_minute=_exact(pm.cost_per_minute),
                    ),
                    tariff=tariff,
                    price_effective_from=pm.price_effective_from,
                    price_holds_to_input_tokens=pm.price_holds_to_input_tokens,
                    billing_granularity_sec=pm.billing_granularity_sec,
                    local=pm.local,
                )

        # 2. Build routing from action chains + validate
        for action_name, action in self.actions.items():
            if not action.chain:
                errors.append(f"Action '{action_name}' has no chain defined")
                continue

            if "default" not in action.chain:
                errors.append(f"Action '{action_name}' chain must have 'default' entry")

            self.routing[action_name] = action.chain

            for strategy_name, model_chain in action.chain.items():
                if not model_chain:
                    errors.append(
                        f"Action '{action_name}' strategy "
                        f"'{strategy_name}' has empty model chain"
                    )
                    continue

                for model_id in model_chain:
                    if model_id not in self.models:
                        errors.append(
                            f"Routing '{action_name}.{strategy_name}' "
                            f"references unknown model: '{model_id}'"
                        )
                        continue

                    model = self.models[model_id]
                    missing = set(action.requires) - set(model.capabilities)
                    if missing:
                        errors.append(
                            f"Model '{model_id}' in "
                            f"'{action_name}.{strategy_name}' "
                            f"lacks required capabilities: {missing}"
                        )

        if errors:
            raise ValueError(
                "Registry validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
            )
        return self

    @property
    def catalog_version(self) -> str | None:
        """The catalog version register rows are priced under, if named."""
        return self.catalog.version if self.catalog is not None else None

    def pricing_fingerprint(self) -> str:
        """SHA-256 of the price data: every model's prices and rules, and the tariffs.

        Computed over parsed values, not over the text, so a comment or a key
        order never moves it; any price, pricing rule or tariff does.
        """

        def text(value: object) -> object:
            return None if value is None else str(value)

        models: dict[str, Any] = {}
        for provider in self.providers.values():
            for pm in provider.models:
                models[pm.id] = {
                    "unit_type": pm.unit_type,
                    "cost_per_1k_in": text(_exact(pm.cost_per_1k_in)),
                    "cost_per_1k_out": text(_exact(pm.cost_per_1k_out)),
                    "cost_per_1k_in_cached": text(_exact(pm.cost_per_1k_in_cached)),
                    "cost_per_minute": text(_exact(pm.cost_per_minute)),
                    "price_effective_from": text(pm.price_effective_from),
                    "price_holds_to_input_tokens": pm.price_holds_to_input_tokens,
                    "billing_granularity_sec": pm.billing_granularity_sec,
                    "tariff": pm.tariff,
                }
        tariffs = {
            name: {
                "timezone": tariff.timezone,
                "catalog_prices": tariff.catalog_prices,
                "peak_multiplier": str(tariff.peak_multiplier),
                "weekdays": [day.value for day in tariff.peak.weekdays],
                "hours": tariff.peak.hours,
            }
            for name, tariff in self.tariffs.items()
        }
        canonical = json.dumps(
            {"models": models, "tariffs": tariffs}, sort_keys=True, ensure_ascii=True
        )
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()

    def get_chain(
        self,
        action: str,
        strategy: str = "default",
    ) -> list[ModelConfig]:
        """Get ordered model chain for action + strategy.

        Falls back to 'default' strategy if requested strategy not found.

        Raises:
            KeyError: if action not found in routing.
        """
        if action not in self.routing:
            raise KeyError(f"Unknown action: '{action}'")

        strategies = self.routing[action]
        chain = strategies.get(strategy) or strategies["default"]
        return [self.models[mid] for mid in chain]

    def get_available_strategies(self, action: str) -> list[str]:
        """List available strategies for an action."""
        if action not in self.routing:
            raise KeyError(f"Unknown action: '{action}'")
        return list(self.routing[action].keys())


def load_registry(config_path: Path) -> ModelRegistryConfig:
    """Load and validate service registry from YAML.

    Args:
        config_path: Path to external_services.yaml.

    The file must carry its ``catalog`` block, and the fingerprint recorded
    there must be the fingerprint of the price data in the file (BIL-C,
    decision 1): a price that changed without a new version stops the process
    here, like the startup check of rung prices does.

    Raises:
        FileNotFoundError: if YAML file doesn't exist.
        ValueError: if YAML parsing or validation fails, the catalog block is
            missing, or its fingerprint does not match the price data.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Registry config not found: {config_path}")

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ValueError(f"Failed to parse registry config '{config_path}': {e}") from e
    registry = ModelRegistryConfig.model_validate(raw)
    if registry.catalog is None:
        raise ValueError(
            f"Registry config '{config_path}' has no 'catalog' block "
            "(version + pricing_sha256): register rows could not name the "
            "prices they were priced at"
        )
    actual = registry.pricing_fingerprint()
    if actual != registry.catalog.pricing_sha256:
        raise ValueError(
            f"Registry config '{config_path}': the price data changed but the "
            f"catalog version '{registry.catalog.version}' did not. Give the "
            f"catalog a new version and set pricing_sha256: {actual}"
        )
    return registry
