"""Pricing of one register row (BIL-C, ``llm/pricing.py``).

Locks, each with the code mutation that turns it red:

* the tariff window is half-open and weekday-bound — red if the end minute is
  counted as peak (``<=``) or the weekday check is dropped;
* the peak price comes from the tariff's multiplier — red if the multiplier is
  not applied;
* cached tokens are priced at the cached price — red if they are priced at the
  cache-miss rate; without a cached price the row is estimated, never exact;
* the failure table of decision P4 — red if any listed case changes side;
* a failed call is estimated from our input count, never NULL or zero — red if
  the old "no response, no cost" gate returns;
* no price is unknown with a NULL sum, never zero.
"""

from __future__ import annotations

import socket
import ssl
from datetime import UTC, date, datetime
from decimal import Decimal

import aiohttp
import anthropic
import httpx
import openai
import pytest
from google.genai import errors as genai_errors

from course_supporter.cost_kinds import CostKind, TariffWindow
from course_supporter.llm.pricing import (
    TokenUsage,
    failure_bills_nothing,
    price_audio_call,
    price_token_call,
)
from course_supporter.llm.providers.dashscope import DashScopeResponseError
from course_supporter.llm.registry import ModelConfig, ModelRegistryConfig

_VERSION = "test-1"
# A Monday and a Saturday in October 2026.
_MON = date(2026, 10, 26)
_SAT = date(2026, 10, 31)


def _at(day: date, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=UTC)


def _registry(**model_fields: object) -> ModelRegistryConfig:
    model: dict[str, object] = {
        "id": "m",
        "unit_type": "tokens",
        "cost_per_1k_in": 0.0002,
        "cost_per_1k_out": 0.0008,
    }
    model.update(model_fields)
    return ModelRegistryConfig.model_validate(
        {
            "catalog": {"version": _VERSION, "pricing_sha256": "0" * 64},
            "tariffs": {
                "peak": {
                    "timezone": "UTC",
                    "catalog_prices": "off_peak",
                    "peak_multiplier": 2,
                    "peak": {
                        "weekdays": ["mon", "tue", "wed", "thu", "fri"],
                        "hours": ["01:00-04:00", "06:00-10:00"],
                    },
                }
            },
            "providers": {
                "p": {"type": "llm", "models": [model]},
                "s": {
                    "type": "stt",
                    "models": [
                        {"id": "stt", "unit_type": "minutes", "cost_per_minute": 0.006}
                    ],
                },
            },
            "actions": {"a": {"chain": {"default": ["m"]}}},
        }
    )


def _model(**fields: object) -> ModelConfig:
    return _registry(**fields).models["m"]


def _price(
    model: ModelConfig | None,
    *,
    at: datetime | None = None,
    usage: TokenUsage | None = None,
    failure: BaseException | None = None,
    input_estimate: int | None = 1000,
    output_estimate: int | None = None,
    catalog_version: str | None = _VERSION,
):  # type: ignore[no-untyped-def]
    return price_token_call(
        model,
        catalog_version=catalog_version,
        started_at=at or _at(_SAT, 12),
        usage=usage,
        failure=failure,
        input_estimate=input_estimate,
        output_estimate=output_estimate,
    )


class TestTariffWindow:
    """Peak 01:00-04:00 and 06:00-10:00 UTC, Monday to Friday, half-open."""

    @pytest.mark.parametrize(
        ("instant", "window"),
        [
            (_at(_MON, 0, 59, 59), TariffWindow.OFF_PEAK),
            (_at(_MON, 1, 0), TariffWindow.PEAK),
            (_at(_MON, 3, 59, 59), TariffWindow.PEAK),
            (_at(_MON, 4, 0), TariffWindow.OFF_PEAK),
            (_at(_MON, 5, 59, 59), TariffWindow.OFF_PEAK),
            (_at(_MON, 6, 0), TariffWindow.PEAK),
            (_at(_MON, 9, 59, 59), TariffWindow.PEAK),
            (_at(_MON, 10, 0), TariffWindow.OFF_PEAK),
            (_at(date(2026, 10, 30), 9, 30), TariffWindow.PEAK),  # Friday
            (_at(_SAT, 2, 0), TariffWindow.OFF_PEAK),
            (_at(date(2026, 11, 1), 7, 0), TariffWindow.OFF_PEAK),  # Sunday
        ],
    )
    def test_window_at(self, instant: datetime, window: TariffWindow) -> None:
        tariff = _model(tariff="peak").tariff
        assert tariff is not None
        assert tariff.window_at(instant) is window

    def test_window_is_read_in_the_tariffs_timezone(self) -> None:
        tariff = _model(tariff="peak").tariff
        assert tariff is not None
        # 03:30 in Kyiv (UTC+2 in late October) is 01:30 UTC: peak.
        from zoneinfo import ZoneInfo

        kyiv = datetime(2026, 10, 26, 3, 30, tzinfo=ZoneInfo("Europe/Kyiv"))
        assert tariff.window_at(kyiv) is TariffWindow.PEAK

    def test_naive_instant_is_refused(self) -> None:
        tariff = _model(tariff="peak").tariff
        assert tariff is not None
        with pytest.raises(ValueError, match="timezone-aware"):
            tariff.window_at(datetime(2026, 10, 26, 2, 0))

    def test_model_without_tariff_is_flat(self) -> None:
        row = _price(
            _model(), usage=TokenUsage(tokens_in=1, tokens_in_cached=None, tokens_out=1)
        )
        assert row.tariff_window is TariffWindow.FLAT


