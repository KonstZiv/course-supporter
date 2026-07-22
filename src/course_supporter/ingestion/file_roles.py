"""№21 file-role proposal — deterministic mapping of a CODE extraction partition
to author-facing file roles (KD20 proposal).

Zero LLM: the proposal is a pure function of what ``CodeProcessor.process_raw``
already computed (the typicality partition), so the DOCUMENT_PREPARATION job and
the expensive processing job see the SAME file set. ``compute_tree_digest`` (I2)
is the one hash both sides compare to detect a stale author decision — one
function, used by prep here and by the processing precondition guard (BE8).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from course_supporter.models.source import SourceDocument
from course_supporter.normalizer.hashing import canonicalize_path

# Role tokens (decision 2) — a separate axis from the mechanical ``cls``.
ROLE_FULL = "full"
ROLE_AUXILIARY = "auxiliary"
ROLE_STRUCTURE_ONLY = "structure_only"

# Reason for a full-role file: it passed typicality as segment-worthy custom
# source (no exclusion signal). structure_only files carry their typicality
# reason verbatim (build-config files → the BUILD_CONFIG token, BE6/decision 9).
# ``auxiliary`` is never auto-proposed — the author assigns it by hand
# (decision 8).
_REASON_CUSTOM_SOURCE = "custom_source"


def default_role(*, is_custom: bool) -> str:
    """The role the deterministic default assigns absent an author decision.

    Segment-worthy custom code → ``full``; everything else (typical / oversize /
    build-config) → ``structure_only``. ``auxiliary`` is NEVER a default — the
    author assigns it by hand (decision 8). This is the single source both the
    prep proposal and the expensive processor's fallback branch use, so "no
    decision" behaves identically on both sides (point 6). ``build_role_proposal``
    is consistent by construction: its chunks are custom → full, its
    description-only files are non-custom → structure_only.
    """
    return ROLE_FULL if is_custom else ROLE_STRUCTURE_ONLY


def decision_roles(file_roles: Mapping[str, Any] | None) -> dict[str, str]:
    """The author's per-file role decision (``{path: role}``), or ``{}``.

    Empty when there is no ``file_roles``, no ``decision`` yet, or the decision
    carries no files — the caller then falls back to :func:`default_role`.
    """
    if not file_roles:
        return {}
    decision = file_roles.get("decision")
    if not decision:
        return {}
    files = decision.get("files")
    return dict(files) if files else {}


def _included_paths(doc: SourceDocument) -> list[str]:
    """Canonical paths of every structurally-INCLUDED file (post-sanitization).

    The union of the segment-worthy custom files (``chunks``) and the
    description-only files (typical / oversize). Excluded / denylist entries are
    absent by construction: the author cannot restore what sanitization removed
    (decision 4), so they lie outside both the digest and the proposal.
    """
    paths = [str(c.metadata["file_path"]) for c in doc.chunks]
    paths += [str(e["path"]) for e in doc.metadata.get("description_only_entries", [])]
    return [canonicalize_path(p) for p in paths]


def compute_tree_digest(doc: SourceDocument) -> str:
    """Deterministic SHA-256 over the sorted canonical INCLUDED path list (I2).

    Path-only (not content): the digest answers "is this the same file tree the
    author marked up?", a property of the path set, not the bytes. One function
    for both the prep job and the expensive-processing precondition guard, so a
    stale decision is caught before any token is spent.
    """
    blob = "\n".join(sorted(_included_paths(doc)))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_role_proposal(
    doc: SourceDocument, *, computed_at: datetime
) -> dict[str, Any]:
    """Derive the ``proposal`` block from a CODE SourceDocument's partition.

    Default role map (decision 8): custom source → ``full``; every
    description-only file (typical / oversize / build-config) → ``structure_only``
    with its typicality reason. Build-config now reaches the description-only side
    via the ``assess`` BUILD_CONFIG detector, so it is proposed structure_only
    like any other non-custom file.

    ``computed_at`` is injected (not read from a clock here) so the proposal is a
    pure function of its inputs — the digest is deterministic and the timestamp
    is the caller's to own.

    The proposal also carries ``removed: {count: N}`` (decision 4 / decision 16,
    a KD20 field-extension) — how many files sanitization removed (safety +
    service-dir denylist). Count only: the author cannot restore them, and the
    confirm screen shows an explanatory line when it is > 0.
    """
    files: dict[str, dict[str, str]] = {}
    for chunk in doc.chunks:
        path = canonicalize_path(str(chunk.metadata["file_path"]))
        files[path] = {"role": ROLE_FULL, "reason": _REASON_CUSTOM_SOURCE}
    for entry in doc.metadata.get("description_only_entries", []):
        path = canonicalize_path(str(entry["path"]))
        files[path] = {
            "role": ROLE_STRUCTURE_ONLY,
            "reason": str(entry.get("reason") or entry.get("disposition") or "typical"),
        }
    # Excluded rows are collapsed (a denylist dir is one row standing for its
    # file count), so the true removed-file count sums each row's ``entries``.
    removed_count = sum(
        int(e.get("entries", 1)) for e in doc.metadata.get("excluded_entries", [])
    )
    return {
        "files": files,
        "tree_digest": compute_tree_digest(doc),
        "computed_at": computed_at.isoformat(),
        "removed": {"count": removed_count},
    }
