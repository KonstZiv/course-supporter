"""The campaign tag of a join link (Б-4): ``/<tenant>/join/<key>?src=<tag>``.

The contract the portal and the site rely on (changed only with the billing
session): a tag is 1-40 characters of ``a-z``, ``0-9``, ``-`` and ``_``. The
portal passes it as ``source_tag`` in the body of a signup or of a join. A
value that breaks the rule does NOT refuse the enrollment: it is dropped
(``None``) with an INFO line ``signup_source_tag_ignored`` that never carries
the value. A tag is a campaign label, not personal data.
"""

from __future__ import annotations

import re

import structlog

SOURCE_TAG_PATTERN = r"^[a-z0-9_-]{1,40}$"
"""The rule, also the database CHECK on ``portal_signups`` and
``enrollment_events``."""

_SOURCE_TAG_RE = re.compile(SOURCE_TAG_PATTERN)

logger = structlog.get_logger(__name__)


def parse_source_tag(raw: object) -> str | None:
    """The tag when ``raw`` keeps the rule; else ``None`` (logged if given)."""
    if raw is None:
        return None
    if isinstance(raw, str) and _SOURCE_TAG_RE.fullmatch(raw):
        return raw
    logger.info("signup_source_tag_ignored")
    return None