class TestExactTokenPrice:
    def test_off_peak_uses_catalog_prices(self) -> None:
        row = _price(
            _model(tariff="peak"),
            at=_at(_MON, 12),
            usage=TokenUsage(tokens_in=1000, tokens_in_cached=None, tokens_out=500),
        )
        # 1000 x 0.0002/1k + 500 x 0.0008/1k
        assert row.cost_usd == Decimal("0.000600000000")
        assert row.cost_kind is CostKind.EXACT
        assert row.tariff_window is TariffWindow.OFF_PEAK
        assert row.price_in_per_1k_usd == Decimal("0.0002")
        assert row.catalog_version == _VERSION

    def test_peak_pays_the_multiplier_and_records_applied_prices(self) -> None:
        row = _price(
            _model(tariff="peak"),
            at=_at(_MON, 2),
            usage=TokenUsage(tokens_in=1000, tokens_in_cached=None, tokens_out=500),
        )
        assert row.cost_usd == Decimal("0.001200000000")
        assert row.tariff_window is TariffWindow.PEAK
        assert row.price_in_per_1k_usd == Decimal("0.0004")
        assert row.price_out_per_1k_usd == Decimal("0.0016")

    def test_cached_tokens_are_priced_at_the_cached_price(self) -> None:
        row = _price(
            _model(cost_per_1k_in_cached=0.00002),
            usage=TokenUsage(tokens_in=1000, tokens_in_cached=600, tokens_out=0),
        )
        # 400 x 0.0002/1k + 600 x 0.00002/1k
        assert row.cost_usd == Decimal("0.000092000000")
        assert row.cost_kind is CostKind.EXACT
        assert row.price_in_cached_per_1k_usd == Decimal("0.00002")

    def test_cached_tokens_without_a_cached_price_are_estimated(self) -> None:
        row = _price(
            _model(),
            usage=TokenUsage(tokens_in=1000, tokens_in_cached=600, tokens_out=0),
        )
        assert row.cost_usd == Decimal("0.000200000000")  # all at the miss rate
        assert row.cost_kind is CostKind.ESTIMATED
        assert row.price_in_cached_per_1k_usd is None

    def test_zero_cached_tokens_without_a_cached_price_stay_exact(self) -> None:
        row = _price(
            _model(), usage=TokenUsage(tokens_in=10, tokens_in_cached=0, tokens_out=1)
        )
        assert row.cost_kind is CostKind.EXACT

    def test_reported_zero_usage_is_an_exact_zero(self) -> None:
        row = _price(
            _model(), usage=TokenUsage(tokens_in=0, tokens_in_cached=None, tokens_out=0)
        )
        assert row.cost_usd == Decimal(0)
        assert row.cost_kind is CostKind.EXACT

    def test_price_not_yet_in_force_is_estimated(self) -> None:
        model = _model(price_effective_from="2027-01-01")
        usage = TokenUsage(tokens_in=10, tokens_in_cached=None, tokens_out=1)
        before = _price(model, at=_at(date(2026, 12, 31), 23, 59), usage=usage)
        on = _price(model, at=_at(date(2027, 1, 1), 0, 0), usage=usage)
        assert before.cost_kind is CostKind.ESTIMATED
        assert on.cost_kind is CostKind.EXACT
        assert before.cost_usd == on.cost_usd

    def test_prompt_above_the_price_tier_is_estimated(self) -> None:
        model = _model(price_holds_to_input_tokens=1000)
        within = _price(
            model, usage=TokenUsage(tokens_in=1000, tokens_in_cached=None, tokens_out=1)
        )
        above = _price(
            model, usage=TokenUsage(tokens_in=1001, tokens_in_cached=None, tokens_out=1)
        )
        assert within.cost_kind is CostKind.EXACT
        assert above.cost_kind is CostKind.ESTIMATED

    def test_no_catalog_version_means_nothing_is_exact(self) -> None:
        row = _price(
            _model(),
            usage=TokenUsage(tokens_in=10, tokens_in_cached=None, tokens_out=1),
            catalog_version=None,
        )
        assert row.cost_kind is CostKind.ESTIMATED
        assert row.cost_usd is not None


