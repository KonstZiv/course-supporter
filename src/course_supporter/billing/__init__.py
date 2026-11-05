"""Billing (BIL-A): who pays, under which rules, with whose consent.

Purpose:
    The platform sells prepaid packages of units. A student pays for the
    reviews of their own homework; an author (the tenant) pays for processing
    the materials of their courses and preparing tasks for assessment. This
    package holds the accounts that pay and everything that decides who pays —
    not the units themselves: the journal of unit movements is BIL-B, and
    charging an action is BIL-E. Nothing here changes how a review or a
    processing job runs.

Modules:
    :mod:`.accounts` — the account of a student or of an author, created
    lazily; its anonymization once the owner is deleted, which also ends the
    payment rules about that owner.
    :mod:`.rules` — the one path that ends a payment rule.
    :mod:`.payer` — THE one place that decides who pays for an action.
    :mod:`.consent` — consent to the billing terms in force.
    :mod:`.admin` — the administrator's operations: read an account, author
    settings, payment rules. Every change writes an audit entry
    (:mod:`.audit`). Today the only entry point is ``scripts/billing_admin.py``;
    administrator routes, when they come, call the same functions.

Storage:
    ``billing_*`` tables (``storage.orm.Billing*``). None has a foreign key to a
    table outside billing, so deleting a tenant or a student never reaches a
    billing row and never fails on one.

Transactions:
    The functions flush and never commit: the caller (a route, the script)
    owns the transaction, so a change and its audit entry land together.
"""
