"""What one paid call cost, and how sure that number is (BIL-C).

Purpose:
    The call register is the money source of the platform: the funds port is
    told a stage's sum from it, the cost reports read it, margin telemetry is
    computed from it. Every row of a paid call therefore carries a sum AND its
    kind (:class:`~course_supporter.cost_kinds.CostKind`) AND the unit prices
    and catalog version it was computed from, so that any row can be checked by
    arithmetic on the row alone. This module computes all of that; it is pure
    — no I/O, no clock, no database — so the router and the speech-to-text
    router write what it returns and nothing else decides a price.

Interface:
    :func:`failure_bills_nothing` — the one classification of a failed call:
    does the provider certainly charge nothing for it (decision P4).
    :func:`price_token_call` — an LLM attempt, from the provider's usage report
    or, failing that, from our own token count.
    :func:`price_audio_call` — a speech-to-text attempt, from the audio
    duration known before the call.
    :class:`PricedCall` — the result: the columns a register row stores.

The arithmetic (``NUMERIC(18,12)``, rounded half-even to 12 places)::

    cost = ((in - cached) * p_in + cached * (p_in_cached or p_in) + out * p_out) / 1000
    cost = billed_seconds * p_per_minute / 60

with each price taken from the catalog and multiplied by the model's tariff for
the window the attempt was SENT in. ``in`` / ``out`` are the provider's counts
where it reported them, and our counts (``unit_in_estimate`` /
``unit_out_estimate``) where it did not.
"""

from __future__ import annotations

import math
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final

import aiohttp
import httpx

from course_supporter.cost_kinds import CostKind, TariffWindow
from course_supporter.llm.registry import ModelConfig

MONEY_QUANTUM: Final = Decimal("1e-12")
"""The scale of every money column (``NUMERIC(18,12)``)."""

_PER_1K: Final = Decimal(1000)
_SECONDS_PER_MINUTE: Final = Decimal(60)
_CHAIN_LIMIT: Final = 10

# ── Failures the provider certainly does not bill (decision P4) ──
#
# | Failure                                               | Bills? | Kind      |
# |-------------------------------------------------------|--------|-----------|
# | HTTP 429, from any SDK or raw httpx / aiohttp         | no     | exact 0   |
# | connection refused, DNS failure, TLS handshake        | no     | exact 0   |
# |   failure, connect timeout — before the request left  |        |           |
# | read timeout after the request was sent               | maybe  | estimated |
# | connection dropped while reading the answer           | maybe  | estimated |
# | 5xx, any other 4xx, anything not listed above         | maybe  | estimated |
#
# An SDK that wraps a transport error keeps it as ``__cause__`` (``raise ...
# from``), so the cause chain is walked; ``__context__`` is not, because it
# also holds exceptions that were merely being handled at the time.
_NOT_SENT: Final[tuple[type[BaseException], ...]] = (
    # httpx raises ConnectError for refused connections, DNS failures and TLS
    # handshake failures alike; openai / anthropic wrap it in APIConnectionError.
    httpx.ConnectError,
    httpx.ConnectTimeout,
    # aiohttp (DashScope SDK): DNS, TLS and certificate errors are subclasses.
    aiohttp.ClientConnectorError,
    ConnectionRefusedError,
    socket.gaierror,
    ssl.SSLCertVerificationError,
)
_RATE_LIMITED: Final = 429


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status an exception carries, under any SDK's spelling of it."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    for attribute in ("status_code", "status", "code"):
        value = getattr(exc, attribute, None)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 100 <= value <= 599
        ):
            return value
    return None


def failure_bills_nothing(exc: BaseException) -> bool:
    """Whether the provider certainly charges nothing for this failed call.

    True for a 429 answer and for a connection that failed before the request
    was sent (the table above); False for every other failure, which may have
    been billed and is therefore estimated.
    """
    current: BaseException | None = exc
    for _ in range(_CHAIN_LIMIT):
        if current is None:
            return False
        if _http_status(current) == _RATE_LIMITED:
            return True
        if isinstance(current, _NOT_SENT):
            return True
        current = current.__cause__
    return False


# ── The priced row ──