class TestResponseWithoutUsage:
    def test_both_counts_missing_use_ours(self) -> None:
        row = _price(
            _model(),
            usage=TokenUsage(tokens_in=None, tokens_in_cached=None, tokens_out=None),
            input_estimate=1000,
            output_estimate=100,
        )
        assert row.cost_kind is CostKind.ESTIMATED
        assert row.cost_usd == Decimal("0.000280000000")
        assert row.unit_out_estimate == 100

    def test_missing_input_count_with_attachments_is_unknown(self) -> None:
        row = _price(
            _model(),
            usage=TokenUsage(tokens_in=None, tokens_in_cached=None, tokens_out=5),
            input_estimate=None,
        )
        assert row.cost_kind is CostKind.UNKNOWN
        assert row.cost_usd is None


class TestFailedCall:
    def test_failure_that_may_bill_is_estimated_from_our_input_count(self) -> None:
        row = _price(_model(), failure=httpx.ReadTimeout("slow"), input_estimate=1000)
        assert row.cost_kind is CostKind.ESTIMATED
        assert row.cost_usd == Decimal("0.000200000000")  # input part only

    def test_failure_at_peak_uses_the_peak_price(self) -> None:
        row = _price(
            _model(tariff="peak"),
            at=_at(_MON, 7),
            failure=httpx.ReadTimeout("slow"),
            input_estimate=1000,
        )
        assert row.cost_usd == Decimal("0.000400000000")

    def test_failure_with_attachments_is_unknown(self) -> None:
        row = _price(_model(), failure=httpx.ReadTimeout("slow"), input_estimate=None)
        assert row.cost_kind is CostKind.UNKNOWN
        assert row.cost_usd is None

    def test_failure_the_provider_does_not_bill_is_an_exact_zero(self) -> None:
        row = _price(_model(), failure=httpx.ConnectError("refused"))
        assert row.cost_kind is CostKind.EXACT
        assert row.cost_usd == Decimal(0)
        assert row.tariff_window is TariffWindow.FLAT

    def test_no_response_and_no_exception_is_estimated(self) -> None:
        # A cancelled attempt: nothing raised that the router could keep.
        row = _price(_model(), failure=None, input_estimate=10)
        assert row.cost_kind is CostKind.ESTIMATED


class TestUnknownPrice:
    def test_model_not_in_the_registry(self) -> None:
        row = _price(
            None, usage=TokenUsage(tokens_in=10, tokens_in_cached=None, tokens_out=1)
        )
        assert row.cost_kind is CostKind.UNKNOWN
        assert row.cost_usd is None

    def test_model_without_a_price(self) -> None:
        row = _price(
            _model(cost_per_1k_in=None, cost_per_1k_out=None),
            usage=TokenUsage(tokens_in=10, tokens_in_cached=None, tokens_out=1),
        )
        assert row.cost_kind is CostKind.UNKNOWN
        assert row.cost_usd is None


def _status_error(
    cls: type[openai.APIStatusError], status: int
) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.example/v1")
    response = httpx.Response(status, request=request)
    return cls("boom", response=response, body=None)


def _wrapped(error: openai.APIConnectionError, cause: BaseException) -> BaseException:
    error.__cause__ = cause
    return error


_REQUEST = httpx.Request("POST", "https://api.example/v1")


