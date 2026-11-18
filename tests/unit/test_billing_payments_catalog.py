"""The catalog of packages, ``config/billing_packages.yaml`` (BIL-PAY).

The shipped file loads and sells the four packages of the pricing page; the
shape of a package is locked field by field; a file that breaks a rule is
refused when it is read.
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

    def test_its_header_documents_pricing_page(self) -> None:
        """The placeholder packages were removed; the header documents that
        the active packages match the pricing page, with provider product ids
        in test mode."""
        text = SHIPPED.read_text(encoding="utf-8")
        assert "рішення оператора; не публікувати" not in text
        assert "Активні пакети — ті, що на сторінці цін" in text

    def test_sandbox_package_loads(self) -> None:
        catalog = load_catalog(SHIPPED)
        sandbox = catalog.package("sandbox-test-500")
        assert sandbox is not None
        assert sandbox.units == 500
        assert sandbox.prices_minor == {"EUR": 500}
        assert sandbox.active is False
        assert (
            catalog.provider_item("creem", "sandbox-test-500", test_mode=True)
            == "prod_2oJ3Yqnf53wOrikCrQGDtk"
        )

    def test_pricing_page_packages_are_active(self) -> None:
        catalog = load_catalog(SHIPPED)
        active = catalog.active()
        assert tuple(p.id for p in active) == (
            "units-500-v1",
            "units-1500-v1",
            "units-5000-v1",
            "units-10000-v1",
        )
        assert tuple(p.units for p in active) == (500, 1500, 5000, 10000)
        assert tuple(p.prices_minor["EUR"] for p in active) == (999, 1499, 3999, 6999)
        assert all(p.active for p in active)

        expected_creem = {
            "units-500-v1": "prod_IjKGKARYgwR25ktF0By1A",
            "units-1500-v1": "prod_7MjqlyfEXv7zVO37m0vCjN",
            "units-5000-v1": "prod_5DTm2EO5XM9Rbm53bRKVps",
            "units-10000-v1": "prod_2u00d6l8qzsfhjeaDTl6FL",
        }
        for package_id, expected_product in expected_creem.items():
            assert (
                catalog.provider_item("creem", package_id, test_mode=True)
                == expected_product
            )

        sandbox = catalog.package("sandbox-test-500")
        assert sandbox is not None
        assert sandbox.active is False

    def test_its_live_creem_section_never_sells_the_sandbox_package(self) -> None:
        """BIL-LIVE: the live section is there (empty until the operator's
        live products); never a test id, never the sandbox package."""
        catalog = load_catalog(SHIPPED)
        assert "creem" in catalog.providers_live
        live = catalog.providers_live["creem"]
        assert "sandbox-test-500" not in live
        assert not set(live.values()) & set(catalog.providers["creem"].values())
        assert set(live) <= {p.id for p in catalog.active()}

    def test_its_packages_have_exactly_the_locked_fields(self) -> None:
        raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
        for package in raw["packages"]:
            assert set(package) == set(Package.model_fields)


def test_the_shape_is_locked() -> None:
    """Adding a field to a package or to the file is a decision, not a drift."""
    assert set(Package.model_fields) == {"id", "units", "prices_minor", "active"}
    assert set(Catalog.model_fields) == {"packages", "providers", "providers_live"}


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
    assert catalog.provider_item("paddle", "starter", test_mode=True) == "pri_1"
    assert catalog.provider_item("paddle", "old", test_mode=True) is None
    assert catalog.provider_item("creem", "starter", test_mode=True) is None
    assert catalog.provider_item("fake", "starter", test_mode=True) is None


def test_each_mode_reads_its_own_table(tmp_path: Path) -> None:
    """BIL-LIVE: test mode reads ``providers``, live mode ``providers_live``
    only — a package without a live id has none in live mode, never the test
    one."""
    catalog = load_catalog(
        _write(
            tmp_path,
            {
                "packages": [_package(), _package(id="other")],
                "providers": {"creem": {"starter": "prod_test", "other": "prod_t2"}},
                "providers_live": {"creem": {"starter": "prod_live"}},
            },
        )
    )
    assert catalog.provider_item("creem", "starter", test_mode=True) == "prod_test"
    assert catalog.provider_item("creem", "starter", test_mode=False) == "prod_live"
    assert catalog.provider_item("creem", "other", test_mode=False) is None
    assert catalog.provider_item("creem", "other", test_mode=True) == "prod_t2"


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
        (
            {"packages": [_package()], "providers_live": {"creem": {"x": "p"}}},
            "providers_live.creem names no such package",
        ),
        (
            {"packages": [_package()], "providers_live": {"creem": {"starter": ""}}},
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
