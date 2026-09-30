"""Methodist full methodology against a real database.

Run with ``uv run pytest --run-db`` (PostgreSQL at the configured URL).

* ``MethodistAgent._fetch_own_documents``: each ready document with its role
  and task type; methodological documents and tasks carry their full text,
  rebuilt from active segments in position order; other educational
  documents carry none.
* ``scripts.reset_methodist_memo``: the preview changes nothing; the reset
  clears both memo keys on the subtree only.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from scripts.reset_methodist_memo import ResetError, apply_reset, plan_reset
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from course_supporter.agents.methodist import MethodistAgent
from course_supporter.config import get_settings
from course_supporter.storage.orm import (
    AuthoredDocument,
    CourseNode,
    DocumentSegment,
    DocumentSummary,
    NodeSummaryRaw,
    Tenant,
)
from tests._helpers.course_node_factory import make_root_course_node

pytestmark = pytest.mark.requires_db


def _document(
    root: CourseNode,
    *,
    order: int,
    role: str = "educational",
    task_type: str | None = None,
) -> AuthoredDocument:
    return AuthoredDocument(
        course_node_id=root.id,
        course_root_id=root.id,
        source_type="text",
        source_url=f"https://example.com/{uuid.uuid4().hex}",
        material_role=role,
        task_type=task_type,
        order=order,
    )


def _summary(doc: AuthoredDocument, title: str, **extra: Any) -> DocumentSummary:
    return DocumentSummary(
        authored_document_id=doc.id,
        course_root_id=doc.course_root_id,
        title=title,
        description=f"Опис: {title}.",
        main_concepts=[f"{title}-main"],
        secondary_concepts=[],
        content_char_count=0,
        **extra,
    )


def _segment(
    summary: DocumentSummary, *, order: int, start: int, content: str
) -> DocumentSegment:
    return DocumentSegment(
        document_summary_id=summary.id,
        course_root_id=summary.course_root_id,
        order=order,
        start_pos=start,
        end_pos=start + len(content),
        content=content,
        main_concepts=[],
        secondary_concepts=[],
    )


class TestFetchOwnDocuments:
    async def test_roles_task_types_and_full_text(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        guide = _document(seed_root_node, order=2, role="methodological")
        task = _document(seed_root_node, order=1, task_type="task")
        lecture = _document(seed_root_node, order=0)
        db_session.add_all([guide, task, lecture])
        await db_session.flush()
        guide_s = _summary(guide, "методичка")
        task_s = _summary(task, "завдання")
        lecture_s = _summary(lecture, "конспект")
        db_session.add_all([guide_s, task_s, lecture_s])
        await db_session.flush()
        deleted = _segment(guide_s, order=3, start=40, content="ВИДАЛЕНО")
        deleted.deleted_at = seed_root_node.created_at
        db_session.add_all(
            [
                # Inserted out of position order on purpose.
                _segment(guide_s, order=1, start=12, content="друга частина."),
                _segment(guide_s, order=0, start=0, content="Перша частина, "),
                deleted,
                _segment(task_s, order=0, start=0, content="Умова завдання."),
                _segment(lecture_s, order=0, start=0, content="Текст лекції."),
            ]
        )
        await db_session.flush()

        agent = MethodistAgent(session=db_session, stage_router=MagicMock())
        docs = await agent._fetch_own_documents(seed_root_node.id)

        assert [d.title for d in docs] == ["конспект", "завдання", "методичка"]
        by_title = {d.title: d for d in docs}
        assert by_title["методичка"].is_methodological
        assert by_title["методичка"].full_text == "Перша частина, друга частина."
        assert by_title["завдання"].task_type == "task"
        assert by_title["завдання"].full_text == "Умова завдання."
        assert by_title["конспект"].full_text is None
        assert by_title["конспект"].main_concepts == ["конспект-main"]

    async def test_not_ready_document_left_out(
        self, db_session: AsyncSession, seed_root_node: CourseNode
    ) -> None:
        failed = _document(seed_root_node, order=0, role="methodological")
        failed.error_message = "boom"
        db_session.add(failed)
        await db_session.flush()
        db_session.add(_summary(failed, "зламана"))
        await db_session.flush()

        agent = MethodistAgent(session=db_session, stage_router=MagicMock())

        assert await agent._fetch_own_documents(seed_root_node.id) == []


# ── The reset script ──────────────────────────────────────────────


@pytest.fixture()
def sync_session() -> Iterator[Session]:
    """A sync session inside an outer transaction that is rolled back.

    The script commits; ``create_savepoint`` turns that commit into a
    savepoint release, so nothing outlives the test.
    """
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        trans = conn.begin()
        session = Session(bind=conn, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            trans.rollback()
    engine.dispose()


def _tree(session: Session) -> dict[str, CourseNode]:
    """root → (block → lesson), other; each with a Raw carrying both keys."""
    tenant = Tenant(name=f"reset-{uuid.uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    root = make_root_course_node(tenant_id=tenant.id, title="Курс", order=0)
    session.add(root)
    session.flush()
    block = CourseNode(tenant_id=tenant.id, parent_id=root.id, title="Блок", order=0)
    other = CourseNode(tenant_id=tenant.id, parent_id=root.id, title="Інший", order=1)
    session.add_all([block, other])
    session.flush()
    lesson = CourseNode(
        tenant_id=tenant.id, parent_id=block.id, title="Заняття", order=0
    )
    session.add(lesson)
    session.flush()
    nodes = {"root": root, "block": block, "other": other, "lesson": lesson}
    for name, node in nodes.items():
        session.add(
            NodeSummaryRaw(
                course_node_id=node.id,
                source_content_hash=f"content-{name}",
                enclosing_context_source_hash=f"context-{name}",
            )
        )
    session.flush()
    return nodes


def _keys(session: Session, node: CourseNode) -> tuple[str | None, str | None]:
    raw = session.execute(
        select(NodeSummaryRaw).where(NodeSummaryRaw.course_node_id == node.id)
    ).scalar_one()
    session.refresh(raw)
    return raw.source_content_hash, raw.enclosing_context_source_hash


class TestResetScript:
    def test_preview_changes_nothing(self, sync_session: Session) -> None:
        nodes = _tree(sync_session)

        plan = plan_reset(sync_session, nodes["block"].id)

        assert plan.course_root.id == nodes["root"].id
        assert [n.title for n in plan.nodes] == ["Блок", "Заняття"]
        assert len(plan.to_reset) == 2
        for name in nodes:
            assert _keys(sync_session, nodes[name]) == (
                f"content-{name}",
                f"context-{name}",
            )

    def test_reset_touches_only_the_subtree(self, sync_session: Session) -> None:
        nodes = _tree(sync_session)

        changed = apply_reset(sync_session, plan_reset(sync_session, nodes["block"].id))

        assert changed == 2
        assert _keys(sync_session, nodes["block"]) == (None, None)
        assert _keys(sync_session, nodes["lesson"]) == (None, None)
        assert _keys(sync_session, nodes["root"]) == ("content-root", "context-root")
        assert _keys(sync_session, nodes["other"]) == (
            "content-other",
            "context-other",
        )
        # A second run finds nothing left to reset.
        assert (
            apply_reset(sync_session, plan_reset(sync_session, nodes["block"].id)) == 0
        )

    def test_unknown_node_is_an_error(self, sync_session: Session) -> None:
        with pytest.raises(ResetError, match="not found"):
            plan_reset(sync_session, uuid.uuid4())

    def test_node_of_another_tenant_in_subtree_is_an_error(
        self, sync_session: Session
    ) -> None:
        nodes = _tree(sync_session)
        stranger = Tenant(name=f"stranger-{uuid.uuid4().hex[:8]}")
        sync_session.add(stranger)
        sync_session.flush()
        sync_session.add(
            CourseNode(
                tenant_id=stranger.id,
                parent_id=nodes["lesson"].id,
                title="Чужий",
                order=0,
            )
        )
        sync_session.flush()

        with pytest.raises(ResetError, match="another tenant"):
            plan_reset(sync_session, nodes["block"].id)
