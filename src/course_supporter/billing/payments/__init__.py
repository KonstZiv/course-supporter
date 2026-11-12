"""Payments (BIL-PAY): the port of payment providers and what billing does with it.

Purpose:
    The platform sells prepaid packages of units through merchants of record —
    several side by side (Paddle, Creem, others), each behind an adapter of
    its own — and, for B2B, by a contract invoice. Everything here is the
    same for every provider; an adapter knows one provider's API and nothing
    of the journal. The journal itself (``billing.ledger``,
    ``billing.refunds``) is called, never rewritten.

Modules:
    :mod:`.port` — what billing asks of a provider (:class:`.port.PaymentProvider`:
    open a checkout, read a webhook, request a refund) and the normalized
    events a webhook becomes.
    :mod:`.catalog` — the packages on sale, ``config/billing_packages.yaml``.
    :mod:`.registry` — the adapters by name; the providers switched on
    (``BILLING_PROVIDERS``, none by default: payment is unavailable).
    :mod:`.fake` — :class:`.fake.FakeProvider`, for tests and local
    development only; never registered in production.
    :mod:`.checkout` — a payer opens a checkout of a package (a student from
    the portal, an author with the tenant's key).
    :mod:`.webhooks` — a provider's event: one transaction, idempotent by the
    provider's event id, the journal's action, a row of
    ``billing_provider_events``.
    :mod:`.invoice` — a contract invoice paid (B2B), credited by the
    administrator through ``scripts/billing_admin.py credit-invoice``.

Transactions:
    The checkout and the invoice flush and never commit, like the rest of
    billing: the caller owns the transaction. The webhooks own theirs — one
    per event — because a failed event is recorded after its action is rolled
    back (:mod:`.webhooks`).
"""
