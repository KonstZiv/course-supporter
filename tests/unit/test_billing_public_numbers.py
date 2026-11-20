"""The public numbers without a database (BIL-PUBLIC-NUMBERS).

The store's one answer per start, the address (503, ETag, 304), the packages
line and the reserve's multiplier. The numbers from a base:
``tests/integration/test_billing_public_numbers_db.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from course_supporter.api.app import app
from course_supporter.api.schemas import PublicNumbersResponse
from course_supporter.billing.payments.catalog import get_catalog
from course_supporter.billing.public_numbers import (
    KEY_PREFIX,
    KEY_TTL_SECONDS,
    Actual,
    PublicNumbers,
    PublicNumbersStore,
    ReviewCost,
    ReviewGroup,
    Rules,
    Sample,
    SmallestPackageReviews,
    StartThreshold,
    _empty_group,
    _packages,
    dump,
    start_key,
)
from course_supporter.config import get_settings
from course_supporter.cost_estimate.core import RESERVE_MULTIPLIER, reserve_units

URL = "/api/v1/public/numbers"


class FakeRedis:
    """GETEX and SET NX/EX of one Redis, as the store uses them."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.ttl: dict[str, int] = {}

    async def getex(self, name: str, *, ex: int) -> bytes | None:
        if name in self.values:
            self.ttl[name] = ex
        return self.values.get(name)

    async def set(self, name: str, value: bytes, *, ex: int, nx: bool) -> bool:
        if nx and name in self.values:
            return False
        self.values[name], self.ttl[name] = value, ex
        return True


def make_numbers(reviews: int = 3) -> PublicNumbers:
    window = get_settings().public_numbers_window_days
    return PublicNumbers(
        contract_version=1,
        as_of=datetime.now(UTC),
        rules=Rules(reserve_multiplier=RESERVE_MULTIPLIER),
        packages=_packages(get_catalog()),
        review_cost=ReviewCost(
            homework=ReviewGroup(
                status="ready",
                actual=Actual(45, 90.0, 135),
                start_threshold=StartThreshold(270.0, 324),
                smallest_package_reviews=SmallestPackageReviews(2, 3, 4),
                sample=Sample(reviews, window, includes_internal=True),
            ),
            project=_empty_group("not_charged", window),
        ),
    )


def counting(
    *failures: Exception,
) -> tuple[Callable[[], Awaitable[PublicNumbers]], list[int]]:
    """A computation that fails with ``failures`` first, then counts its calls."""
    calls: list[int] = []
    pending = list(failures)

    async def compute() -> PublicNumbers:
        calls.append(1)
        if pending:
            raise pending.pop(0)
        return make_numbers(reviews=len(calls))

    return compute, calls


class TestTheStore:
    async def test_the_processes_of_one_start_share_the_first_computation(
        self,
    ) -> None:
        redis = FakeRedis()
        compute, calls = counting()

        first = await PublicNumbersStore(redis, "k", compute).get()
        second = await PublicNumbersStore(redis, "k", compute).get()
        another_start = await PublicNumbersStore(redis, "k2", compute).get()

        assert first is not None and second is not None and another_start is not None
        assert len(calls) == 2  # "k" once, "k2" once
        assert (second.body, second.etag, second.numbers.as_of) == (
            first.body,
            first.etag,
            first.numbers.as_of,
        )
        assert another_start.etag != first.etag
        assert redis.ttl == {"k": KEY_TTL_SECONDS, "k2": KEY_TTL_SECONDS}

    async def test_a_process_that_lost_the_race_serves_the_winners_numbers(
        self,
    ) -> None:
        redis = FakeRedis()
        winner = make_numbers(reviews=7)

        async def compute() -> PublicNumbers:
            # Another process writes while this one computes.
            await redis.set("k", dump(winner), ex=KEY_TTL_SECONDS, nx=True)
            return make_numbers(reviews=1)

        found = await PublicNumbersStore(redis, "k", compute).get()

        assert found is not None and found.body == dump(winner)

    async def test_a_failure_is_none_and_the_next_call_tries_again(self) -> None:
        redis = FakeRedis()
        compute, calls = counting(RuntimeError("the base is down"))
        store = PublicNumbersStore(redis, "k", compute)

        assert await store.get() is None
        assert await store.get() is not None
        assert len(calls) == 2

    def test_the_key_of_a_start_is_the_same_for_every_call(self) -> None:
        assert start_key() == start_key()
        assert start_key().startswith(f"{KEY_PREFIX}:")