@dataclass(frozen=True, slots=True, kw_only=True)
class PricedCall:
    """The price columns of one register row.

    ``cost_usd`` is ``None`` exactly when ``cost_kind`` is ``UNKNOWN``. The
    prices are the ones APPLIED — catalog price times the tariff's multiplier
    for ``tariff_window`` — so the row's sum can be recomputed from the row.
    ``unit_out_estimate`` is set only when our output count went into the sum
    (a response without the provider's output count); ``billed_seconds`` only
    on a speech-to-text row. ``call_started_at`` is the instant the window was
    chosen by.
    """

    cost_usd: Decimal | None
    cost_kind: CostKind
    catalog_version: str | None
    tariff_window: TariffWindow | None
    call_started_at: datetime | None = None
    price_in_per_1k_usd: Decimal | None = None
    price_in_cached_per_1k_usd: Decimal | None = None
    price_out_per_1k_usd: Decimal | None = None
    price_per_minute_usd: Decimal | None = None
    unit_out_estimate: int | None = None
    billed_seconds: int | None = None


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _window(model: ModelConfig | None, started_at: datetime) -> TariffWindow:
    if model is None or model.tariff is None:
        return TariffWindow.FLAT
    return model.tariff.window_at(started_at)


def _applied(
    model: ModelConfig, price: Decimal | None, window: TariffWindow
) -> Decimal | None:
    if price is None or model.tariff is None:
        return price
    return price * model.tariff.multiplier(window)


def _settled(kind: CostKind, catalog_version: str | None) -> CostKind:
    """Nothing is exact without a catalog version to say which prices those were."""
    if kind is CostKind.EXACT and catalog_version is None:
        return CostKind.ESTIMATED
    return kind


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenUsage:
    """What the provider reported about one LLM call; ``None`` = not reported.

    ``tokens_in`` counts every input token, cached ones included;
    ``tokens_in_cached`` is the cached part of it. ``tokens_out`` counts every
    billed output token, reasoning included.
    """

    tokens_in: int | None
    tokens_in_cached: int | None
    tokens_out: int | None


@dataclass(frozen=True, slots=True)
class _TokenPrices:
    """A model's token prices for one window, multiplier applied."""

    in_per_1k: Decimal | None
    in_cached_per_1k: Decimal | None
    out_per_1k: Decimal | None


def _token_prices(model: ModelConfig | None, window: TariffWindow) -> _TokenPrices:
    if model is None:
        return _TokenPrices(None, None, None)
    return _TokenPrices(
        in_per_1k=_applied(model, model.prices.in_per_1k, window),
        in_cached_per_1k=_applied(model, model.prices.in_cached_per_1k, window),
        out_per_1k=_applied(model, model.prices.out_per_1k, window),
    )


def _token_row(
    cost: Decimal | None,
    kind: CostKind,
    *,
    catalog_version: str | None,
    started_at: datetime,
    window: TariffWindow | None,
    prices: _TokenPrices,
    unit_out_estimate: int | None = None,
) -> PricedCall:
    return PricedCall(
        cost_usd=None if cost is None else _money(cost),
        cost_kind=_settled(kind, catalog_version),
        catalog_version=catalog_version,
        tariff_window=window,
        call_started_at=started_at,
        price_in_per_1k_usd=prices.in_per_1k,
        price_in_cached_per_1k_usd=prices.in_cached_per_1k,
        price_out_per_1k_usd=prices.out_per_1k,
        unit_out_estimate=unit_out_estimate,
    )


