"""The catalog of packages, ``config/billing_packages.yaml`` (BIL-PAY).

The shipped file loads and keeps its placeholders off sale; the shape of a
package is locked field by field; a file that breaks a rule is refused when
it is read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from course_supporter.billing.payments.catalog import (
    REQUIRED_CURRENCY,
    Catalog,
    Package,
    get_catalog,
    load_catalog,
)
from course_supporter.config import get_settings

SHIPPED = get_settings().billing_packages_path


def _package(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "starter",
        "units": 1000,
        "prices_minor": {"EUR": 1000},
        "active": True,
    } | overrides


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "packages.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class TestShippedCatalog:
    def test_it_loads(self) -> None:
        catalog = load_catalog(SHIPPED)
        assert catalog.packages
        assert all(REQUIRED_CURRENCY in p.prices_minor for p in catalog.packages)

    def test_its_placeholders_are_off_sale(self) -> None:
        """Placeholder packages («рішення оператора; не публікувати») stay off
        sale until the operator sets the real values; the only active package
        shipped is the sandbox test package for Creem."""
        catalog = load_catalog(SHIPPED)
        assert tuple(p.id for p in catalog.active()) == ("sandbox-test-500",)
        placeholders = [p for p in catalog.packages if p.id != "sandbox-test-500"]
        assert all(not p.active for p in placeholders)
        text = SHIPPED.read_text(encoding="utf-8")
        assert text.count("рішення оператора; не публікувати") >= 2 * len(placeholders)

    def test_sandbox_package_loads(self) -> None:
        catalog = load_catalog(SHIPPED)
        sandbox = catalog.package("sandbox-test-500")
        assert sandbox is not None
        assert sandbox.units == 500
        assert sandbox.prices_minor == {"EUR": 500}
        assert sandbox.active is True
        assert (
            catalog.provider_item("creem", "sandbox-test-500")
            == "prod_2oJ3Yqnf53wOrikCrQGDtk"
        )

    def test_its_packages_have_exactly_the_locked_fields(self) -> None:
        raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
        for package in raw["packages"]:
            assert set(package) == set(Package.model_fields)


def test_the_shape_is_locked() -> None:
    """Adding a field to a package or to the file is a decision, not a drift."""
    assert set(Package.model_fields) == {"id", "units", "prices_minor", "active"}
    assert set(Catalog.model_fields) == {"packages", "providers"}


def test_lookups(tmp_path: Path) -> None:
    catalog = load_catalog(
        _write(
            tmp_path,
            {
                "packages": [
                    _package(),
                    _package(id="old", active=False, prices_minor={"EUR": 5, "USD": 6}),
                ],
                "providers": {"paddle": {"starter": "pri_1"}, "creem": {}},
            },
        )
    )
    assert [p.id for p in catalog.active()] == ["starter"]
    old = catalog.package("old")
    assert old is not None and old.prices_minor == {"EUR": 5, "USD": 6}
    assert catalog.package("nope") is None
    assert catalog.provider_item("paddle", "starter") == "pri_1"
    assert catalog.provider_item("paddle", "old") is None
    assert catalog.provider_item("creem", "starter") is None
    assert catalog.provider_item("fake", "starter") is None


@pytest.mark.parametrize(
    ("package", "error"),
    [
        (_package(prices_minor={"USD": 1000}), "a price in EUR"),
        (_package(prices_minor={"EUR": 0}), "must be positive"),
        (_package(prices_minor={"EUR": 100, "eur": 100}), "not an ISO 4217"),
        (_package(units=0), "greater than 0"),
        (_package(units=True), "valid integer"),
        (_package(units=1.5), "valid integer"),
        (_package(active="yes"), "valid boolean"),
        (_package(id="Starter Pack"), "pattern"),
        (_package(colour="red"), "Extra inputs"),
        ({k: v for k, v in _package().items() if k != "active"}, "Field required"),
    ],
)
def test_a_bad_package_is_refused(
    tmp_path: Path, package: dict[str, Any], error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        load_catalog(_write(tmp_path, {"packages": [package]}))


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"packages": [_package(), _package()]}, "package ids repeat"),
        (
            {"packages": [_package()], "providers": {"paddle": {"other": "pri_1"}}},
            "names no such package",
        ),
        (
            {"packages": [_package()], "providers": {"Paddle": {}}},
            "not a provider's name",
        ),
        (
            {"packages": [_package()], "providers": {"paddle": {"starter": ""}}},
            "1 to 64 characters",
        ),
        ({"packages": [_package()], "prices": {}}, "Extra inputs"),
    ],
)
def test_a_bad_catalog_is_refused(
    tmp_path: Path, data: dict[str, Any], error: str
) -> None:
    with pytest.raises(ValidationError, match=error):
        load_catalog(_write(tmp_path, data))


def test_a_missing_or_broken_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_catalog(tmp_path / "absent.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("packages: [", encoding="utf-8")
    with pytest.raises(ValueError, match="Failed to parse"):
        load_catalog(broken)


def test_get_catalog_reads_the_setting_and_caches(tmp_path: Path) -> None:
    custom = _write(tmp_path, {"packages": [_package(id="custom")]})
    assert get_catalog(custom).package("custom") is not None
    assert get_catalog().package("custom") is not None  # cached
    assert get_catalog(SHIPPED).package("custom") is None  # reread
