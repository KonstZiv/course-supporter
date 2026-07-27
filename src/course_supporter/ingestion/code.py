"""Code processor: single source file OR .zip project archive (R1).

task-code-materials: a first-class ``SourceType.CODE`` ingestion path
producing the standard ``DocumentSummaryDraft + DocumentSegmentDraft``
contract with DETERMINISTIC segmentation — one included file = one
segment; offsets are computed in code over ``assemble_text()`` and
never LLM-emitted (ratified F1). Description-only files never enter
the reference text (F2 — FULL-COVER holds trivially over included
files); they live in the S3 archive + ``DocumentSummary.structure``.

Generation inherits the existing two-step pattern (ratified):

* per included file — a skeleton via the R4 extraction ladder
  (AST → regex → LLM ``code_skeleton_extraction`` → deterministic
  minimum; README/small configs verbatim) feeding one CHEAP
  ``code_segment_description`` call (bounded concurrency, mirror of
  the presentation Pass 1 semaphore);
* per document — ONE large-window ``code_summary`` call whose input is
  the project structure + the ready per-file descriptions (never the
  skeletons). No batching (ratified: do not deviate from the pattern).

Segment title = file name (deterministic, truncated to the ORM 128
cap); segment concepts = deterministic namespace core verified /
supplemented by the describe call; doc-level concepts = KD-2.1-O
union+dedup over segments. ``file_path`` (fourth anchor kind) is set
per segment in Pass 2b.

Inclusion = ``extract_archive_safely(classify=True)`` INCLUDED
verdicts (extension whitelist + magic) refined by the typicality
filter (:mod:`course_supporter.ingestion.code_typicality`, F3): the
KD18 dir-denylist layer + the file layer (lockfiles / minified /
vendored / build-config) + the 4 MiB ``kept_single`` sanitary cap (F7).
Non-custom files default to description-only — never rejected (R5).

№21: the author's file-role DECISION (or, absent one, the deterministic
default) — not raw typicality — makes the final segment / description-only
call. A full/auxiliary role is a segment; structure_only is description-only.
Role also routes concepts (auxiliary → secondary only, decision 6) and rides
each structure entry + each segment (decision 2 / decision 5).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError

from course_supporter.ingestion.base import (
    CategorisedProcessingError,
    MaterialProcessor,
    ProcessingError,
    UnsupportedFormatError,
)
from course_supporter.ingestion.code_skeleton import (
    ast_skeleton,
    is_verbatim_file,
    minimum_skeleton,
    namespace_core,
    regex_skeleton,
    render_llm_skeleton,
    verbatim_skeleton,
)
from course_supporter.ingestion.code_structure import (
    CodeStructureReason,
    denylist_token,
    reason_for_verdict,
    render_structure_block,
    structure_reason,
)
from course_supporter.ingestion.code_typicality import assess
from course_supporter.ingestion.file_roles import (
    ROLE_AUXILIARY,
    ROLE_FULL,
    ROLE_STRUCTURE_ONLY,
    decision_roles,
    default_role,
)
from course_supporter.ingestion.schemas import (
    CodeSegmentDescription,
    CodeSkeletonResult,
    CodeSummaryResult,
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.language import display_name
from course_supporter.llm.error_categories import (
    LadderExhaustedError,
    StructuralRetryError,
)
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
    SourceType,
)
from course_supporter.normalizer.classify import (
    collapse_denylist,
    denylist_prefix,
)
from course_supporter.normalizer.hashing import canonicalize_path
from course_supporter.security.archive import EntryVerdict, extract_archive_safely
from course_supporter.security.exceptions import ErrorCategory
from course_supporter.security.file_type import (
    extension_of,
    verify_extension_matches_content,
)
from course_supporter.security.policies import AUTHORED_POLICY

if TYPE_CHECKING:
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.storage.orm import AuthoredDocument

logger = structlog.get_logger()

# Per-file LLM concurrency bound — mirror of the presentation Pass 1
# semaphore (_PASS_1_CONCURRENCY = 6, KD-2.3-F fail-fast precedent).
_PER_FILE_CONCURRENCY = 6

# File header prefix inside the reference text (F1: path-header + body).
# Load-bearing offset arithmetic: assemble_text joins chunk texts with
# "\n\n", each chunk text = _file_header(path) + body.
_HEADER_PREFIX = "===== FILE: "
_HEADER_SUFFIX = " =====\n"

# ORM DocumentSegment.title is String(128) — ratified: plain truncate,
# the full path lives in the ``file_path`` anchor.
_TITLE_MAX = 128


def _file_header(path: str) -> str:
    return f"{_HEADER_PREFIX}{path}{_HEADER_SUFFIX}"


def _excluded_row(
    path: str,
    size: int,
    reason: CodeStructureReason,
    detail: str | None = None,
    *,
    entries: int = 1,
) -> dict[str, Any]:
    """Build one excluded ``structure`` row — typed on ``CodeStructureReason``.

    This is where №19's no-raw-passthrough invariant lives: ``reason`` is
    the ENUM, never a ``str``, so ``entry.verdict.value`` fails ``mypy``
    at the call site rather than leaking into the prompt again. ``entries``
    is the count of raw files the row stands for (``1`` per file; the
    collapsed count for a denylist directory).
    """
    return {
        "path": path,
        "size": size,
        "reason": structure_reason(reason, detail),
        "entries": entries,
    }


def _strip_fences(content: str) -> str:
    """Markdown-fence tolerance (KD-2.3-T Path 3, presentation precedent)."""
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else ""
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


class CodeProcessor(MaterialProcessor):
    """Process code materials: one source file or a .zip project (R1).

    Per-run extraction byproducts (``_excluded``) live on the instance
    between ``process_raw`` and ``process_macro`` — the same pattern as
    ``PresentationProcessor.rendered_slides`` consumed by the
    orchestrator; the ARQ worker runs one job at a time (max_jobs=1).
    """

    _excluded: list[dict[str, Any]]
    _description_only: list[dict[str, Any]]

    async def process_raw(
        self,
        source: AuthoredDocument,
    ) -> SourceDocument:
        if source.source_type != SourceType.CODE:
            raise UnsupportedFormatError(
                f"CodeProcessor expects 'code', got '{source.source_type}'"
            )

        path = Path(source.source_url)
        self._excluded = []
        members = self._load_members(source, path)

        if not members:
            raise CategorisedProcessingError(
                ErrorCategory.EMPTY_DOCUMENT,
                "Code material contains no includable source files "
                "(all archive entries were excluded)",
            )

        # Typicality partition (F2/F3/F7): custom files become verbatim
        # segments; typical/library files and files over the 4 MiB
        # sanitary cap go DESCRIPTION-ONLY — fully outside the reference
        # text, recorded in DocumentSummary.structure with the reason and
        # logged loudly per file (the material is never rejected, R5).
        # One policy for a single file and a project alike (a single file
        # is the degenerate one-member project).
        # №21 (point 6): the ROLE — not raw typicality — decides segment-
        # worthiness. The author's decision (keyed on canonical paths) wins;
        # absent a decision the default is identical to ``build_role_proposal``
        # (custom → full, else structure_only). ``auxiliary`` arrives ONLY from a
        # decision. A promoted config becomes a segment; a demoted custom file
        # goes description-only. ``role_map`` (raw path → role) is threaded to
        # ``process_macro`` via the SourceDocument metadata.
        roles = decision_roles(source.file_roles)
        role_map: dict[str, str] = {}
        description_only: list[dict[str, Any]] = []
        chunks: list[ContentChunk] = []
        for member_path, body in members:
            verdict = assess(member_path, len(body))
            role = roles.get(canonicalize_path(member_path)) or default_role(
                is_custom=verdict.is_custom
            )
            role_map[member_path] = role
            if role == ROLE_STRUCTURE_ONLY:
                # An intrinsically non-custom file carries its OWN typicality
                # reason; a custom file the author demoted has none, so it takes
                # the AUTHOR_STRUCTURE_ONLY token instead.
                reason = verdict.reason or structure_reason(
                    CodeStructureReason.AUTHOR_STRUCTURE_ONLY
                )
                description_only.append(
                    {
                        "path": member_path,
                        "size": len(body),
                        "reason": reason,
                        "disposition": verdict.disposition,
                    }
                )
                logger.warning(
                    "code_file_description_only",
                    file_path=member_path,
                    size_bytes=len(body),
                    disposition=verdict.disposition,
                    reason=reason,
                    role=role,
                )
                continue
            text = body.decode("utf-8", errors="replace")
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.CODE_FILE,
                    text=_file_header(member_path) + text,
                    index=len(chunks),
                    metadata={
                        "file_path": member_path,
                        "size_bytes": len(body),
                    },
                )
            )
        self._description_only = description_only

        logger.info(
            "code_processing_done",
            source_url=source.source_url,
            chunk_count=len(chunks),
            description_only_count=len(description_only),
            # Row count, not raw file count (№19: denylist junk collapses to
            # one row per dir/leaf) — the live-gate reads this drop directly;
            # the true excluded file count is the sum of each row's ``entries``.
            excluded_count=len(self._excluded),
        )

        return SourceDocument(
            source_type=SourceType.CODE,
            source_url=source.source_url,
            title=source.filename or path.stem,
            chunks=chunks,
            metadata={
                "excluded_entries": self._excluded,
                "description_only_entries": description_only,
                # №21: raw path → role, consumed by process_macro (decision 6
                # concept routing + decision 5 segment flag) and _build_structure
                # (decision 2 tree role).
                "file_roles": role_map,
            },
        )

    def _load_members(
        self, source: AuthoredDocument, path: Path
    ) -> list[tuple[str, bytes]]:
        """Read the upload (sync helper — mirror of the text.py pattern).

        A single file is the degenerate one-member project (R1); a .zip
        goes through the classify-mode member loop.
        """
        raw = path.read_bytes()
        if extension_of(path.name) == "zip":
            return self._extract_zip(raw)
        name = source.filename or path.name
        if raw:
            # №17: the solo path enforces the SAME textual invariant as
            # the in-archive classify path (verify_extension_matches_content
            # runs inside extract_archive_safely). A binary mislabeled with
            # a code extension must not be segmented verbatim into the
            # reference text — a non-text solo file raises MAGIC_MISMATCH,
            # handled by the ingestion security-reject branch. The ``if
            # raw`` guard mirrors the classify path, which short-circuits
            # empty content to INCLUDED (archive.py, before verify is ever
            # called) rather than rejecting it — so empty files are treated
            # identically on both paths. Calling verify unconditionally
            # would REJECT empty solo files and diverge from the archive.
            verify_extension_matches_content(name, raw)
        return [(name, raw)]

    def _extract_zip(self, raw: bytes) -> list[tuple[str, bytes]]:
        """Classify-mode member loop (KD18 machinery, reused as-is).

        INCLUDED verdicts become segments; every other verdict is
        recorded for ``DocumentSummary.structure`` through the code
        contour's own reason vocabulary (:mod:`code_structure`, №19) —
        never the raw ``EntryVerdict`` value. Denylist junk is skipped
        BEFORE resource accounting via the canonical KD18 matcher (№14:
        never unpacked, never counted in depth/size/ratio) and is then
        COLLAPSED to one row per directory (or leaf) exactly like the
        normalizer, so hundreds of ``__MACOSX/._*`` twins do not flood
        the tree. Structural anti-bomb guards still raise
        (``SecurityRejectedError`` → generic failure path).
        """
        included: list[tuple[str, bytes]] = []
        excluded: list[dict[str, Any]] = []
        denied: list[tuple[str, int]] = []
        unzipped = AUTHORED_POLICY.max_archive_unzipped_bytes
        depth = AUTHORED_POLICY.max_archive_nesting_depth
        if unzipped is None or depth is None:  # pragma: no cover - policy invariant
            raise ProcessingError("authored archive caps are not configured")
        for entry in extract_archive_safely(
            raw,
            archive_kind="zip",
            max_unzipped_size=unzipped,
            max_nesting_depth=depth,
            allowed_extensions=AUTHORED_POLICY.allowed_extensions,
            classify=True,
            skip_matcher=denylist_prefix,
        ):
            if entry.verdict is EntryVerdict.INCLUDED:
                included.append((entry.arcname, entry.content))
            elif entry.verdict is EntryVerdict.DENYLIST_SKIP:
                # Collected for a post-loop collapse (mirror of the
                # normalizer's collapse_denylist). declared_size: the
                # entry was never read, the header value is the only one.
                denied.append((entry.arcname, entry.declared_size))
            else:
                # Explicit translation of the foreign EntryVerdict into
                # the code contour's own token — never the raw .value
                # (that was №19: forbidden_type leaked into the prompt and
                # told the model ".gitignore is forbidden"). Total map;
                # a new reachable verdict raises rather than leaking.
                excluded.append(
                    _excluded_row(
                        entry.arcname,
                        entry.declared_size,
                        reason_for_verdict(entry.verdict),
                    )
                )
        # Collapse denylist junk like the normalizer: one row per denied
        # prefix (dir) or leaf (file), with an entries count. collapse_denylist
        # returns the normalizer's ExcludedEntry whose ``.reason`` is that
        # layer's INTERNAL TRANSPORT enum (ExcludedReason.DENYLIST_DIR) —
        # do NOT propagate it. We derive our OWN dir/file token from the
        # prefix shape; putting the foreign enum straight into structure is
        # exactly the №19 bug being fixed here (do not "simplify" to row.reason).
        for row in collapse_denylist(denied):
            excluded.append(
                _excluded_row(
                    row.path,
                    row.size,
                    denylist_token(row.path),
                    entries=row.entries,
                )
            )
        self._excluded = excluded
        return included

    # ── Pass 2a analog: two-step generation over deterministic segments ──

    async def process_macro(
        self,
        doc: SourceDocument,
        router: StageRouter,
    ) -> DocumentSummaryDraft:
        """Two-step generation (ratified): N cheap per-file calls + ONE summary.

        Segmentation itself is deterministic — offsets are computed
        here from the chunk layout of :meth:`SourceDocument.assemble_text`
        and validated through the standard FULL-COVER Pydantic gates
        (self-check of our own arithmetic, same invariant surface the
        LLM-emitting processors go through).
        """
        reference = doc.assemble_text()
        chunks = [c for c in doc.chunks if c.text]
        if not chunks:
            if not doc.metadata.get("description_only_entries"):
                raise CategorisedProcessingError(
                    ErrorCategory.EMPTY_DOCUMENT,
                    "Cannot run Pass 2a on empty document (no content chunks)",
                )
            # All files went description-only (e.g. one oversize file, R5:
            # the material is NOT rejected): a zero-segment draft is a
            # valid FULL-COVER state; the summary speaks from structure.
            structure = self._build_structure(doc, [])
            summary = await self._summarise(doc, [], [], structure, router)
            draft = DocumentSummaryDraft.model_validate(
                {
                    "title": (summary.title or doc.title)[:_TITLE_MAX],
                    "description": summary.description,
                    "main_concepts": [],
                    "secondary_concepts": [],
                    "segments": [],
                }
            )
            draft.structure = structure
            return draft
        if not reference.strip():  # pragma: no cover - defensive
            raise CategorisedProcessingError(
                ErrorCategory.EMPTY_DOCUMENT,
                "Cannot run Pass 2a on empty document (no content chunks)",
            )
        language = display_name(doc.language) if doc.language else None

        offsets = self._chunk_offsets(chunks, reference)

        # №21: the file-role map process_raw threaded in; only full / auxiliary
        # roles reached the chunk stage (structure_only did not).
        role_map: dict[str, str] = doc.metadata.get("file_roles", {})

        def _role_of(chunk: ContentChunk) -> str:
            return role_map.get(str(chunk.metadata["file_path"]), ROLE_FULL)

        # Step 1 — per-file skeleton + describe (bounded concurrency,
        # presentation Pass 1 precedent: fail-fast on first failure). Decision 7:
        # the per-file prompt gets the role so an auxiliary file is described
        # CONCISELY (not less thoroughly — the Mentor reads the description).
        sem = asyncio.Semaphore(_PER_FILE_CONCURRENCY)

        async def _describe(chunk: ContentChunk) -> CodeSegmentDescription:
            async with sem:
                skeleton, core = await self._skeleton_for(chunk, router)
                return await self._describe_file(
                    chunk,
                    skeleton,
                    core,
                    router,
                    language,
                    is_auxiliary=_role_of(chunk) == ROLE_AUXILIARY,
                )

        results = await asyncio.gather(
            *(_describe(c) for c in chunks), return_exceptions=True
        )
        failures = [
            (c.metadata["file_path"], r)
            for c, r in zip(chunks, results, strict=True)
            if isinstance(r, BaseException)
        ]
        if failures:
            first_path, first_exc = failures[0]
            raise ProcessingError(
                f"per-file describe failed on {first_path!r}: {first_exc}; "
                f"{len(failures)}/{len(chunks)} files total failed"
            )
        descriptions = [r for r in results if not isinstance(r, BaseException)]

        # Step 2 — ONE large-window summary call (input: structure +
        # ready per-file descriptions, never skeletons — ratified).
        structure = self._build_structure(doc, chunks)
        summary = await self._summarise(doc, chunks, descriptions, structure, router)

        # Deterministic segment drafts; content filled in Pass 2b. Decision 5:
        # the is_auxiliary flag is written from the SAME role as the tree entry.
        segments = [
            DocumentSegmentDraft(
                order=i,
                start_pos=start,
                end_pos=end,
                title=str(chunk.metadata["file_path"]).rsplit("/", 1)[-1][:_TITLE_MAX],
                description=desc.description,
                main_concepts=desc.main_concepts,
                secondary_concepts=desc.secondary_concepts,
                is_auxiliary=_role_of(chunk) == ROLE_AUXILIARY,
            )
            for i, (chunk, (start, end), desc) in enumerate(
                zip(chunks, offsets, descriptions, strict=True)
            )
        ]

        # Doc-level concepts: KD-2.1-O union + dedup + conflict rule, now
        # role-aware (decision 6). A full file contributes main→main /
        # secondary→secondary as before; an auxiliary file sends ALL its concepts
        # (even the ones central WITHIN the file) to secondary only. The conflict
        # rule (a concept primary anywhere is not also secondary) is verbatim.
        all_main: set[str] = set()
        all_secondary: set[str] = set()
        for chunk, desc in zip(chunks, descriptions, strict=True):
            if _role_of(chunk) == ROLE_AUXILIARY:
                all_secondary.update(desc.main_concepts)
                all_secondary.update(desc.secondary_concepts)
            else:  # full (structure_only never reaches a chunk)
                all_main.update(desc.main_concepts)
                all_secondary.update(desc.secondary_concepts)
        all_secondary -= all_main

        draft = DocumentSummaryDraft.model_validate(
            {
                "title": (summary.title or doc.title)[:_TITLE_MAX],
                "description": summary.description,
                "main_concepts": sorted(all_main),
                "secondary_concepts": sorted(all_secondary),
                "segments": [s.model_dump() for s in segments],
            },
            context={"reference_text_length": len(reference)},
        )
        draft.structure = structure
        return draft

    @staticmethod
    def _chunk_offsets(
        chunks: list[ContentChunk], reference: str
    ) -> list[tuple[int, int]]:
        """Deterministic FULL-COVER offsets over the assemble_text join.

        Segment ``i`` spans from its chunk's start to the next chunk's
        start (the inter-chunk ``"\\n\\n"`` separator belongs to the
        preceding segment); the last segment ends at ``len(reference)``.
        """
        starts: list[int] = []
        pos = 0
        for i, chunk in enumerate(chunks):
            starts.append(pos)
            pos += len(chunk.text)
            if i < len(chunks) - 1:
                pos += 2  # the "\n\n" separator
        if pos != len(reference):  # pragma: no cover - arithmetic invariant
            raise ProcessingError(
                f"code offset arithmetic diverged from assemble_text: "
                f"{pos} != {len(reference)}"
            )
        return [
            (start, starts[i + 1] if i + 1 < len(starts) else len(reference))
            for i, start in enumerate(starts)
        ]

    async def _skeleton_for(
        self, chunk: ContentChunk, router: StageRouter
    ) -> tuple[str, list[str]]:
        """R4 extraction ladder for one file; returns (skeleton, namespace core).

        Rung order: verbatim (README/config) → AST (Python) → regex
        (v1 families) → LLM ``code_skeleton_extraction`` → deterministic
        minimum. Every output is code-side capped at 8192 bytes.
        """
        path = str(chunk.metadata["file_path"])
        size = int(chunk.metadata["size_bytes"])
        body = chunk.text[len(_file_header(path)) :]
        ext = extension_of(path)
        core = namespace_core(path, ext, body)

        if is_verbatim_file(path):
            return verbatim_skeleton(path, body), core

        if ext == "py":
            skeleton = ast_skeleton(path, body)
            if skeleton is not None:
                return skeleton, core

        skeleton = regex_skeleton(path, ext, body)
        if skeleton is not None:
            return skeleton, core

        llm_skeleton = await self._llm_skeleton(path, ext, body, router)
        if llm_skeleton is not None:
            skeleton_text, llm_core = llm_skeleton
            # Namespace core: prefer the LLM-extracted one (it parsed the
            # actual language structure); on this rung the deterministic
            # ``core`` is only the file-stem fallback (AST/regex did not
            # apply here), so it backstops an empty LLM namespace rather
            # than competing with a richer one.
            return skeleton_text, llm_core or core

        return minimum_skeleton(path, body, size_bytes=size), core

    async def _llm_skeleton(
        self, path: str, ext: str, body: str, router: StageRouter
    ) -> tuple[str, list[str]] | None:
        """LLM rung — spike-ratified stage; ``None`` degrades to minimum.

        Ladder exhaustion here must NOT fail the whole document (the
        deterministic minimum rung sits below by design, R4), so the
        stage's LadderExhaustedError is caught and logged loudly.
        """
        parsed: dict[str, CodeSkeletonResult] = {}

        def _validator(content: str) -> None:
            try:
                parsed["result"] = CodeSkeletonResult.model_validate_json(
                    _strip_fences(content)
                )
            except ValidationError as exc:
                first = exc.errors()[0]
                raise StructuralRetryError(
                    f"{first.get('msg', 'validation error')} "
                    f"(field: {'.'.join(str(x) for x in first.get('loc', []))}). "
                    "Emit strict JSON per the skeleton schema."
                ) from exc

        try:
            await router.execute_for_stage(
                "code_skeleton_extraction",
                response_validator=_validator,
                expects_json=True,
                file_path=path,
                extension=ext,
                content=body,
            )
        except LadderExhaustedError as exc:
            logger.warning(
                "code_skeleton.llm_rung_exhausted",
                file_path=path,
                error=str(exc),
            )
            return None
        result = parsed["result"]
        rendered = render_llm_skeleton(path, result)
        core = list(result.module_namespace) + [
            d.name for d in result.declarations if d.kind in {"class", "module"}
        ]
        return rendered, core

    async def _describe_file(
        self,
        chunk: ContentChunk,
        skeleton: str,
        core: list[str],
        router: StageRouter,
        language: str | None,
        *,
        is_auxiliary: bool = False,
    ) -> CodeSegmentDescription:
        """One cheap ``code_segment_description`` call per included file.

        Decision 7 (careful form): an auxiliary file is described CONCISELY, not
        less thoroughly — ``is_auxiliary`` conditions the prompt so the
        description focuses on the file's purpose. An empty auxiliary description
        would be worse than a verbose one (the Mentor reads it).
        """
        parsed: dict[str, CodeSegmentDescription] = {}

        def _validator(content: str) -> None:
            try:
                parsed["result"] = CodeSegmentDescription.model_validate_json(
                    _strip_fences(content)
                )
            except ValidationError as exc:
                first = exc.errors()[0]
                raise StructuralRetryError(
                    f"{first.get('msg', 'validation error')} "
                    f"(field: {'.'.join(str(x) for x in first.get('loc', []))}). "
                    "Emit strict JSON per the description schema."
                ) from exc

        await router.execute_for_stage(
            "code_segment_description",
            response_validator=_validator,
            expects_json=True,
            file_path=str(chunk.metadata["file_path"]),
            skeleton=skeleton,
            namespace_core=", ".join(core),
            language=language,
            is_auxiliary=is_auxiliary,
        )
        return parsed["result"]

    def _build_structure(
        self, doc: SourceDocument, chunks: list[ContentChunk]
    ) -> dict[str, Any]:
        """Project tree for ``DocumentSummary.structure`` (ratified).

        Source → representation: built from the extraction results the
        processor already holds; the KD18 manifest artifact remains
        canonical in its own contour. Included entries carry
        ``cls='included'``; excluded carry the verdict reason. №21 (decision 2)
        adds a ``role`` per entry (full / auxiliary / structure_only), a
        SEPARATE axis from ``cls`` — excluded rows have ``role=None`` (the author
        cannot restore what sanitization removed, decision 4). The role is
        written from the same decision as the segment ``is_auxiliary`` flag
        (invariant).
        """
        role_map: dict[str, str] = doc.metadata.get("file_roles", {})
        entries: list[dict[str, Any]] = [
            {
                "path": str(c.metadata["file_path"]),
                "size": int(c.metadata["size_bytes"]),
                "cls": "included",
                "reason": None,
                "role": role_map.get(str(c.metadata["file_path"]), ROLE_FULL),
            }
            for c in chunks
        ]
        for do_entry in doc.metadata.get("description_only_entries", []):
            entries.append(
                {
                    "path": do_entry["path"],
                    "size": do_entry["size"],
                    "cls": "description_only",
                    "reason": do_entry["reason"],
                    "role": role_map.get(str(do_entry["path"]), ROLE_STRUCTURE_ONLY),
                }
            )
        for exc_entry in doc.metadata.get("excluded_entries", []):
            # ``entries`` (the collapsed file count) rides only on excluded
            # rows — included / description_only stay 1-per-file (№19).
            entries.append(
                {
                    "path": exc_entry["path"],
                    "size": exc_entry["size"],
                    "cls": "excluded",
                    "reason": exc_entry["reason"],
                    "role": None,
                    "entries": exc_entry.get("entries", 1),
                }
            )
        return {"entries": entries}

    async def _summarise(
        self,
        doc: SourceDocument,
        chunks: list[ContentChunk],
        descriptions: list[CodeSegmentDescription],
        structure: dict[str, Any],
        router: StageRouter,
    ) -> CodeSummaryResult:
        """ONE large-window ``code_summary`` call (ratified two-step)."""
        # LLM layer (№19): the model gets the CONSEQUENCE, not the raw
        # token, and the excluded set is aggregated to one line per kind —
        # never the internal reason token, never a service-file path. The
        # persisted DB structure stays full per-file; this collapse is the
        # prompt's alone.
        structure_block = render_structure_block(structure["entries"])
        descriptions_block = "\n\n".join(
            f"[{c.metadata['file_path']}]\n{d.description}\n"
            f"concepts: {', '.join(d.main_concepts) or '—'}"
            for c, d in zip(chunks, descriptions, strict=True)
        )
        parsed: dict[str, CodeSummaryResult] = {}

        def _validator(content: str) -> None:
            try:
                parsed["result"] = CodeSummaryResult.model_validate_json(
                    _strip_fences(content)
                )
            except ValidationError as exc:
                first = exc.errors()[0]
                raise StructuralRetryError(
                    f"{first.get('msg', 'validation error')} "
                    f"(field: {'.'.join(str(x) for x in first.get('loc', []))}). "
                    "Emit strict JSON per the summary schema."
                ) from exc

        await router.execute_for_stage(
            "code_summary",
            response_validator=_validator,
            expects_json=True,
            document_title=doc.title,
            structure_block=structure_block,
            file_descriptions=descriptions_block,
            language=display_name(doc.language) if doc.language else None,
        )
        return parsed["result"]

    # ── Pass 2b: deterministic slice + file_path anchor ──

    async def process_detail(
        self,
        doc: SourceDocument,
        summary_draft: DocumentSummaryDraft,
    ) -> list[DocumentSegmentDraft]:
        """Fill content by slicing + set the ``file_path`` anchor.

        Zero LLM calls. ``compute_paragraph_anchors`` is deliberately
        NOT called for code (ratified segment model): the fourth anchor
        kind is the file path; exactly one anchor is non-null per row.
        """
        reference = doc.assemble_text()
        chunks = [c for c in doc.chunks if c.text]
        filled: list[DocumentSegmentDraft] = []
        for draft, chunk in zip(summary_draft.segments, chunks, strict=True):
            update: dict[str, object] = {
                "file_path": str(chunk.metadata["file_path"]),
            }
            if draft.content is None:
                update["content"] = reference[draft.start_pos : draft.end_pos]
            filled.append(draft.model_copy(update=update))
        return filled


__all__ = ["CodeProcessor"]
