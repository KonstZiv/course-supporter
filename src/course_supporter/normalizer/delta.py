"""Deterministic base-vs-submission delta (KD18 P1).

``compute_delta`` compares two manifests by canonical path over their
INCLUDED entries. Pure function, zero I/O. Manifests produced by
``normalize_archive`` have unique INCLUDED paths (the duplicate-path
guard lives in the orchestrator), so the path -> hash maps never
collide here.
"""

from __future__ import annotations

from course_supporter.normalizer.models import Delta, Manifest


def compute_delta(base: Manifest, sub: Manifest) -> Delta:
    """Diff ``sub`` against ``base`` at the path level.

    * ``changed`` -- path present in both, raw hash differs. This is a
      pure hash inequality; a class transition is not surfaced
      separately (with the extension-based classifier both sides of a
      changed pair share the class, since class is a function of the
      path's extension; a consumer that needs class detail reads both
      manifests).
    * ``new`` -- path only in the submission.
    * ``deleted`` -- path only in the base.
    * ``hygiene_new_excluded`` -- excluded path present in the
      submission but not the base (a forgotten ``.venv`` slipping in, or
      a file that flipped to magic-mismatch). Orthogonal to ``deleted``:
      a file that moved INCLUDED -> excluded appears in both, and is not
      deduplicated across the two.

    An empty ``base`` (no INCLUDED entries) yields every submission path
    as ``new`` (KD18: base absent -> delta is "all new"). All tuples are
    sorted for determinism.
    """
    base_hashes = {entry.path: entry.hash for entry in base.included}
    sub_hashes = {entry.path: entry.hash for entry in sub.included}

    changed = tuple(
        sorted(
            path
            for path, digest in sub_hashes.items()
            if path in base_hashes and base_hashes[path] != digest
        )
    )
    new = tuple(sorted(path for path in sub_hashes if path not in base_hashes))
    deleted = tuple(sorted(path for path in base_hashes if path not in sub_hashes))

    base_excluded = {entry.path for entry in base.excluded}
    sub_excluded = {entry.path for entry in sub.excluded}
    hygiene_new_excluded = tuple(sorted(sub_excluded - base_excluded))

    return Delta(
        changed=changed,
        new=new,
        deleted=deleted,
        hygiene_new_excluded=hygiene_new_excluded,
    )
