"""Material tree node management API endpoints.

Provides CRUD operations for the hierarchical material tree.
Root nodes (parent_id IS NULL) serve as top-level entities (courses).
Tenant isolation is enforced by verifying node ownership via tenant_id.

Routes
------
- ``POST   /nodes``                           — Create root node (= course)
- ``GET    /nodes``                           — List root nodes (paginated)
- ``POST   /nodes/{nid}/children``            — Create child node
- ``GET    /nodes/{nid}/tree``                — Get full subtree
- ``GET    /nodes/{nid}/detail``              — Get subtree with materials
- ``GET    /nodes/{nid}``                     — Get single node
- ``PATCH  /nodes/{nid}``                     — Update node
- ``POST   /nodes/{nid}/move``                — Move node (reparent)
- ``POST   /nodes/{nid}/reorder``             — Reorder among siblings
- ``DELETE /nodes/{nid}``                     — Delete node (cascade)
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.api.deps import get_arq_redis, get_s3_client, get_session
from course_supporter.api.schemas import (
    ChildNodeCreateRequest,
    NodeListResponse,
    NodeMoveRequest,
    NodeReorderRequest,
    NodeResponse,
    NodeTreeResponse,
    NodeUpdateRequest,
    NodeWithDocumentsResponse,
    RootNodeCreateRequest,
)
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.jobs.cancellation_service import JobCancellationService
from course_supporter.services.s3_cleanup_orchestration import enqueue_s3_cleanup
from course_supporter.storage.cascade import CascadeDeleteService, build_cascade_map
from course_supporter.storage.content_hash import ContentHashService
from course_supporter.storage.course_node_repository import (
    CourseNodeRepository,
    SummaryStatus,
)
from course_supporter.storage.orm import CourseNode
from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger()

router = APIRouter(tags=["nodes"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]
S3Dep = Annotated[S3Client, Depends(get_s3_client)]
ArqDep = Annotated[ArqRedis, Depends(get_arq_redis)]
PrepDep = Annotated[TenantContext, Depends(require_scope(AuthScope.PREP))]
SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]


def _node_response(node: CourseNode) -> NodeResponse:
    """Build NodeResponse with computed children_count and authored_documents_count."""
    resp = NodeResponse.model_validate(node)
    resp.children_count = len(node.children) if node.children else 0
    # Soft-deleted documents stay on the relationship for cascade/audit
    # (Phase 1 KD3 contract) — exclude them from the user-facing count.
    active_documents = (
        [d for d in node.documents if d.deleted_at is None] if node.documents else []
    )
    resp.authored_documents_count = len(active_documents)
    return resp


def _collect_node_ids(node: NodeWithDocumentsResponse) -> list[uuid.UUID]:
    """Flatten the response tree to its CourseNode ids (Task 3.2.5b)."""
    ids = [node.id]
    for child in node.children:
        ids.extend(_collect_node_ids(child))
    return ids


def _apply_summary_states(
    node: NodeWithDocumentsResponse,
    states: dict[uuid.UUID, tuple[SummaryStatus, bool]],
) -> None:
    """Assign the batch-computed summary badge state onto the response tree.

    Mirrors the post-``model_validate`` field-set pattern of
    :func:`_node_response`, extended recursively. Pure Python O(n) walk —
    no per-node query (the single batch ``SELECT`` already ran). Nodes
    absent from ``states`` keep the schema defaults.
    """
    state = states.get(node.id)
    if state is not None:
        node.summary_status, node.materials_changed = state
    for child in node.children:
        _apply_summary_states(child, states)


async def _require_node_for_tenant(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
) -> object:
    """Verify the node exists and belongs to the tenant.

    Raises:
        HTTPException 404: If the node is not found or
            does not belong to the authenticated tenant.
    """
    repo = CourseNodeRepository(session)
    node = await repo.get_by_id(node_id)
    if node is None or node.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Node not found")
    return node


async def _require_child_node(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    node_id: uuid.UUID,
) -> object:
    """Verify a child node exists and belongs to the same tenant.

    Same as _require_node_for_tenant but with a clearer name for
    sub-node operations (slide-mapping, materials).
    """
    return await _require_node_for_tenant(session, tenant_id, node_id)


# ── Root node (= course) CRUD ──


@router.post("/nodes", status_code=201)
async def create_root_node(
    body: RootNodeCreateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Create a root node (course) in the material tree.

    Root nodes have no parent and appear at the top level.
    The ``order`` is auto-assigned as the next available position.

    ``default_language`` is required — see ``RootNodeCreateRequest``.
    The validator normalizes any standard input form to canonical ISO 639-3
    before the DB write.
    """
    repo = CourseNodeRepository(session)
    node = await repo.create(
        tenant_id=tenant.tenant_id,
        title=body.title,
        description=body.description,
        default_language=body.default_language,
    )
    await session.commit()

    logger.info(
        "root_node_created",
        node_id=str(node.id),
        tenant_id=str(tenant.tenant_id),
    )
    return NodeResponse.model_validate(node)


