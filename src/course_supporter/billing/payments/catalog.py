"""The catalog of packages: ``config/billing_packages.yaml`` (BIL-PAY).

What a payer buys: a package — a whole number of units for a price, in minor
units of a currency (EUR always, others where set). ``active`` says whether
it is on sale: the portal lists, and a checkout opens, active packages only.
A purchase already paid is credited whatever ``active`` says by then — the
payer paid for it.

The units of a purchase are read from here when the provider says it was
paid, so a package's id stands for its units and prices for good: changing a
package means a new id.

Each provider has a section of its own — package id → the provider's id of
the product or price its checkout needs (a Creem product id, say). An empty
section, or none, is allowed; whether a provider can sell a package without
one is its adapter's business.

Two tables of such sections, by the provider's mode (its adapter's
``test_mode``), since a provider's test and live environments know different
products: ``providers`` — of the test mode; ``providers_live`` — of the live
mode (operator 2026-11-18). A checkout in live mode reads ``providers_live``
only: a package without an id there is not sold in live mode (Creem: 503),
never through its test id.

The shape is checked when the file is read, as for the other config files;
a broken catalog fails the first request that needs it, not the boot.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from course_supporter.billing.journal import PROVIDER_NAME

REQUIRED_CURRENCY: Final = "EUR"
"""Every package has a price in this currency."""

_CURRENCY = re.compile(r"[A-Z]{3}")
_MAX_ITEM_ID: Final = 64


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Package(_Frozen):
    """One package of units and its prices."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    units: StrictInt = Field(gt=0)
    prices_minor: dict[str, StrictInt]
    """Currency (ISO 4217) → price in its minor units (cents), tax included
    or not as the operator decides; the provider charges its own figure."""
    active: StrictBool
    tax_mode: Literal["inclusive", "exclusive"] | None = None
    """inclusive — the tax is already in the price; exclusive — the tax is
    added on top; as the product is set up at the merchant of record. None —
    not stated."""

    @field_validator("prices_minor")
    @classmethod
    def _prices(cls, value: dict[str, int]) -> dict[str, int]:
        if REQUIRED_CURRENCY not in value:
            msg = f"a package has a price in {REQUIRED_CURRENCY}"
            raise ValueError(msg)
        for currency, amount in value.items():
            if _CURRENCY.fullmatch(currency) is None:
                msg = f"{currency!r} is not an ISO 4217 code"
                raise ValueError(msg)
            if amount <= 0:
                msg = f"the price in {currency} must be positive"
                raise ValueError(msg)
        return value


class Catalog(_Frozen):
    """Top-level shape of ``config/billing_packages.yaml``."""

    packages: tuple[Package, ...]
    providers: dict[str, dict[str, str]] = Field(default_factory=dict)
    """Provider's name → package id → the provider's id of it, in its test
    mode."""
    providers_live: dict[str, dict[str, str]] = Field(default_factory=dict)
    """The same, in the provider's live mode (module docstring)."""

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        ids = [package.id for package in self.packages]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        if repeated:
            msg = f"package ids repeat: {repeated}"
            raise ValueError(msg)
        tables = {"providers": self.providers, "providers_live": self.providers_live}
        for table, sections in tables.items():
            for provider, items in sections.items():
                if PROVIDER_NAME.fullmatch(provider) is None:
                    msg = f"{provider!r} is not a provider's name ({table})"
                    raise ValueError(msg)
                unknown = sorted(set(items) - set(ids))
                if unknown:
                    msg = f"{table}.{provider} names no such package: {unknown}"
                    raise ValueError(msg)
                for package_id, item in items.items():
                    if not item or len(item) > _MAX_ITEM_ID:
                        msg = (
                            f"{table}.{provider} id of {package_id} must be 1 to "
                            f"{_MAX_ITEM_ID} characters"
                        )
                        raise ValueError(msg)
        return self

    def package(self, package_id: str) -> Package | None:
        """The package of that id, on sale or not; None when there is none."""
        return next((p for p in self.packages if p.id == package_id), None)

    def active(self) -> tuple[Package, ...]:
        """The packages on sale, in the file's order."""
        return tuple(p for p in self.packages if p.active)

    def provider_item(
        self, provider: str, package_id: str, *, test_mode: bool
    ) -> str | None:
        """The provider's id of the package in its mode (``providers`` in test
        mode, ``providers_live`` in live mode); None when that section has
        none."""
        table = self.providers if test_mode else self.providers_live
        return table.get(provider, {}).get(package_id)


def load_catalog(config_path: Path) -> Catalog:
    """Read and check the catalog.

    Raises:
        FileNotFoundError: the file does not exist.
        ValueError: the YAML does not parse or does not have the shape.
    """
    if not config_path.exists():
        msg = f"Catalog of packages not found: {config_path}"
        raise FileNotFoundError(msg)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"Failed to parse '{config_path}': {exc}"
        raise ValueError(msg) from exc
    return Catalog.model_validate(raw)


_catalog: Catalog | None = None


def get_catalog(config_path: Path | None = None) -> Catalog:
    """The cached catalog; read on first use (or on an override).

    Read once per process, like the other config files (deploy-based reload).
    """
    global _catalog
    if _catalog is None or config_path is not None:
        if config_path is None:
            from course_supporter.config import get_settings

            config_path = get_settings().billing_packages_path
        _catalog = load_catalog(config_path)
    return _catalog
