"""Versioned review structure — the anchor of the new Mentor path (mentor-rebuild 01).

Purpose:
    Every review of the new form states which shape it has, so any later
    reader — the assembler, the portal, a reproducibility check, a migration
    of old reviews — can tell shapes apart without guessing from content.
    The version lives INSIDE the review structure (not in a column of the
    submissions table): it describes the review's form and travels with it.

Interface:
    :class:`VersionedReview` — the base every new-form review model extends.
    Its one field, ``schema_version``, is required and has no default, so a
    new-form review cannot be assembled without naming its version: the
    assembler states it, the model refuses to exist otherwise.

    :func:`review_schema_version` — reads the version of any stored review
    mapping (a ``review_result`` JSONB, for instance). Reviews written before
    the rebuild have no key; they read as :data:`PRE_REBUILD_SCHEMA`, which
    needs no migration of existing rows.

Extending:
    Task 04 subclasses :class:`VersionedReview` with the review body. A change
    to the body's shape that a reader must know about is a new version: add
    the value to :data:`ReviewSchemaVersion`, point
    :data:`REVIEW_SCHEMA_VERSION` at it, and let readers dispatch on
    :func:`review_schema_version`.

>>> VersionedReview(schema_version=REVIEW_SCHEMA_VERSION).schema_version
'1'
>>> review_schema_version({"verdict": {"passed": True}})
'pre-rebuild'
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

ReviewSchemaVersion = Literal["1"]
"""Every review-structure version this code can assemble."""

REVIEW_SCHEMA_VERSION: Final[ReviewSchemaVersion] = "1"
"""The version new-form reviews are assembled with."""

PRE_REBUILD_SCHEMA: Final = "pre-rebuild"
"""What a stored review without ``schema_version`` reads as — today's Mentor."""

_VERSION_KEY: Final = "schema_version"


class VersionedReview(BaseModel):
    """Base of every new-form review structure.

    ``schema_version`` is required with no default on purpose: a default
    would let a review be built without anyone having decided its version.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: ReviewSchemaVersion


def review_schema_version(review: Mapping[str, object]) -> str:
    """The schema version of a stored review mapping.

    Args:
        review: A review structure as stored (e.g. ``review_result`` JSONB).

    Returns:
        The stored ``schema_version``, or :data:`PRE_REBUILD_SCHEMA` when the
        key is absent — a review written before the rebuild.

    Raises:
        ValueError: The key is present but not a string. That is neither a
            new-form review nor a pre-rebuild one, and reading it as either
            would hide a corrupted record.
    """
    if _VERSION_KEY not in review:
        return PRE_REBUILD_SCHEMA
    version = review[_VERSION_KEY]
    if not isinstance(version, str):
        msg = f"schema_version must be a string, got {type(version).__name__}"
        raise ValueError(msg)
    return version