@router.get("/nodes")
async def list_root_nodes(
    tenant: SharedDep,
    session: SessionDep,
    limit: int = Query(
        default=20,
        ge=1,
        le=100,
        description="Maximum number of root nodes to return (1-100).",
    ),
    offset: int = Query(
        default=0,
        ge=0,
        description="Number of root nodes to skip for pagination.",
    ),
) -> NodeListResponse:
    """List root nodes (courses) for the authenticated tenant.

    Returns a paginated list sorted by creation date (newest first).
    """
    repo = CourseNodeRepository(session)
    roots = await repo.list_roots(tenant.tenant_id, limit=limit, offset=offset)
    total = await repo.count_roots(tenant.tenant_id)
    return NodeListResponse(
        items=[_node_response(r) for r in roots],
        total=total,
        limit=limit,
        offset=offset,
    )


# ── Child node CRUD ──


@router.post("/nodes/{node_id}/children", status_code=201)
async def create_child_node(
    node_id: uuid.UUID,
    body: ChildNodeCreateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Create a child node under an existing parent.

    The child inherits the tenant from the parent. The ``order``
    is auto-assigned as the next available position among siblings.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    repo = CourseNodeRepository(session)

    node = await repo.create(
        tenant_id=tenant.tenant_id,
        parent_id=node_id,
        title=body.title,
        description=body.description,
        default_language=body.default_language,
    )
    await session.commit()

    logger.info(
        "child_node_created",
        node_id=str(node.id),
        parent_id=str(node_id),
    )
    return NodeResponse.model_validate(node)


# ── Tree operations ──


@router.get("/nodes/{node_id}/tree")
async def get_tree(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> list[NodeTreeResponse]:
    """Get the full subtree rooted at a node.

    Returns all nodes in a nested structure, with children
    recursively populated. Each level is sorted by ``order``.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = CourseNodeRepository(session)
    roots = await repo.get_subtree(node_id)
    return [NodeTreeResponse.model_validate(r) for r in roots]


@router.get("/nodes/{node_id}/detail")
async def get_node_detail(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> NodeWithDocumentsResponse:
    """Get the full subtree with materials attached to each node.

    Returns the hierarchical view including materials at each level
    and their lifecycle states. Includes a course-level fingerprint.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)

    repo = CourseNodeRepository(session)
    # User-facing endpoint — filter soft-deleted documents at the loader
    # so the response never exposes them (Phase 1 KD3 user-facing
    # contract; hotfix-7 regression H).
    tree_roots = await repo.get_subtree_with_active_documents(node_id)
    if not tree_roots:
        raise HTTPException(status_code=404, detail="Node not found")
    response = NodeWithDocumentsResponse.model_validate(tree_roots[0])

    # Summary badge state (Task 3.2.5b): one batch query for the whole
    # subtree, then a pure recursive walk to attach it — no N+1.
    node_ids = _collect_node_ids(response)
    states = await repo.fetch_summary_states(node_ids)
    _apply_summary_states(response, states)
    return response


# ── Single node operations ──


@router.get("/nodes/{node_id}")
async def get_node(
    node_id: uuid.UUID,
    tenant: SharedDep,
    session: SessionDep,
) -> NodeResponse:
    """Get a single node by ID.

    Returns the flat node representation without children.
    Use ``GET /nodes/{id}/tree`` for the nested tree.
    """
    node = await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    return NodeResponse.model_validate(node)


@router.patch("/nodes/{node_id}")
async def update_node(
    node_id: uuid.UUID,
    body: NodeUpdateRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Update a node's title, description, and/or default language.

    Only fields present in the request body are updated.
    To clear a field, send it as ``null``. Updating
    ``default_language`` is a pure DB write — it does not trigger
    re-ingestion of existing materials. **Root nodes** cannot have
    ``default_language`` cleared — the request is rejected with 422
    so the CHECK constraint never fires as a 500.
    """
    node = await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    repo = CourseNodeRepository(session)

    # Distinguish "field omitted" from "field set to null"
    update_kwargs: dict[str, str | None] = {}
    if "title" in body.model_fields_set:
        update_kwargs["title"] = body.title
    if "description" in body.model_fields_set:
        update_kwargs["description"] = body.description
    if "default_language" in body.model_fields_set:
        # Root nodes carry the course language; clearing it would violate
        # ``course_nodes_root_language_required`` (a 500 from the DB).
        # Reject at the route boundary with a clear 422 instead.
        if body.default_language is None and getattr(node, "parent_id", None) is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "default_language cannot be cleared on a root node "
                    "(course language is required)."
                ),
            )
        update_kwargs["default_language"] = body.default_language

    node = await repo.update(node_id, **update_kwargs)
    await session.commit()

    logger.info("node_updated", node_id=str(node_id), fields=list(update_kwargs))
    return NodeResponse.model_validate(node)


@router.post("/nodes/{node_id}/move")
async def move_node(
    node_id: uuid.UUID,
    body: NodeMoveRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Move a node to a new parent (or to root).

    Cycle detection is enforced. Set ``parent_id`` to ``null``
    to make the node a root. Returns 422 if the move would create a cycle.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    repo = CourseNodeRepository(session)

    # Validate target parent belongs to the same tenant
    if body.parent_id is not None:
        await _require_node_for_tenant(session, tenant.tenant_id, body.parent_id)

    try:
        node = await repo.move(node_id, body.parent_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await session.commit()

    logger.info(
        "node_moved",
        node_id=str(node_id),
        new_parent_id=str(body.parent_id),
    )
    return NodeResponse.model_validate(node)


@router.post("/nodes/{node_id}/reorder")
async def reorder_node(
    node_id: uuid.UUID,
    body: NodeReorderRequest,
    tenant: PrepDep,
    session: SessionDep,
) -> NodeResponse:
    """Reorder a node among its siblings.

    The target ``order`` is 0-based. If the value exceeds the
    number of siblings, it is clamped to the last position.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    repo = CourseNodeRepository(session)

    try:
        node = await repo.reorder(node_id, body.order)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await session.commit()

    logger.info(
        "node_reordered",
        node_id=str(node_id),
        new_order=body.order,
    )
    return NodeResponse.model_validate(node)


@router.delete("/nodes/{node_id}", status_code=204)
async def delete_node(
    node_id: uuid.UUID,
    tenant: PrepDep,
    session: SessionDep,
    s3: S3Dep,
    arq: ArqDep,
) -> None:
    """KD3 soft-delete the node and its full subtree (vision §3 KD3).

    Cascade flow per Phase 1 KD3 adoption:

    1. Verify the node exists and belongs to the caller's tenant
       (404 otherwise — same shape as the rest of the route).
    2. Walk the subtree via ``get_subtree(include_documents=True,
       tenant_id=...)`` and collect S3 keys from every descendant
       AuthoredDocument's ``source_url`` BEFORE cascade fires.
       Order matters: the cascade engine dispatches
       ``scrub_authored_document`` per descendant which sets
       ``source_url = ''`` — collecting keys after cascade would
       miss them all.
    3. Issue ``CascadeDeleteService.soft_delete_with_cascade`` rooted
       at the node. The engine drives the four-phase hook chain
       (cancel → invalidate → scrub → write deleted_at → flush) plus
       per-victim scrub dispatch:

       * ``on_cancel_jobs`` — direct-bind to
         :class:`JobCancellationService.cancel_jobs_for_entities`
         per vision §KD13. Subtree victim ids are course_node_id-keyed
         (cascade traverses ``CourseNode → CourseNode → AuthoredDocument``
         so the victim list includes the full subtree CourseNode set),
         matching the JCS ``Job.course_node_id`` IN lookup path
         directly — no closure augmentation needed (the augmentation
         pattern in ``documents.py`` + ``storage.py`` is reserved for
         delete sites where victim ids are document-keyed). KD13 cancel
         semantics: ``status='cancelled'`` + ``completed_at = now()``;
         ``deleted_at`` REMAINS NULL (Job ∉ any
         ``__cascades_soft_delete_to__`` chain per PHASE.md §1.2 audit).
       * ``on_invalidate_hashes`` — Gap 3 hook bridging
         :class:`ContentHashService.invalidate_subtree` so parent-hash
         recompute treats victims as already gone.
       * Class-level ``__scrub_callable__`` dispatch (models-fix-3)
         clears KD3 fields per type: CourseNode →
         ``scrub_course_node`` (``title`` + ``description`` ← KD3
         marker per hotfix-4); AuthoredDocument →
         ``scrub_authored_document`` (``filename`` + ``source_url`` ←
         KD3 marker).
    4. Hand off to ``enqueue_s3_cleanup`` — helper persists a
       ``s3_cleanup`` Job row, commits cascade + Job atomically
       (QQ5 boundary), then dispatches the ARQ task post-commit
       and records the resolved ``arq_job_id``. Empty key list
       short-circuits — no Job row, no ARQ enqueue.
    """
    await _require_node_for_tenant(session, tenant.tenant_id, node_id)
    node_repo = CourseNodeRepository(session)

    # (2) Collect S3 keys before cascade scrub blanks ``source_url``.
    # Tenant-scoped subtree (Gap 1 — commit (j)) defends against any
    # corrupt parent_id chain pulling foreign-tenant docs into the
    # cleanup batch. ``get_subtree_with_active_documents`` eager-loads
    # only active (``deleted_at IS NULL``) AuthoredDocuments per node
    # — already-deleted docs have already been scrub-cleaned + their
    # S3 keys enqueued via their own ``delete_document`` path, so
    # re-collecting their keys here would either no-op (extract_key
    # returns None on the KD3 marker) or duplicate cleanup work.
    # Active-only filter keeps the batch precisely what cascade-scrub
    # is about to blank in the same flush.
    subtree = await node_repo.get_subtree_with_active_documents(
        node_id,
        tenant_id=tenant.tenant_id,
    )
    file_keys: list[str] = []
    for node in _flatten(subtree):
        for doc in node.documents:
            key = s3.extract_key(doc.source_url)
            if key is not None:
                file_keys.append(key)

    # (3) Cascade soft-delete. Class-level ``__scrub_callable__``
    # dispatch (models-fix-3) clears KD3 fields on root + every
    # descendant in the same flush as the ``deleted_at`` write.
    cascade_service = CascadeDeleteService(session)
    cascade_map = build_cascade_map(CourseNode)
    content_hash_service = ContentHashService(session)
    job_cancellation_service = JobCancellationService(session)

    async def invalidate_hook(
        ids: list[uuid.UUID], exclude_ids: set[uuid.UUID]
    ) -> None:
        await content_hash_service.invalidate_subtree(ids, exclude_ids=exclude_ids)

    # ``subtree[0]`` is the loaded root with eager-loaded relationships
    # — reuse it as the cascade entry point so we don't issue another
    # ``SELECT`` for the same row. Empty subtree should never reach
    # here (``_require_node_for_tenant`` already failed if so).
    root_node = subtree[0]
    await cascade_service.soft_delete_with_cascade(
        root_node,
        cascade_map,
        on_cancel_jobs=job_cancellation_service.cancel_jobs_for_entities,
        on_invalidate_hashes=invalidate_hook,
    )

    # (4) Persist the s3_cleanup Job + dispatch ARQ task. Helper owns
    # the QQ5 commit boundary; we pass ``course_node_id=node_id`` so
    # tenant scope on the Job row is recoverable via the
    # ``Job.course_node_id → CourseNode.tenant_id`` join even after
    # the cascade scrub clears the row's content fields.
    s3_files_cleaned = len(file_keys)
    if file_keys:
        await enqueue_s3_cleanup(
            session=session,
            arq=arq,
            file_keys=file_keys,
            tenant_id=tenant.tenant_id,
            course_node_id=node_id,
        )
    else:
        # No S3 keys to clean (pure-text/web subtree or leaf with no
        # documents) — still need to commit the cascade soft-delete
        # since the helper would have done it for us otherwise.
        await session.commit()

    logger.info(
        "node_deleted",
        node_id=str(node_id),
        s3_files_cleaned=s3_files_cleaned,
    )


def _flatten(nodes: Sequence[CourseNode]) -> list[CourseNode]:
    """Flatten a tree of nodes into a flat list."""
    result: list[CourseNode] = []
    stack = list(nodes)
    while stack:
        node = stack.pop()
        result.append(node)
        stack.extend(node.children)
    return result
