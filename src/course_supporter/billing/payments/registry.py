"""The adapters of payment providers by name, and the ones switched on (BIL-PAY).

Two lists:

* the adapters there are (:func:`adapters`) — one per provider the code
  can speak to: :class:`~.creem.CreemProvider` everywhere (it needs
  ``CREEM_API_KEY`` and ``CREEM_WEBHOOK_SECRET`` once switched on, or the
  startup stops); :class:`~.fake.FakeProvider` only in development and
  testing: its secret is in the code, so production and staging never have
  it (a test locks it). Another provider's adapter joins this list.
* the providers switched on — ``BILLING_PROVIDERS``, in order of preference,
  none by default. With none, payment is unavailable: a checkout answers 503
  and no webhook address answers at all (404).

:func:`build_registry` turns the setting into a :class:`ProviderRegistry`; a
name no adapter answers to is a :class:`PaymentConfigError`, and the API
refuses to start on it (``api/app.py``), as on any other broken setting.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from functools import lru_cache
from typing import TYPE_CHECKING, Final

from course_supporter.billing.journal import PROVIDER_NAME
from course_supporter.billing.payments.creem import CREEM, CreemProvider
from course_supporter.billing.payments.fake import FAKE, FakeProvider
from course_supporter.billing.payments.invoice import INVOICE_PROVIDER
from course_supporter.billing.payments.port import PaymentProvider
from course_supporter.config import Environment, get_settings

if TYPE_CHECKING:
    from course_supporter.config import Settings

Factory = Callable[["Settings"], PaymentProvider]
"""Builds an adapter from the settings (its keys, its endpoints)."""

FAKE_ENVIRONMENTS: Final = frozenset({Environment.DEVELOPMENT, Environment.TESTING})
"""Where :class:`~.fake.FakeProvider` exists."""


class PaymentConfigError(ValueError):
    """``BILLING_PROVIDERS`` names a provider this process has no adapter for."""


def _fake(_settings: Settings) -> PaymentProvider:
    return FakeProvider()


def _creem(settings: Settings) -> PaymentProvider:
    """Creem with its key and secret; without either, a broken setting."""
    key, secret = settings.creem_api_key, settings.creem_webhook_secret
    missing = [
        name
        for name, value in (("CREEM_API_KEY", key), ("CREEM_WEBHOOK_SECRET", secret))
        if value is None or not value.get_secret_value().strip()
    ]
    if missing or key is None or secret is None:
        msg = f"BILLING_PROVIDERS names {CREEM}, but {', '.join(missing)} is not set"
        raise PaymentConfigError(msg)
    return CreemProvider(
        api_key=key.get_secret_value().strip(),
        webhook_secret=secret.get_secret_value().strip(),
        api_base=settings.creem_api_base,
    )


def adapters(environment: Environment) -> dict[str, Factory]:
    """Every adapter there is in ``environment``, by name."""
    found: dict[str, Factory] = {CREEM: _creem}
    if environment in FAKE_ENVIRONMENTS:
        found[FAKE] = _fake
    return found


class ProviderRegistry:
    """The providers switched on, by name, in order of preference."""

    def __init__(self, providers: Iterable[PaymentProvider] = ()) -> None:
        self._providers: dict[str, PaymentProvider] = {}
        for provider in providers:
            name = provider.name
            if PROVIDER_NAME.fullmatch(name) is None or name == INVOICE_PROVIDER:
                msg = f"{name!r} cannot name a payment provider"
                raise PaymentConfigError(msg)
            if name in self._providers:
                msg = f"payment provider {name!r} is switched on twice"
                raise PaymentConfigError(msg)
            self._providers[name] = provider

    def __len__(self) -> int:
        return len(self._providers)

    @property
    def names(self) -> tuple[str, ...]:
        """The names, the default first."""
        return tuple(self._providers)

    def get(self, name: str) -> PaymentProvider | None:
        """The provider of that name, when it is switched on."""
        return self._providers.get(name)

    def default(self) -> PaymentProvider | None:
        """The first provider switched on; None when there is none."""
        return next(iter(self._providers.values()), None)


def build_registry(settings: Settings) -> ProviderRegistry:
    """The providers ``settings.billing_providers`` switches on.

    Raises:
        PaymentConfigError: a name without an adapter in this environment,
            the same name twice, or an adapter without its keys.
    """
    known = adapters(settings.environment)
    missing = [name for name in settings.billing_providers if name not in known]
    if missing:
        msg = (
            f"BILLING_PROVIDERS names {missing}, but {settings.environment.value} "
            f"has adapters for {sorted(known) or 'no provider'}"
        )
        raise PaymentConfigError(msg)
    return ProviderRegistry(
        known[name](settings) for name in settings.billing_providers
    )


@lru_cache(maxsize=1)
def payment_registry() -> ProviderRegistry:
    """The providers of this process, built once from the settings."""
    return build_registry(get_settings())
