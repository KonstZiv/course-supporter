"""The catalog of packages, ``config/billing_packages.yaml`` (BIL-PAY).

The shipped file loads and sells the four packages of the pricing page; the
shape of a package is locked field by field; a file that breaks a rule is
refused when it is read. Creem's fee tariff (``provider_fees``, Б-5) is as the
product owner set it on 2026-11-27, its sources beside the numbers.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from course_supporter.billing.payments.catalog import (
    REQUIRED_CURRENCY,
    Catalog,
    FeeTariff,
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

    def test_its_live_creem_products(self) -> None:
        """BIL-LIVE: the live Creem products (the operator's live dashboard,
        2026-11-18) in live mode, the test ones in test mode — one per active
        package; never a test id live, never the sandbox package."""
        catalog = load_catalog(SHIPPED)
        expected_live = {
            "units-500-v1": "prod_641KyNDG0h2IN0GPNJ7zKT",
            "units-1500-v1": "prod_6JbFesPKLrkcURhHH8F3sk",
            "units-5000-v1": "prod_5t0ORZST2Hw4ZqW8hV5qC7",
            "units-10000-v1": "prod_1TE2jSDw0OcglA1TY0TC1O",
        }
        expected_test = {
            "units-500-v1": "prod_IjKGKARYgwR25ktF0By1A",
            "units-1500-v1": "prod_7MjqlyfEXv7zVO37m0vCjN",
            "units-5000-v1": "prod_5DTm2EO5XM9Rbm53bRKVps",
            "units-10000-v1": "prod_2u00d6l8qzsfhjeaDTl6FL",
        }
        assert catalog.providers_live == {"creem": expected_live}
        for package_id in expected_live:
            live = catalog.provider_item("creem", package_id, test_mode=False)
            test = catalog.provider_item("creem", package_id, test_mode=True)
            assert (live, test) == (
                expected_live[package_id],
                expected_test[package_id],
            )
        assert (
            catalog.provider_item("creem", "sandbox-test-500", test_mode=False) is None
        )
        assert not set(expected_live.values()) & set(
            catalog.providers["creem"].values()
        )
        assert set(expected_live) == {p.id for p in catalog.active()}

    def test_its_packages_have_exactly_the_locked_fields(self) -> None:
        """A package on sale states every field; one off sale may leave out
        its tax mode (added on 2026-11-20 to the packages on sale only)."""
        raw = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
        for package in raw["packages"]:
            optional = set() if package["active"] else {"tax_mode"}
            assert set(Package.model_fields) - optional <= set(package)
            assert set(package) <= set(Package.model_fields)

    def test_the_tax_is_added_on_top_of_every_package_on_sale(self) -> None:
        """Operator 2026-11-20: the tax is added on top (the products in Creem)."""
        catalog = load_catalog(SHIPPED)
        assert {p.id: p.tax_mode for p in catalog.active()} == {
            "units-500-v1": "exclusive",
            "units-1500-v1": "exclusive",
            "units-5000-v1": "exclusive",
            "units-10000-v1": "exclusive",
        }
        sandbox = catalog.package("sandbox-test-500")
        assert sandbox is not None and sandbox.tax_mode is None


def test_the_shape_is_locked() -> None:
    """Adding a field to a package or to the file is a decision, not a drift."""
    assert set(Package.model_fields) == {
        "id",
        "units",
        "prices_minor",
        "active",
        "tax_mode",
    }
    assert set(Catalog.model_fields) == {
        "packages",
        "providers",
        "providers_live",
        "provider_fees",  # Б-5: the providers' fee tariffs
    }
    assert set(FeeTariff.model_fields) == {"percent", "fixed_minor"}


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
        (_package(tax_mode="included"), "'inclusive' or 'exclusive'"),
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


# ── The providers' fee tariffs (Б-5) ───────────────────────────────────


class TestShippedFeeTariff:
    def test_creem_is_39_percent_and_40_cents_eur_not_set(self) -> None:
        creem = load_catalog(SHIPPED).provider_fees["creem"]
        assert creem.percent == Decimal("3.9")
        assert creem.fixed_minor == {"USD": 40, "EUR": None}

    def test_each_number_names_its_source_and_date(self) -> None:
        """The product owner's rule: no number without its source."""
        text = SHIPPED.read_text(encoding="utf-8")
        source = (
            "# джерело: https://creem.io/pricing — 3.9% + 40¢ per transaction; "
            "перевірено 2026-11-27"
        )
        lines = text.splitlines()
        for number in ("    percent: 3.9", "      USD: 40"):
            at = lines.index(number)
            assert lines[at - 1].strip() == source, number
        at = lines.index("      EUR: null")
        assert lines[at - 1].strip() == (
            "# не задано: підтвердити в кабінеті / договорі Creem"
        )


class TestFeeTariff:
    @pytest.mark.parametrize(
        ("paid", "fee"),
        [
            (1000, 39 + 40),  # 39.0
            (1500, 59 + 40),  # 58.5: half up, not to even
            (2000, 78 + 40),  # 78.0
            (1210, 47 + 40),  # 47.19
            (0, 40),
        ],
    )
    def test_the_formula(self, paid: int, fee: int) -> None:
        tariff = FeeTariff(percent=Decimal("3.9"), fixed_minor={"USD": 40})
        assert tariff.fee_minor(paid, "USD") == fee

    def test_a_currency_without_a_fixed_part_has_no_fee(self) -> None:
        tariff = FeeTariff(percent=Decimal("3.9"), fixed_minor={"USD": 40, "EUR": None})
        assert tariff.fee_minor(1000, "EUR") is None
        assert tariff.fee_minor(1000, "GBP") is None

    @pytest.mark.parametrize(
        ("tariff", "error"),
        [
            ({"percent": -1, "fixed_minor": {}}, "greater than or equal to 0"),
            ({"percent": 101, "fixed_minor": {}}, "less than or equal to 100"),
            ({"percent": "3.905", "fixed_minor": {}}, "2 decimal places"),
            ({"percent": 3.9, "fixed_minor": {"usd": 40}}, "ISO 4217"),
            ({"percent": 3.9, "fixed_minor": {"USD": -1}}, "cannot be negative"),
            ({"percent": 3.9, "fixed_minor": {"USD": 0.4}}, "valid integer"),
            ({"percent": 3.9, "fixed": {"USD": 40}}, "Extra inputs"),
        ],
    )
    def test_a_bad_tariff_is_refused(
        self, tmp_path: Path, tariff: dict[str, Any], error: str
    ) -> None:
        data = {"packages": [_package()], "provider_fees": {"creem": tariff}}
        with pytest.raises(ValidationError, match=error):
            load_catalog(_write(tmp_path, data))

    def test_a_tariff_names_a_provider(self, tmp_path: Path) -> None:
        data = {
            "packages": [_package()],
            "provider_fees": {"Creem": {"percent": 3.9}},
        }
        with pytest.raises(ValidationError, match="not a provider's name"):
            load_catalog(_write(tmp_path, data))