class TestFailureBillsNothing:
    """Decision P4: only a 429 and a connection that never sent the request."""

    @pytest.mark.parametrize(
        "failure",
        [
            _status_error(openai.RateLimitError, 429),
            _status_error(anthropic.RateLimitError, 429),  # type: ignore[arg-type]
            genai_errors.ClientError(429, {"error": {"message": "quota"}}),
            DashScopeResponseError(429, "Throttling", "rate limited"),
            httpx.HTTPStatusError(
                "429", request=_REQUEST, response=httpx.Response(429, request=_REQUEST)
            ),
            httpx.ConnectError("connection refused"),
            httpx.ConnectTimeout("connect timed out"),
            _wrapped(
                openai.APIConnectionError(request=_REQUEST), httpx.ConnectError("dns")
            ),
            ConnectionRefusedError(111, "refused"),
            socket.gaierror(-2, "Name or service not known"),
            ssl.SSLCertVerificationError("certificate verify failed"),
            aiohttp.ClientConnectorError(
                aiohttp.client_reqrep.ConnectionKey(  # type: ignore[call-arg]
                    "api.example", 443, True, True, None, None, None
                ),
                OSError(111, "refused"),
            ),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_not_billed(self, failure: BaseException) -> None:
        assert failure_bills_nothing(failure) is True

    @pytest.mark.parametrize(
        "failure",
        [
            httpx.ReadTimeout("read timed out"),
            httpx.RemoteProtocolError("peer closed connection"),
            httpx.ReadError("connection reset"),
            openai.APITimeoutError(request=_REQUEST),
            _wrapped(
                openai.APIConnectionError(request=_REQUEST), httpx.ReadError("reset")
            ),
            _status_error(openai.InternalServerError, 500),
            _status_error(openai.BadRequestError, 400),
            genai_errors.ServerError(503, {"error": {"message": "overloaded"}}),
            DashScopeResponseError(500, "InternalError", "boom"),
            ConnectionResetError(104, "reset by peer"),
            ValueError("anything else"),
        ],
        ids=lambda e: type(e).__name__,
    )
    def test_may_be_billed(self, failure: BaseException) -> None:
        assert failure_bills_nothing(failure) is False

    def test_context_is_not_followed(self) -> None:
        # An exception that was merely being handled when this one was raised
        # says nothing about this one.
        try:
            try:
                raise httpx.ConnectError("earlier, handled")
            except httpx.ConnectError:
                raise httpx.ReadTimeout("this one")  # noqa: B904
        except httpx.ReadTimeout as exc:
            assert failure_bills_nothing(exc) is False


class TestAudioPrice:
    def _stt(self, **fields: object) -> ModelConfig:
        registry = _registry()
        model = registry.models["stt"]
        return model.model_copy(update=fields)

    def test_with_granularity_is_exact_and_rounded_up(self) -> None:
        row = price_audio_call(
            self._stt(billing_granularity_sec=15),
            catalog_version=_VERSION,
            started_at=_at(_MON, 2),
            duration_sec=61.2,
            succeeded=True,
            failure=None,
        )
        assert row.billed_seconds == 75
        assert row.cost_usd == Decimal("0.007500000000")  # 75 s x 0.006 / 60
        assert row.cost_kind is CostKind.EXACT
        assert row.tariff_window is TariffWindow.FLAT

    def test_without_granularity_is_estimated(self) -> None:
        row = price_audio_call(
            self._stt(),
            catalog_version=_VERSION,
            started_at=_at(_MON, 2),
            duration_sec=61.2,
            succeeded=True,
            failure=None,
        )
        assert row.billed_seconds == 62
        assert row.cost_kind is CostKind.ESTIMATED

    def test_unknown_duration_is_unknown(self) -> None:
        row = price_audio_call(
            self._stt(billing_granularity_sec=1),
            catalog_version=_VERSION,
            started_at=_at(_MON, 2),
            duration_sec=None,
            succeeded=True,
            failure=None,
        )
        assert row.cost_kind is CostKind.UNKNOWN
        assert row.cost_usd is None

    def test_failure_that_may_bill_is_estimated(self) -> None:
        row = price_audio_call(
            self._stt(billing_granularity_sec=1),
            catalog_version=_VERSION,
            started_at=_at(_MON, 2),
            duration_sec=30,
            succeeded=False,
            failure=httpx.ReadTimeout("slow"),
        )
        assert row.cost_kind is CostKind.ESTIMATED
        assert row.cost_usd == Decimal("0.003000000000")

    def test_rate_limited_failure_is_an_exact_zero(self) -> None:
        row = price_audio_call(
            self._stt(),
            catalog_version=_VERSION,
            started_at=_at(_MON, 2),
            duration_sec=30,
            succeeded=False,
            failure=httpx.HTTPStatusError(
                "429", request=_REQUEST, response=httpx.Response(429, request=_REQUEST)
            ),
        )
        assert row.cost_kind is CostKind.EXACT
        assert row.cost_usd == Decimal(0)