def price_token_call(
    model: ModelConfig | None,
    *,
    catalog_version: str | None,
    started_at: datetime,
    usage: TokenUsage | None,
    failure: BaseException | None,
    input_estimate: int | None,
    output_estimate: int | None,
) -> PricedCall:
    """Price one LLM attempt (decisions 2 and 3, P2-P4).

    Args:
        model: The rung's model from the registry; ``None`` if it is not there.
        catalog_version: The registry's catalog version.
        started_at: When the attempt was sent (timezone-aware); picks the
            tariff window and is compared with ``price_effective_from``.
        usage: The provider's report; ``None`` when no response came back.
        failure: The exception the attempt raised, if it raised one.
        input_estimate: Our count of the request's input tokens; ``None`` when
            the request carried attachments our count cannot see.
        output_estimate: Our count of the response's output tokens; ``None``
            without a response.

    The cases, in order:

    * no response, and the failure is one the provider does not bill → exact 0;
    * the model is unknown or has no token price → unknown;
    * no response otherwise → estimated from our input count alone, or unknown
      when the request carried attachments;
    * a response → the provider's counts priced; estimated instead of exact
      when one of its counts is missing (ours stands in, or unknown if we have
      none), when cached tokens have no cached price, before
      ``price_effective_from``, or above ``price_holds_to_input_tokens``.
    """
    window = _window(model, started_at)
    prices = _token_prices(model, window)
    if usage is None and failure is not None and failure_bills_nothing(failure):
        return _token_row(
            Decimal(0),
            CostKind.EXACT,
            catalog_version=catalog_version,
            started_at=started_at,
            window=window,
            prices=prices,
        )

    p_in, p_out = prices.in_per_1k, prices.out_per_1k
    if model is None or p_in is None or p_out is None:
        return _token_row(
            None,
            CostKind.UNKNOWN,
            catalog_version=catalog_version,
            started_at=started_at,
            window=None,
            prices=_TokenPrices(None, None, None),
        )

    def unknown() -> PricedCall:
        return _token_row(
            None,
            CostKind.UNKNOWN,
            catalog_version=catalog_version,
            started_at=started_at,
            window=window,
            prices=prices,
        )

    if usage is None:
        if input_estimate is None:
            return unknown()
        return _token_row(
            input_estimate * p_in / _PER_1K,
            CostKind.ESTIMATED,
            catalog_version=catalog_version,
            started_at=started_at,
            window=window,
            prices=prices,
        )

    approximated = False
    tokens_in = usage.tokens_in
    if tokens_in is None:
        if input_estimate is None:
            return unknown()
        tokens_in, approximated = input_estimate, True
    tokens_out = usage.tokens_out
    used_output_estimate: int | None = None
    if tokens_out is None:
        if output_estimate is None:
            return unknown()
        tokens_out = used_output_estimate = output_estimate
        approximated = True

    cached = min(usage.tokens_in_cached or 0, tokens_in)
    p_cached = prices.in_cached_per_1k
    if cached and p_cached is None:
        approximated = True
    if (
        model.price_effective_from is not None
        and started_at.date() < model.price_effective_from
    ):
        approximated = True
    if (
        model.price_holds_to_input_tokens is not None
        and tokens_in > model.price_holds_to_input_tokens
    ):
        approximated = True

    cost = (
        (tokens_in - cached) * p_in
        + cached * (p_cached if p_cached is not None else p_in)
        + tokens_out * p_out
    ) / _PER_1K
    return _token_row(
        cost,
        CostKind.ESTIMATED if approximated else CostKind.EXACT,
        catalog_version=catalog_version,
        started_at=started_at,
        window=window,
        prices=prices,
        unit_out_estimate=used_output_estimate,
    )


def price_audio_call(
    model: ModelConfig | None,
    *,
    catalog_version: str | None,
    started_at: datetime,
    duration_sec: float | None,
    succeeded: bool,
    failure: BaseException | None,
) -> PricedCall:
    """Price one speech-to-text attempt (decision 6, P9).

    Only the duration known BEFORE the call counts — what a provider reports
    back is not used (ElevenLabs reports the last word's end, not the billed
    length). Billed seconds are the duration rounded up to the model's
    ``billing_granularity_sec``: exact for a successful call with a
    granularity, estimated without one (rounded up to a whole second) or for a
    failure the provider may have billed; unknown without a duration or a
    price; exact 0 for a failure the provider does not bill.
    """
    window = _window(model, started_at)
    per_minute = (
        _applied(model, model.prices.per_minute, window) if model is not None else None
    )
    if not succeeded and failure is not None and failure_bills_nothing(failure):
        return PricedCall(
            cost_usd=_money(Decimal(0)),
            cost_kind=_settled(CostKind.EXACT, catalog_version),
            catalog_version=catalog_version,
            tariff_window=window,
            call_started_at=started_at,
            price_per_minute_usd=per_minute,
        )
    if model is None or per_minute is None or duration_sec is None:
        return PricedCall(
            cost_usd=None,
            cost_kind=CostKind.UNKNOWN,
            catalog_version=catalog_version,
            tariff_window=window if per_minute is not None else None,
            call_started_at=started_at,
            price_per_minute_usd=per_minute,
        )

    granularity = model.billing_granularity_sec
    step = granularity if granularity is not None else 1
    billed = math.ceil(duration_sec / step) * step
    kind = (
        CostKind.EXACT if succeeded and granularity is not None else CostKind.ESTIMATED
    )
    return PricedCall(
        cost_usd=_money(billed * per_minute / _SECONDS_PER_MINUTE),
        cost_kind=_settled(kind, catalog_version),
        catalog_version=catalog_version,
        tariff_window=window,
        call_started_at=started_at,
        price_per_minute_usd=per_minute,
        billed_seconds=billed,
    )
