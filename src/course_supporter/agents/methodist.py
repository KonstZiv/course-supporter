"""MethodistAgent — KD16 production agent for two-pass methodist generation.

Phase 3.2.3a ships the Pass 1 (bottom-up) implementation. Pass 2 is a
deliberate no-op until Phase 3.2.3b lands; the orchestrator
(``storage/node_summary_orchestrator.py``) consumes both methods via
the :class:`MethodistGenerator` Protocol so the seam is locked from
Phase 3.2.2 onwards — Phase 3.2.3b will replace the no-op without any
orchestrator refactor.

KD16 reference pattern: ``ingestion/text.py`` Pass 2a callsite —
``StageRouter.execute_for_stage`` with a ``response_validator`` closure
that translates ``pydantic.ValidationError`` to
:class:`StructuralRetryError` so the existing instructor-style retry
+ ladder fallback machinery fires before terminal failure.

Domain responsibilities split (probe-resolved S3 + S5, ratified):

* **Agent owns** canonical content fields (everything LLM-generated +
  deterministic concept unions + per-node size counters).
* **Orchestrator owns** source-hash linkages (``source_content_hash``
  after the hook returns; ``enclosing_context_source_hash`` after
  Pass 2 — Phase 3.2.3b territory).
* **Deterministic by code, never asked of the LLM** (KD-2.1-O extended):
  ``main_concepts`` / ``secondary_concepts`` (union over own
  DocumentSummary + children NodeSummaryRaw concepts);
  ``own_documents_count`` / ``own_chars_count`` /
  ``cumulative_documents_count`` / ``cumulative_chars_count``.

Budget exhaustion contract: when ``StageRouter`` exhausts the ladder
because every rung's input estimate exceeds its budget, the agent
translates the infrastructure-level :class:`LadderExhaustedError`
into a domain-level :class:`MethodistInputBudgetExhaustedError` with
a "restructure your course" message. Other exhaustion reasons
(provider down, structural retries failed, etc.) propagate as the
original error so K2-catch in the orchestrator records the raw
infrastructure context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from course_supporter.language import display_name
from course_supporter.llm.error_categories import (
    LadderExhaustedError,
    StructuralRetryError,
)
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSummary,
    NodeSummaryRaw,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from course_supporter.llm.stage_router import StageRouter

logger = structlog.get_logger(__name__)


class Pass1ChildRawMissingError(RuntimeError):
    """Raised when a child CourseNode has no NodeSummaryRaw at parent visit time.

    KD10 leaf→root ordering guarantees every child's Raw row is committed
    before the orchestrator visits its parent. If
    :meth:`MethodistAgent._fetch_children_raws` sees a child with no
    active Raw row, the traversal contract was violated — either by an
    orchestrator bug, a partial-commit race, or external soft-delete
    during the walk. The agent does NOT silently drop the child from
    the prompt input: doing so would (a) deceive the LLM about the
    node's structure, and (b) produce wrong cumulative_* counters.

    The agent raises this error; K2-catch in the orchestrator records
    the parent node as ERROR and continues the walk. Mirrors the
    Phase 3.2.2 Pass 2 ``Pass2ParentRawMissingError`` pattern.

    Attributes:
        parent_node_id: CourseNode whose Pass 1 visit triggered the
            missing-child probe.
        child_node_id: CourseNode whose Raw row was absent.
    """

    def __init__(self, parent_node_id: Any, child_node_id: Any) -> None:
        self.parent_node_id = parent_node_id
        self.child_node_id = child_node_id
        super().__init__(
            f"Pass 1 read contract violation: child CourseNode "
            f"{child_node_id} of parent {parent_node_id} has no active "
            f"NodeSummaryRaw at the time of the parent's visit. "
            f"KD10 leaf→root order should have committed it. "
            f"Possible causes: orchestrator ordering bug, partial-commit "
            f"race, soft-delete during walk. Inspect orchestrator state "
            f"and child's Pass 1 errors[]."
        )


class MethodistRootResolutionError(RuntimeError):
    """Raised when walk-up from a CourseNode to the course root hits the depth cap.

    :meth:`MethodistAgent._resolve_course_context` walks parent_id
    until it reaches a root (parent_id IS NULL). A defensive depth cap
    of 25 surfaces accidentally-introduced cycles in the parent_id
    graph or pathologically deep trees. If the cap fires while
    parent_id is still non-NULL, the walk did NOT reach the real root
    — using a mid-chain node as the course title / language source
    would feed the LLM the wrong context (mid-chain default_language
    is mostly NULL per Phase 2.4.13 root-only CHECK constraint).
    Loud raise → K2 catch in the orchestrator → ERROR in run-state.

    Attributes:
        node_id: CourseNode that started the walk-up.
        last_mid_chain_node_id: The mid-chain node where the cap fired
            (still had parent_id set).
        depth_cap: The cap value (currently 25).
    """

    def __init__(
        self,
        node_id: Any,
        last_mid_chain_node_id: Any,
        depth_cap: int,
    ) -> None:
        self.node_id = node_id
        self.last_mid_chain_node_id = last_mid_chain_node_id
        self.depth_cap = depth_cap
        super().__init__(
            f"Course root resolution failed: walk-up from CourseNode "
            f"{node_id} did not reach a root within {depth_cap} steps "
            f"(stopped at {last_mid_chain_node_id}, still has a parent). "
            f"Possible causes: parent_id cycle (data corruption), tree "
            f"deeper than the defensive cap. Inspect parent_id chain."
        )


class MethodistInputBudgetExhaustedError(Exception):
    """Raised when Pass 1 ladder exhaustion is driven by input-budget skips.

    Domain-level signal: the node's input is too large for every rung's
    declared ``input_budget_ratio * max_context``. Recovery is for the
    author to restructure the course (split the node, reduce attached
    documents, reorganise children) — not an infrastructure retry.
    The orchestrator catches this via K2 and records it as a per-node
    ERROR with this message; operators inspect ``errors[]`` in
    ``Job.stage_progress`` to triage.

    Attributes:
        node_id: CourseNode that triggered the exhaustion.
        stage_name: Methodist stage that exhausted (``methodist_bottomup``).
        attempts: ``(provider, model, reason)`` triples preserved from
            the underlying :class:`LadderExhaustedError`.
    """

    def __init__(
        self,
        node_id: Any,
        stage_name: str,
        attempts: list[tuple[str, str, str]],
    ) -> None:
        self.node_id = node_id
        self.stage_name = stage_name
        self.attempts = attempts
        details = "; ".join(f"{p}/{m}: {r}" for p, m, r in attempts)
        super().__init__(
            f"Pass 1 input budget exhausted on node {node_id} for stage "
            f"'{stage_name}'. The node carries more authored material "
            f"than any methodist model in the ladder can fit inside "
            f"its safe-input window. Restructure the course: split this "
            f"node into smaller subtopics, reduce the number of attached "
            f"documents, or reorganise the children so the methodist "
            f"can synthesise per-node. Underlying ladder attempts: {details}"
        )


# ── LLM output schema (validated via response_validator) ──────────


class _MethodistBottomupLearningOutcomeItem(BaseModel):
    """LearningOutcomeItem mirrors ``NodeSummaryRaw.knowledge`` / ``.skills``."""

    model_config = ConfigDict(extra="forbid")
    name: str
    description: str


class _MethodistBottomupResult(BaseModel):
    """Strict shape of ``methodist_bottomup`` LLM output.

    Concept fields are intentionally absent — those are computed
    deterministically in code per KD-2.1-O (algorithmic union over
    own DocumentSummary concepts union children's NodeSummaryRaw concepts).
    """

    model_config = ConfigDict(extra="forbid")

    title: str = ""
    description: str = ""
    learning_objectives: list[str] = Field(default_factory=list)
    knowledge: list[_MethodistBottomupLearningOutcomeItem] = Field(default_factory=list)
    skills: list[_MethodistBottomupLearningOutcomeItem] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    assessment_approach: str = ""
    teaching_approach: str = ""
    key_activities: list[str] = Field(default_factory=list)
    common_mistakes: list[str] = Field(default_factory=list)
    compressed_summary: str = ""
    methodist_observations: list[str] = Field(default_factory=list)


# ── MethodistAgent ────────────────────────────────────────────────


class MethodistAgent:
    """KD16 methodist agent (Phase 3.2.3a).

    Implements the :class:`MethodistGenerator` Protocol from
    ``storage/node_summary_orchestrator.py``. Owns the per-node
    methodist synthesis call; the orchestrator owns traversal,
    per-node COMMIT, and source-hash linkage materialisation.

    Construct via the helper in ``agents.methodist_factory`` or in
    tests directly (``MethodistAgent(session, stage_router)``).
    The session is the same ``AsyncSession`` the orchestrator uses,
    per S5 ratify — there is no per-call session creation; reads share
    the orchestrator's transaction so children's already-committed
    Raws are visible without re-opening.
    """

    def __init__(
        self,
        session: AsyncSession,
        stage_router: StageRouter,
    ) -> None:
        self._session = session
        self._stage_router = stage_router

    async def generate_bottomup(self, node: CourseNode, raw: NodeSummaryRaw) -> None:
        """Pass 1: synthesise canonical fields + compressed_summary on ``raw``.

        Read inputs per S5 / KD10 read policy:

        * Own DocumentSummary rows for this node's ready AuthoredDocuments
          (NOT DocumentSegment — KD10 read policy MVP).
        * Children CourseNodes via ``parent_id == node.id``.
        * Children's NodeSummaryRaw rows for their ``compressed_summary``
          and concept fields (KD10 guarantees they are committed in
          the leaf→root walk by the time this node is visited).

        Resolve the course language by walking up to the root and
        reading ``CourseNode.default_language`` (ISO 639-3 per Phase
        2.4.13 entry-gate). The language is passed through
        :func:`display_name` for the render context, mirroring
        ``ingestion/text.py`` Pass 2a (mechanism 2.4.14).

        Mutate ``raw`` in place; the orchestrator flushes / commits and
        materialises ``raw.source_content_hash`` after this method
        returns successfully. On ladder exhaustion driven by input-budget
        skips, raise :class:`MethodistInputBudgetExhaustedError` for
        domain-level operator messaging.
        """
        own_docs = await self._fetch_own_ready_summaries(node.id)
        child_raws = await self._fetch_children_raws(node.id)
        course_title, language = await self._resolve_course_context(node)

        own_documents_payload = [
            {
                "title": d.title or "",
                "description": d.description or "",
                "main_concepts": list(d.main_concepts or []),
                "secondary_concepts": list(d.secondary_concepts or []),
            }
            for d in own_docs
        ]
        children_compressed = [
            {
                "title": c["title"],
                "compressed_summary": (c["compressed_summary"] or ""),
            }
            for c in child_raws
        ]

        # has_inputs distinguishes a structurally-trivial node (no own
        # documents AND no children — empty Raw legitimate per KD10
        # line 845) from a content-bearing node (at least one input). The
        # closure-captured validator uses this to enforce the semantic
        # minimum: title / description / compressed_summary must be
        # non-empty when the node carries any input. An empty response
        # would otherwise pass schema validation (all fields default to
        # empty) and produce a silent DONE on a content-bearing node.
        has_inputs = bool(own_documents_payload) or bool(children_compressed)

        parsed: dict[str, _MethodistBottomupResult] = {}

        def _validator(content: str) -> None:
            try:
                result = _MethodistBottomupResult.model_validate_json(content)
            except ValidationError as exc:
                first = exc.errors()[0]
                loc = ".".join(str(x) for x in first.get("loc", []))
                logger.warning(
                    "methodist_bottomup_validation_failed",
                    node_id=str(node.id),
                    error_type=first.get("type", "unknown"),
                    error_msg=first.get("msg", ""),
                    error_loc=loc,
                )
                feedback = (
                    f"{first.get('msg', 'validation error')} "
                    f"(field: {loc or '<root>'}). "
                    "Regenerate the response with valid JSON matching the schema."
                )
                raise StructuralRetryError(feedback) from exc
            if has_inputs:
                missing: list[str] = []
                if not result.title.strip():
                    missing.append("title")
                if not result.description.strip():
                    missing.append("description")
                if not result.compressed_summary.strip():
                    missing.append("compressed_summary")
                if missing:
                    logger.warning(
                        "methodist_bottomup_semantic_minimum_violated",
                        node_id=str(node.id),
                        empty_fields=missing,
                        own_documents=len(own_documents_payload),
                        children=len(children_compressed),
                    )
                    raise StructuralRetryError(
                        f"Required fields are empty: {missing}. The node has "
                        f"{len(own_documents_payload)} own documents and "
                        f"{len(children_compressed)} children — title, "
                        f"description, and compressed_summary MUST contain "
                        f"a synthesis of those inputs. Regenerate with "
                        f"non-empty values for the listed fields."
                    )
            parsed["result"] = result

        try:
            await self._stage_router.execute_for_stage(
                "methodist_bottomup",
                response_validator=_validator,
                expects_json=True,
                course_title=course_title,
                node_title=node.title,
                language=language,
                own_document_count=len(own_documents_payload),
                own_documents=own_documents_payload,
                child_count=len(children_compressed),
                children_compressed=children_compressed,
            )
        except LadderExhaustedError as exc:
            if self._all_attempts_are_input_budget(exc.attempts):
                raise MethodistInputBudgetExhaustedError(
                    node_id=node.id,
                    stage_name=exc.stage_name,
                    attempts=exc.attempts,
                ) from exc
            raise

        result = parsed["result"]

        # Compute deterministic fields (concept unions + counters).
        main_concepts, secondary_concepts = self._compute_concept_unions(
            own_docs, child_raws
        )
        own_documents_count, own_chars_count = self._compute_own_size(own_docs)
        (
            cumulative_documents_count,
            cumulative_chars_count,
        ) = self._compute_cumulative_size(
            own_documents_count, own_chars_count, child_raws
        )

        # Write canonical fields in place (KD16 hook contract).
        raw.title = result.title
        raw.description = result.description
        raw.learning_objectives = list(result.learning_objectives)
        raw.knowledge = [
            {"name": k.name, "description": k.description} for k in result.knowledge
        ]
        raw.skills = [
            {"name": s.name, "description": s.description} for s in result.skills
        ]
        raw.success_criteria = list(result.success_criteria)
        raw.assessment_approach = result.assessment_approach
        raw.teaching_approach = result.teaching_approach
        raw.key_activities = list(result.key_activities)
        raw.common_mistakes = list(result.common_mistakes)
        raw.compressed_summary = result.compressed_summary
        raw.methodist_observations = list(result.methodist_observations)
        raw.main_concepts = main_concepts
        raw.secondary_concepts = secondary_concepts
        raw.own_documents_count = own_documents_count
        raw.own_chars_count = own_chars_count
        raw.cumulative_documents_count = cumulative_documents_count
        raw.cumulative_chars_count = cumulative_chars_count

    async def generate_topdown(
        self,
        node: CourseNode,
        raw: NodeSummaryRaw,
        parent_raw: NodeSummaryRaw,
    ) -> None:
        """Pass 2 hook — DELIBERATELY NO-OP until Phase 3.2.3b.

        The orchestrator invokes this method for every non-root in-scope
        node on Pass 2. While 3.2.3a is the only methodist agent in the
        repo, returning without mutating ``raw`` keeps the orchestrator's
        traversal valid AND triggers
        ``orchestrator._encl_hash.update_source_hash(...)`` after this
        method returns — meaning ``enclosing_context_source_hash`` WILL
        be materialised even though ``enclosing_context`` stays NULL.

        That is a Phase 3.2.3a process risk (extension of DD-3.2.2-A
        to axis 2): once axis-2 source-hash is materialised on a Raw
        row, a future real Pass 2 invocation in 3.2.3b will memo-skip
        that node silently. The task spec §Архітектурні інваріанти
        therefore forbids any production-data orchestrator run that
        reaches Pass 2 before 3.2.3b merges. Live-smoke during 3.2.3a
        must call Pass 1 only (see ``tools/methodist_live_smoke.py``).
        """
        del node, raw, parent_raw  # explicit no-op

    # ── Internal helpers ───────────────────────────────────────────

    @staticmethod
    def _all_attempts_are_input_budget(
        attempts: list[tuple[str, str, str]],
    ) -> bool:
        """Return True when every ladder attempt's reason is an input-budget skip.

        The StageRouter skip reason format (Phase 3.2.3-pre) starts with
        ``"input budget exceeded:"``. When EVERY rung was skipped for
        that reason — never reached the LLM at all — the exhaustion is
        a domain-level "input too large", not an infrastructure failure.
        Any mixed signal (some rungs failed semantically / structurally
        / provider-side) keeps the original ``LadderExhaustedError``.
        """
        if not attempts:
            return False
        return all(r.startswith("input budget exceeded") for _, _, r in attempts)

    async def _fetch_own_ready_summaries(
        self, course_node_id: Any
    ) -> list[DocumentSummary]:
        """READY own AuthoredDocuments with their DocumentSummary attached.

        READY ≡ ``error_message IS NULL AND job_id IS NULL`` (mirrors
        the live ``MaterialState.READY`` property on ORM). Soft-deleted
        rows are excluded on both joins.
        """
        result = await self._session.execute(
            select(DocumentSummary)
            .join(
                AuthoredDocument,
                AuthoredDocument.id == DocumentSummary.authored_document_id,
            )
            .where(AuthoredDocument.course_node_id == course_node_id)
            .where(AuthoredDocument.deleted_at.is_(None))
            .where(AuthoredDocument.error_message.is_(None))
            .where(AuthoredDocument.job_id.is_(None))
            .where(DocumentSummary.deleted_at.is_(None))
        )
        return list(result.scalars().all())

    async def _fetch_children_raws(self, course_node_id: Any) -> list[dict[str, Any]]:
        """Children CourseNode + their NodeSummaryRaw row, ordered by id.

        KD10 leaf→root traversal guarantees every child's Raw row is
        committed by the time the orchestrator visits this node. A
        missing child Raw at this point is a **read contract violation**
        (orchestrator ordering bug, partial-commit race, or external
        soft-delete during walk). The agent raises
        :class:`Pass1ChildRawMissingError` — silently dropping the
        child would (a) deceive the LLM about the node's structure and
        (b) corrupt the cumulative_documents_count /
        cumulative_chars_count counters that the parent will then
        propagate upward. K2-catch in the orchestrator records the
        parent node as ERROR and continues the walk; the operator
        inspects ``errors[]`` to triage.

        A child whose Raw exists but whose ``compressed_summary`` is
        empty is a different case — legitimate at structurally trivial
        leaves (KD10 line 845). The agent passes an empty string
        through unchanged; the LLM sees the empty compressed_summary
        and synthesises from whatever input remains.
        """
        children_result = await self._session.execute(
            select(CourseNode)
            .where(CourseNode.parent_id == course_node_id)
            .where(CourseNode.deleted_at.is_(None))
            .order_by(CourseNode.id)
        )
        children = list(children_result.scalars().all())
        if not children:
            return []
        child_ids = [c.id for c in children]
        raws_result = await self._session.execute(
            select(NodeSummaryRaw)
            .where(NodeSummaryRaw.course_node_id.in_(child_ids))
            .where(NodeSummaryRaw.deleted_at.is_(None))
        )
        raws_by_node = {r.course_node_id: r for r in raws_result.scalars().all()}

        children_payload: list[dict[str, Any]] = []
        for child in children:
            child_raw = raws_by_node.get(child.id)
            if child_raw is None:
                raise Pass1ChildRawMissingError(
                    parent_node_id=course_node_id,
                    child_node_id=child.id,
                )
            children_payload.append(
                {
                    "title": child.title,
                    "compressed_summary": child_raw.compressed_summary or "",
                    "main_concepts": list(child_raw.main_concepts or []),
                    "secondary_concepts": list(child_raw.secondary_concepts or []),
                    "cumulative_documents_count": (
                        child_raw.cumulative_documents_count or 0
                    ),
                    "cumulative_chars_count": child_raw.cumulative_chars_count or 0,
                }
            )
        return children_payload

    async def _resolve_course_context(self, node: CourseNode) -> tuple[str, str | None]:
        """Resolve (course_title, display-name language) by walking to the root.

        ``default_language`` is owned by the root per Phase 2.4.13
        entry-gate (root-required CHECK constraint). Inner CourseNodes
        may carry a NULL default_language; agent walks ``parent_id``
        until ``parent_id IS NULL`` and uses that root for both title
        and language.

        Walk-depth defensive cap (25): cycles in the ``parent_id`` graph
        (data corruption) or pathologically deep trees would otherwise
        loop indefinitely. Unlike the orchestrator's
        ``_collect_course_nodes`` which only consumes ``tenant_id`` (the
        denormalised same value at every depth, so silent break on cap
        is harmless), this method consumes **semantic** root fields:
        a mid-chain "root" would feed the LLM the wrong course title and
        almost certainly a NULL ``default_language`` (the CHECK
        constraint only forces it on real roots). So when the cap fires
        while ``parent_id`` is still non-NULL, raise
        :class:`MethodistRootResolutionError` instead of silently using
        a mid-chain node. K2-catch in the orchestrator records this
        node as ERROR and continues the walk.
        """
        root = node
        depth = 0
        while root.parent_id is not None and depth < 25:
            parent = await self._session.get(CourseNode, root.parent_id)
            if parent is None or parent.deleted_at is not None:
                break
            root = parent
            depth += 1
        if root.parent_id is not None:
            raise MethodistRootResolutionError(
                node_id=node.id,
                last_mid_chain_node_id=root.id,
                depth_cap=25,
            )
        language = (
            display_name(root.default_language) if root.default_language else None
        )
        return root.title, language

    @staticmethod
    def _compute_concept_unions(
        own_docs: list[DocumentSummary],
        child_raws: list[dict[str, Any]],
    ) -> tuple[list[str], list[str]]:
        """Algorithmic concept union (KD-2.1-O extended to methodist tier).

        Combines own DocumentSummary main/secondary concepts with
        children's NodeSummaryRaw main/secondary concepts. Conflict
        rule mirrors ``ingestion/text.py`` Pass 2a: any concept that
        appears as ``main`` in any input stays in ``main_concepts``
        and is removed from ``secondary_concepts``. Both lists are
        sorted for deterministic order so the content_hash stays
        stable across re-runs.
        """
        main_set: set[str] = set()
        secondary_set: set[str] = set()
        for d in own_docs:
            main_set.update(d.main_concepts or [])
            secondary_set.update(d.secondary_concepts or [])
        for c in child_raws:
            main_set.update(c.get("main_concepts", []))
            secondary_set.update(c.get("secondary_concepts", []))
        secondary_set -= main_set
        return sorted(main_set), sorted(secondary_set)

    @staticmethod
    def _compute_own_size(own_docs: list[DocumentSummary]) -> tuple[int, int]:
        """Own document count + own char count from DocumentSummary inputs."""
        own_chars = sum((d.content_char_count or 0) for d in own_docs)
        return len(own_docs), own_chars

    @staticmethod
    def _compute_cumulative_size(
        own_documents_count: int,
        own_chars_count: int,
        child_raws: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Cumulative = own + sum(children.cumulative).

        Children already carry their own cumulative_* counts on their
        NodeSummaryRaw from the prior Pass 1 visit (KD10 leaf→root
        ordering guarantees this). The agent reads them as-is and adds
        the current node's own contribution.
        """
        total_docs = own_documents_count + sum(
            c.get("cumulative_documents_count", 0) for c in child_raws
        )
        total_chars = own_chars_count + sum(
            c.get("cumulative_chars_count", 0) for c in child_raws
        )
        return total_docs, total_chars
