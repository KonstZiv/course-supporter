"""№21 file-role proposal — deterministic, no DB, no LLM.

Covers the role map (decision 8, the "as today" part), the exclusion of
sanitization-removed files (decision 4), and the path-only tree_digest (I2).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from course_supporter.ingestion.file_roles import (
    ROLE_FULL,
    ROLE_STRUCTURE_ONLY,
    build_role_proposal,
    compute_tree_digest,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)

_FIXED = datetime(2026, 7, 22, 12, 0, 0, tzinfo=UTC)


def _doc(
    *,
    chunks: Sequence[tuple[str, int]] = (),
    description_only: Sequence[tuple[str, int, str, str]] = (),
    excluded: Sequence[dict[str, object]] = (),
) -> SourceDocument:
    """A CODE SourceDocument mirroring CodeProcessor.process_raw's output shape."""
    return SourceDocument(
        source_type=SourceType.CODE,
        source_url="archive.zip",
        chunks=[
            ContentChunk(
                chunk_type=ChunkType.CODE_FILE,
                text="x",
                index=i,
                metadata={"file_path": path, "size_bytes": size},
            )
            for i, (path, size) in enumerate(chunks)
        ],
        metadata={
            "description_only_entries": [
                {"path": p, "size": s, "reason": r, "disposition": d}
                for (p, s, r, d) in description_only
            ],
            "excluded_entries": list(excluded),
        },
    )


def test_role_map_custom_full_typical_structure_only() -> None:
    """custom source → full; typical/oversize (description-only) → structure_only."""
    doc = _doc(
        chunks=[("src/app.ts", 200), ("src/util.ts", 50)],
        description_only=[("package-lock.json", 9000, "lockfile", "typical")],
    )
    proposal = build_role_proposal(doc, computed_at=_FIXED)
    files = proposal["files"]
    assert files["src/app.ts"]["role"] == ROLE_FULL
    assert files["src/util.ts"]["role"] == ROLE_FULL
    assert files["package-lock.json"]["role"] == ROLE_STRUCTURE_ONLY
    assert files["package-lock.json"]["reason"] == "lockfile"
    assert proposal["computed_at"] == _FIXED.isoformat()
    assert set(files) == {"src/app.ts", "src/util.ts", "package-lock.json"}


def test_removed_files_absent_from_proposal() -> None:
    """decision 4: sanitization-removed files are outside the proposal (author
    cannot restore them)."""
    doc = _doc(
        chunks=[("src/app.ts", 200)],
        excluded=[{"path": ".env", "size": 10, "reason": "forbidden_type"}],
    )
    proposal = build_role_proposal(doc, computed_at=_FIXED)
    assert ".env" not in proposal["files"]
    assert set(proposal["files"]) == {"src/app.ts"}


def test_tree_digest_is_path_only_and_order_independent() -> None:
    """Same path SET (different order + different sizes) → same digest (I2)."""
    a = _doc(
        chunks=[("src/app.ts", 1), ("src/b.ts", 2)],
        description_only=[("lock", 3, "lockfile", "typical")],
    )
    b = _doc(
        chunks=[("src/b.ts", 999), ("src/app.ts", 999)],
        description_only=[("lock", 999, "lockfile", "typical")],
    )
    assert compute_tree_digest(a) == compute_tree_digest(b)


def test_tree_digest_changes_when_path_set_changes() -> None:
    """A file added/removed changes the digest — the staleness signal (I2)."""
    a = _doc(chunks=[("src/app.ts", 1)])
    b = _doc(chunks=[("src/app.ts", 1), ("src/new.ts", 1)])
    assert compute_tree_digest(a) != compute_tree_digest(b)


def test_proposal_digest_equals_standalone_digest() -> None:
    """The proposal's tree_digest is exactly what the shared function computes —
    the same value the processing precondition guard (BE8) will compare."""
    doc = _doc(
        chunks=[("src/app.ts", 1)],
        description_only=[("lock", 2, "lockfile", "typical")],
    )
    proposal = build_role_proposal(doc, computed_at=_FIXED)
    assert proposal["tree_digest"] == compute_tree_digest(doc)