@pytest.fixture()
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as found:
        yield found


@pytest.fixture()
def serving(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Put a store over a fake Redis on the app, as the lifespan does."""

    def serve(compute: Callable[[], Awaitable[PublicNumbers]]) -> None:
        monkeypatch.setattr(
            app.state,
            "public_numbers",
            PublicNumbersStore(FakeRedis(), "k", compute),
            raising=False,
        )

    return serve


class TestTheAddress:
    async def test_without_numbers_503_then_200_once_they_come(
        self, client: AsyncClient, serving: Callable[..., None]
    ) -> None:
        compute, _ = counting(RuntimeError("the base is down"))
        serving(compute)

        refused = await client.get(URL)
        served = await client.get(URL)

        assert refused.status_code == 503
        assert refused.json()["detail"]["code"] == "numbers_unavailable"
        assert served.status_code == 200

    async def test_no_store_is_503(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(app.state, "public_numbers", raising=False)
        response = await client.get(URL)
        assert (response.status_code, response.json()["detail"]["code"]) == (
            503,
            "numbers_unavailable",
        )

    async def test_the_etag_and_304(
        self, client: AsyncClient, serving: Callable[..., None]
    ) -> None:
        compute, _ = counting()
        serving(compute)

        first = await client.get(URL)
        etag = first.headers["etag"]
        same = await client.get(URL, headers={"If-None-Match": etag})
        weak = await client.get(URL, headers={"If-None-Match": f"W/{etag}, x"})
        other = await client.get(URL, headers={"If-None-Match": '"other"'})

        assert first.status_code == 200
        assert first.headers["cache-control"] == "public, max-age=300"
        assert (same.status_code, same.content) == (304, b"")
        assert same.headers["etag"] == etag
        assert weak.status_code == 304
        assert (other.status_code, other.headers["etag"]) == (200, etag)

    async def test_the_body_is_the_contract(
        self, client: AsyncClient, serving: Callable[..., None]
    ) -> None:
        compute, _ = counting()
        serving(compute)

        body: dict[str, Any] = (await client.get(URL)).json()

        PublicNumbersResponse.model_validate(body)
        assert body["contract_version"] == 1
        assert body["rules"] == {"reserve_multiplier": 2}
        assert body["review_cost"]["homework"]["sample"] == {
            "reviews": 1,
            "window_days": 90,
            "includes_internal": True,
        }
        assert body["review_cost"]["project"]["status"] == "not_charged"

    def test_the_address_is_public_and_documented(self) -> None:
        operation = app.openapi()["paths"][URL]["get"]
        assert "security" not in operation
        assert operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/PublicNumbersResponse"}
        assert "numbers_unavailable" in operation["responses"]["503"]["description"]
        assert "304" in operation["responses"]


class TestThePackages:
    def test_the_packages_on_sale_fewest_units_first(self) -> None:
        lines = _packages(get_catalog())
        assert [(p.id, p.units, p.price_minor, p.currency) for p in lines] == [
            ("units-500-v1", 500, 999, "EUR"),
            ("units-1500-v1", 1500, 1499, "EUR"),
            ("units-5000-v1", 5000, 3999, "EUR"),
            ("units-10000-v1", 10000, 6999, "EUR"),
        ]
        assert {(p.validity_months, p.tax_mode) for p in lines} == {(6, "exclusive")}


class TestTheReserveMultiplier:
    def test_the_reserve_is_twice_e_as_before(self) -> None:
        assert RESERVE_MULTIPLIER == 2
        assert reserve_units(Decimal("0.25"), units_per_usd=Decimal(900)) == 450
        assert make_numbers().rules.reserve_multiplier == 2
