"""Subtree readiness check for structure generation.

Traverses a subtree from a given node and identifies materials
that are not yet ready (RAW, INTEGRITY_BROKEN) — blocking
structure generation for that scope.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from course_supporter.storage.orm import MaterialNode, MaterialState

#: States that block structure generation.
_STALE_STATES = frozenset({MaterialState.RAW, MaterialState.INTEGRITY_BROKEN})


@dataclass(frozen=True, slots=True)
class StaleMaterial:
    """A material entry that is not ready for structure generation."""

    entry_id: uuid.UUID
    filename: str | None
    state: MaterialState
    node_id: uuid.UUID
    node_title: str


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    """Result of a subtree readiness check."""

    ready: bool
    stale: list[StaleMaterial]


class ReadinessService:
    """Check whether a subtree is ready for structure generation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def check_subtree(self, node_id: uuid.UUID) -> ReadinessResult:
        """Check readiness of a subtree rooted at *node_id*.

        Loads all descendant nodes (including the root) with their
        materials via recursive CTE, then filters stale entries
        in Python.
        """
        nodes = await self._load_subtree(node_id)
        stale: list[StaleMaterial] = []
        for node in nodes:
            for entry in node.materials:
                if entry.state in _STALE_STATES:
                    stale.append(
                        StaleMaterial(
                            entry_id=entry.id,
                            filename=entry.filename,
                            state=entry.state,
                            node_id=node.id,
                            node_title=node.title,
                        )
                    )
        return ReadinessResult(ready=len(stale) == 0, stale=stale)

    async def _load_subtree(self, root_id: uuid.UUID) -> list[MaterialNode]:
        """Load root node and all descendants with materials eager-loaded.

        Uses a recursive CTE to find all descendant node IDs.
        """
        base = select(MaterialNode.id).where(MaterialNode.id == root_id)
        cte = base.cte(name="subtree", recursive=True)
        recursive = select(MaterialNode.id).join(
            cte, MaterialNode.parent_materialnode_id == cte.c.id
        )
        cte = cte.union_all(recursive)

        stmt = (
            select(MaterialNode)
            .where(MaterialNode.id.in_(select(cte.c.id)))
            .options(selectinload(MaterialNode.materials))
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
