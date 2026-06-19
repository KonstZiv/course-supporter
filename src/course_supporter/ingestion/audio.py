"""AudioProcessor — three-stage audio ingestion pipeline (KD-2.2-D/E/F/A/I).

Phase 2.2 sub-area #5 (Krok C). AudioProcessor consumes word-aligned
STT output and produces a ``DocumentSummaryDraft`` plus a list of
``DocumentSegmentDraft`` parallel to the text/web pipeline. Three
stages mirror ``VideoProcessor``:

* :meth:`process_raw` — STT call + inline word cache + per-segment
  ``ContentChunk`` construction + critical invariant assert.
* :meth:`process_macro` — Pass 2a metadata mapping via
  ``StageRouter.execute_for_stage("audio_pass_2a_mapping", ...)``;
  ``AudioPass2aResult`` → ``DocumentSummaryDraft`` conversion with
  three pass-through fields (title, description, noisy) and
  algorithmic concept aggregation parallel to TextProcessor's
  KD-2.1-O union+dedup pattern.
* :meth:`process_detail` — Pass 2b algorithmic slice via the
  ``chars_per_word_cumsum`` bridge (KD-2.2-F) + Pass 2c selective
  denoise on segments where ``noisy=True`` via the
  ``audio_pass_2c_denoise`` stage (``asyncio.gather`` over the
  filtered subset).

Factory dispatch invariant (``create_processors``):
the factory routes by ``source_type`` so AudioProcessor only ever
receives ``SourceType.AUDIO`` inputs. Entry guard intentionally
absent — the belt-and-suspenders pattern in TextProcessor /
WebProcessor is legacy; factory dispatch is the architectural
defence. If a future refactor disables source_type-based factory
dispatch, an explicit entry guard must be added here.

Doc-level pass-through (KD-2.2-A v0.3 + v0.4): the LLM emits a
top-level ``title`` / ``description`` (Pass 2a) plus a per-segment
``noisy`` flag (Pass 2a). ``process_macro`` copies all three into
the downstream Pydantic carriers without algorithmic derivation —
preserves LLM judgement over client-side defaults per dev-time
orientation (quality > simplicity). ``title`` may be ``None`` per
``AudioPass2aResult`` schema (talks without an explicit theme);
the ``""`` fallback at the conversion point preserves the
``DocumentSummaryDraft.title: str`` contract without a
cross-cutting schema change. The empty string is a sentinel
meaning "untitled / no explicit theme", not a bug.

Subsegment flattening: AudioPass2aResult.segments may carry an
optional 2-level hierarchy (≤5 subsegments per segment). The
processor emits top-level segments only as DocumentSegmentDraft —
subsegments serve as LLM-internal hierarchical scaffolding and as
input to the ``_subsegments_cover_parent`` validator gate
(KD-2.2-H DD-2.2-X failure mode mitigation). Subsegment-level
granularity is not propagated downstream; the future-enhancement
hook is via Phase 2.5+ if production data motivates fine-grained
denoise routing.

Noisy aggregation: Pass 2c routing uses top-level
``segment.noisy`` directly without aggregating subsegment noisy
values. The LLM emits ``noisy`` independently on both levels per
prompt directive; top-level ``noisy=True`` instructs
``process_detail`` to denoise the whole segment content,
``noisy=False`` keeps the raw slice. Subsegment noisy values
influence only the ``_subsegments_cover_parent`` validator and
the LLM's hierarchical reasoning — they do not affect routing.

Cache (KD-2.2-D): inline ``_store_words`` / ``_fetch_words``
Redis-backed methods; TTL = ``worker_job_timeout`` (DD-3.3c-G,
lockstep with the video STT carrier); key
``audio_transcription:{job_id}``. Partial-result discipline —
``_store_words`` is called only on successful STT completion;
partial provider output raises ``ProcessingError`` before the
cache write.

Critical invariant (KD-2.2-E line 333): ``process_raw`` asserts
``assemble_text(doc) == " ".join(w.text for w in result.words)``
as a runtime precondition for Pass 2b correctness. The assertion
references audio's source_type-conditional separator in
``models/source.py`` (v0.3 ``5b7fc67`` consolidation). The assert
serves as a production runtime tripwire — it fails fast if
``models/source.py`` reverts the separator branch or if chunk
construction here violates segment-ordering invariants.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Protocol

import structlog
from arq.connections import ArqRedis
from pydantic import ValidationError

from course_supporter.config import get_settings
from course_supporter.ingestion.base import (
    MaterialProcessor,
    ProcessingError,
    UnsupportedFormatError,
)
from course_supporter.ingestion.schemas import (
    AudioPass2aResult,
    AudioPass2cResult,
    DocumentSegmentDraft,
    DocumentSummaryDraft,
)
from course_supporter.language import display_name
from course_supporter.llm.error_categories import StructuralRetryError
from course_supporter.models.source import (
    ChunkType,
    ContentChunk,
    SourceDocument,
)
from course_supporter.service_logging import get_current_job_id
from course_supporter.stt.router import STTRouter
from course_supporter.stt.schemas import STTWord

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.llm.stage_router import StageRouter
    from course_supporter.storage.orm import AuthoredDocument

logger = structlog.get_logger()

_WORD_CACHE_KEY_TMPL = "audio_transcription:{job_id}"

_PASS_2A_STAGE_NAME = "audio_pass_2a_mapping"  # noqa: S105
_PASS_2C_STAGE_NAME = "audio_pass_2c_denoise"  # noqa: S105


class SupportsText(Protocol):
    """A word-like token exposing ``.text`` — the only field the bridge reads.

    Lets :func:`chars_per_word_cumsum` serve any word-stream source type
    structurally: audio ``STTWord`` and video ``SttWord`` both satisfy it
    without a shared base class. (Phase 2.4 task 2.4.5 — video reuse;
    DD-2.4-E covers extracting the bridge to a shared module if a third
    cross-processor consumer appears.)
    """

    text: str


def chars_per_word_cumsum(words: Sequence[SupportsText]) -> list[int]:
    """Word-index → char-offset bridge for audio Pass 2b (KD-2.2-F).

    Builds a cumulative offset array such that ``cumsum[i]`` is the
    char position in the canonical space-joined word stream where
    word ``i`` begins, and ``cumsum[len(words)]`` equals the total
    length of that stream — which equals ``len(assemble_text(doc))``
    once audio's source_type-conditional separator lands in sub-area
    #6 (single space inter-chunk).

    Every word in positions ``[0, len(words) - 1)`` contributes its
    text length plus one for the trailing space separator; the last
    word contributes only its text length (no trailing separator
    in ``assemble_text`` output). Without this branch, the final
    cumulative entry would over-count by one trailing space and
    break the ``last.end_pos == len(assemble_text(doc))`` invariant
    enforced by ``DocumentSummaryDraft._coverage_matches_reference_length``.

    Pass 2b slice (KD-2.2-F) populates
    ``DocumentSegmentDraft.start_pos = cumsum[seg.start_word_idx]``
    and ``DocumentSegmentDraft.end_pos = cumsum[seg.end_word_idx]``;
    non-last segments thus include their trailing inter-word space
    in the slice (parallel to text/web's ``\\n\\n`` convention where
    each non-last segment owns its trailing separator).

    Empty ``words`` returns ``[0]`` — the single-element baseline
    matches ``segments[-1].end_pos == 0`` if a (degenerate) Pass 2a
    were to emit an empty segments list.
    """
    if not words:
        return [0]
    cumsum = [0]
    last_idx = len(words) - 1
    for i, w in enumerate(words):
        space = 1 if i < last_idx else 0
        cumsum.append(cumsum[-1] + len(w.text) + space)
    return cumsum


class AudioProcessor(MaterialProcessor):
    """Audio source-type processor — three-stage ingestion (KD-2.2-D/E/F/A/I).

    See module docstring for architectural context (factory dispatch
    invariant, cache semantics, doc-level pass-through, subsegment
    flattening, noisy aggregation, bridge contract, critical
    invariant tripwire). Constructor signature is forward-declarative;
    factory wiring + arq redis injection land in sub-areas #6 + #7.
    """

    SUPPORTED_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".ogg", ".flac"})
    MAX_DURATION_SEC = 150 * 60  # vision.md §5 Phase 2.2

    def __init__(
        self,
        *,
        stt_router: STTRouter,
        redis: ArqRedis,
        stage_router: StageRouter | None = None,
    ) -> None:
        super().__init__()
        self._stt_router = stt_router
        self._redis = redis
        # Pass 2c text-cleanup ladder (task 2.4.7). Optional so the ~20
        # direct ``AudioProcessor(stt_router=, redis=)`` test constructions
        # keep working; the factory injects the real router in production
        # (``api/tasks.py`` ctx). When present it is the Pass 2c router
        # source — process_detail prefers it over the (never-passed) ABC
        # ``router`` method-arg, mirroring the VideoProcessor injection.
        self._stage_router = stage_router

    async def _store_words(self, job_id: uuid.UUID, words: list[STTWord]) -> None:
        """Store STT words in Redis (KD-2.2-D inline cache).

        Per the partial-result discipline (S2 implementer review),
        callers invoke this only on successful STT completion;
        truncated provider output must raise ``ProcessingError``
        upstream before reaching the cache.
        """
        key = _WORD_CACHE_KEY_TMPL.format(job_id=job_id)
        payload = json.dumps([w.model_dump() for w in words], separators=(",", ":"))
        ttl = get_settings().worker_job_timeout
        await self._redis.set(key, payload, ex=ttl)

    async def _fetch_words(self, job_id: uuid.UUID) -> list[STTWord] | None:
        """Fetch cached STT words from Redis (KD-2.2-D inline cache).

        Returns ``None`` if the key is missing or expired; callers
        treat that as a process_raw / process_macro ordering
        violation and raise ``ProcessingError``.
        """
        key = _WORD_CACHE_KEY_TMPL.format(job_id=job_id)
        raw = await self._redis.get(key)
        if raw is None:
            return None
        data = json.loads(raw)
        return [STTWord.model_validate(item) for item in data]

    async def process_raw(
        self,
        source: AuthoredDocument,
        *,
        router: ModelRouter | None = None,
    ) -> SourceDocument:
        """Stage 1 — STT transcription → SourceDocument з chunks.

        The ``router`` parameter is accepted for ABC symmetry but
        not used (audio routes STT calls through ``self._stt_router``,
        not the LLM ``ModelRouter``).

        Flow:
        1. Read ``job_id`` from ContextVar (set at arq task entry).
        2. Call ``STTRouter.transcribe`` — ladder + log_callback per
           Phase 2.1 infrastructure.
        3. Entry guard (KD-2.2-C) — ``result.words`` non-empty;
           raise ``ProcessingError`` if the provider does not surface
           word-level alignment.
        4. Store words in Redis (KD-2.2-D inline cache).
        5. Build one ``ContentChunk`` per STT segment with space-joined
           words and segment-level timestamps (KD-2.2-E).
        6. Construct ``SourceDocument`` з pass-through
           ``source_type`` (Option (a) reformulation; zero
           ``SourceType.AUDIO`` reference).
        7. Assert the critical invariant (KD-2.2-E line 333) —
           runtime precondition for the Pass 2b char-offset bridge.
        """
        job_id = get_current_job_id()
        if job_id is None:
            raise ProcessingError(
                "AudioProcessor.process_raw requires ContextVar "
                "job_id; the ARQ task entry must set _current_job_id "
                "before invocation."
            )

        result = await self._stt_router.transcribe(
            "transcribe",
            source.source_url,
            language=None,
        )

        if not result.words:
            raise ProcessingError(
                f"STT provider '{result.provider}' returned an empty "
                f"words list for job_id={job_id}; AudioProcessor "
                f"requires word-level alignment per KD-2.2-C. Verify "
                f"the provider is Scribe v2 or a compatible word-level "
                f"STT model."
            )

        duration = result.audio_duration_sec
        if duration is not None and duration > self.MAX_DURATION_SEC:
            raise UnsupportedFormatError(
                f"Audio duration ({duration / 60:.1f} min) exceeds maximum "
                f"{self.MAX_DURATION_SEC / 60:.0f} min per Phase 2.2 vision §5."
            )

        await self._store_words(job_id, result.words)

        chunks: list[ContentChunk] = []
        for idx, seg in enumerate(result.segments):
            seg_words = [
                w for w in result.words if seg.start_sec <= w.start_sec < seg.end_sec
            ]
            if not seg_words:
                continue
            chunks.append(
                ContentChunk(
                    chunk_type=ChunkType.TRANSCRIPT,
                    text=" ".join(w.text for w in seg_words),
                    index=idx,
                    start_sec=seg.start_sec,
                    end_sec=seg.end_sec,
                )
            )

        doc = SourceDocument(
            source_type=source.source_type,
            source_url=source.source_url,
            title=source.filename or "",
            chunks=chunks,
        )

        expected = " ".join(w.text for w in result.words)
        actual = doc.assemble_text()
        assert actual == expected, (
            f"AudioProcessor invariant violated: "
            f"len(assemble_text)={len(actual)} != "
            f"len(' '.join(words))={len(expected)}. Check that "
            f"sub-area #6 assemble_text source_type-conditional "
            f"separator landed (KD-2.2-E body) and chunks cover "
            f"all words without duplication or omission."
        )

        return doc

    async def process_macro(
        self,
        doc: SourceDocument,
        router: StageRouter,
    ) -> DocumentSummaryDraft:
        """Pass 2a — audio metadata mapping (KD-2.2-A + KD-2.2-H).

        Flow:
        1. Read ``job_id`` ContextVar.
        2. Fetch cached words (cache hit invariant — process_raw
           must populate before this method runs).
        3. Build Jinja template kwargs (``words_count``,
           ``words_json``) for the prompt.
        4. ``StageRouter.execute_for_stage`` with
           ``audio_pass_2a_mapping`` stage; the response_validator
           wraps a Pydantic ``ValidationError`` as
           ``StructuralRetryError`` per Phase 2.1 fixup 2.1.7.2.
        5. Convert ``AudioPass2aResult`` → ``DocumentSummaryDraft``:
           three pass-through conversions (title, description,
           noisy) plus algorithmic concept aggregation parallel до
           TextProcessor's KD-2.1-O union+dedup pattern.

        Subsegment flattening — top-level segments only (see module
        docstring rationale).
        """
        job_id = get_current_job_id()
        if job_id is None:
            raise ProcessingError(
                "AudioProcessor.process_macro requires ContextVar "
                "job_id; cache lookup needs it."
            )

        words = await self._fetch_words(job_id)
        if not words:
            raise ProcessingError(
                f"AudioProcessor.process_macro: word cache miss for "
                f"job_id={job_id}. process_raw must populate the cache "
                f"before process_macro is called."
            )

        total_word_count = len(words)
        words_json = json.dumps(
            [
                {
                    "text": w.text,
                    "start": w.start_sec,
                    "end": w.end_sec,
                    "logprob": w.logprob,
                }
                for w in words
            ],
            separators=(",", ":"),
        )

        parsed: dict[str, AudioPass2aResult] = {}

        def _audio_pass_2a_validator(content: str) -> None:
            """StageRouter response_validator (Phase 2.1 fixup 2.1.7.2).

            Translates a Pydantic ``ValidationError`` into
            ``StructuralRetryError`` so the ladder's instructor-style
            retry + fallback path fires.
            """
            try:
                local = AudioPass2aResult.model_validate_json(
                    content,
                    context={"total_word_count": total_word_count},
                )
            except ValidationError as exc:
                error_types = sorted({e.get("type", "unknown") for e in exc.errors()})
                first = exc.errors()[0]
                first_loc = ".".join(str(x) for x in first.get("loc", []))
                logger.warning(
                    "audio_pass2a.validation.failed",
                    job_id=str(job_id),
                    validation_error_types=error_types,
                    first_error_msg=first.get("msg", ""),
                    first_error_loc=first_loc,
                    total_word_count=total_word_count,
                )
                feedback = (
                    f"{first.get('msg', 'validation error')} "
                    f"(field: {first_loc or '<root>'}). "
                    "Regenerate the response with valid output."
                )
                raise StructuralRetryError(feedback) from exc
            parsed["result"] = local

        router_result = await router.execute_for_stage(
            _PASS_2A_STAGE_NAME,
            response_validator=_audio_pass_2a_validator,
            expects_json=True,
            words_count=total_word_count,
            words_json=words_json,
            language=display_name(doc.language) if doc.language else None,
        )
        audio_result = parsed["result"]
        logger.debug(
            "audio_pass2a.validation.ok",
            job_id=str(job_id),
            provider=router_result.provider_used,
            model=router_result.model_used,
            attempt_count=router_result.attempt_count,
            total_word_count=total_word_count,
            segment_count=len(audio_result.segments),
        )

        cumsum = chars_per_word_cumsum(words)
        segment_drafts: list[DocumentSegmentDraft] = []
        for idx, seg in enumerate(audio_result.segments):
            segment_drafts.append(
                DocumentSegmentDraft(
                    order=idx,
                    start_pos=cumsum[seg.start_word_idx],
                    end_pos=cumsum[seg.end_word_idx],
                    title=seg.title,
                    description=seg.description,
                    main_concepts=list(seg.main_concepts),
                    secondary_concepts=list(seg.secondary_concepts),
                    start_time_sec=words[seg.start_word_idx].start_sec,
                    end_time_sec=words[seg.end_word_idx - 1].end_sec,
                    noisy=seg.noisy,
                )
            )

        all_main: set[str] = set()
        all_secondary: set[str] = set()
        for seg in audio_result.segments:
            all_main.update(seg.main_concepts)
            all_secondary.update(seg.secondary_concepts)
        all_secondary -= all_main

        return DocumentSummaryDraft(
            title=audio_result.title or "",
            description=audio_result.description,
            main_concepts=sorted(all_main),
            secondary_concepts=sorted(all_secondary),
            segments=segment_drafts,
        )

    async def process_detail(
        self,
        doc: SourceDocument,
        summary_draft: DocumentSummaryDraft,
        *,
        router: StageRouter | None = None,
    ) -> list[DocumentSegmentDraft]:
        """Pass 2b + Pass 2c — algorithmic slice + selective denoise.

        Pass 2b (always): for each ``DocumentSegmentDraft`` carrying
        ``content=None``, slice ``content`` from
        ``doc.assemble_text()[start_pos:end_pos]``. The bridge
        offsets were populated in process_macro per KD-2.2-F.

        Pass 2c (selective): segments з ``noisy=True`` go through the
        ``audio_pass_2c_denoise`` stage; the single-string response
        replaces the raw slice as the segment's ``content``. Non-
        noisy segments keep the raw slice.

        Pass 2c routes on top-level ``segment.noisy``; subsegment
        noisy values are used by the ``_subsegments_cover_parent``
        validator but are not aggregated up for the routing
        decision (per module docstring rationale).

        Router source (task 2.4.7): the orchestrator passes a router
        only to ``process_macro``, never to ``process_detail`` — so
        Pass 2c uses the ctor-injected ``self._stage_router`` (wired by
        the factory in production), preferring it over the keyword-only
        ``router`` method-arg, which is retained for ABC symmetry. Only
        when neither is available (router-less direct construction in
        tests) does Pass 2c skip with a warning and return raw slices.
        """
        reference_text = doc.assemble_text()

        # Pass 2b — algorithmic slice for every segment.
        sliced: list[DocumentSegmentDraft] = []
        for draft in summary_draft.segments:
            if draft.content is not None:
                sliced.append(draft)
                continue
            raw_content = reference_text[draft.start_pos : draft.end_pos]
            sliced.append(draft.model_copy(update={"content": raw_content}))

        # Pass 2c — selective denoise on top-level noisy=True.
        noisy_indices = [i for i, draft in enumerate(sliced) if draft.noisy]
        if not noisy_indices:
            return sliced

        # Prefer the ctor-injected stage_router (factory wiring, task 2.4.7)
        # over the never-passed ABC method-arg. In production one is always
        # present, so the skip below is reachable only in router-less direct
        # construction (tests).
        effective_router = self._stage_router or router
        if effective_router is None:
            logger.warning(
                "audio_pass2c.skipped_no_router",
                noisy_segment_count=len(noisy_indices),
                reason=(
                    "AudioProcessor.process_detail has no stage_router "
                    "(neither injected nor passed); Pass 2c selective "
                    "denoise skipped, raw slices returned."
                ),
            )
            return sliced

        denoise_results = await asyncio.gather(
            *(self._denoise_segment(sliced[i], effective_router) for i in noisy_indices)
        )
        for i, cleaned in zip(noisy_indices, denoise_results, strict=True):
            sliced[i] = sliced[i].model_copy(update={"content": cleaned})

        return sliced

    async def _denoise_segment(
        self,
        draft: DocumentSegmentDraft,
        router: StageRouter,
    ) -> str:
        """Pass 2c denoise call for a single noisy segment (KD-2.2-I).

        Returns the cleaned content as a plain string. The Pass 2c
        prompt commits to single-string output (no JSON wrapping);
        the response_validator wraps the raw response into
        ``AudioPass2cResult.content`` and the cleaned string is
        returned to the caller.
        """
        raw_content = draft.content or ""
        parsed: dict[str, AudioPass2cResult] = {}

        def _audio_pass_2c_validator(content: str) -> None:
            try:
                local = AudioPass2cResult.model_validate({"content": content})
            except ValidationError as exc:
                first = exc.errors()[0]
                first_loc = ".".join(str(x) for x in first.get("loc", []))
                logger.warning(
                    "audio_pass2c.validation.failed",
                    segment_order=draft.order,
                    first_error_msg=first.get("msg", ""),
                    first_error_loc=first_loc,
                )
                feedback = (
                    f"{first.get('msg', 'validation error')} "
                    f"(field: {first_loc or '<root>'}). "
                    "Regenerate the response as plain cleaned text."
                )
                raise StructuralRetryError(feedback) from exc
            parsed["result"] = local

        await router.execute_for_stage(
            _PASS_2C_STAGE_NAME,
            response_validator=_audio_pass_2c_validator,
            content=raw_content,
            title=draft.title,
            description=draft.description,
            main_concepts_json=json.dumps(draft.main_concepts, separators=(",", ":")),
            secondary_concepts_json=json.dumps(
                draft.secondary_concepts, separators=(",", ":")
            ),
        )
        return parsed["result"].content
