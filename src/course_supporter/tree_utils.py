"""Utilities for traversing MaterialNode trees."""

from __future__ import annotations

import uuid
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from course_supporter.models.course import MaterialNodeSummary
    from course_supporter.storage.orm import MaterialNode


def flatten_subtree(root: MaterialNode) -> list[MaterialNode]:
    """BFS from root collecting all nodes (children must be populated).

    Args:
        root: Root node of the subtree with children eagerly loaded.

    Returns:
        Flat list of all nodes in the subtree, root first.
    """
    result: list[MaterialNode] = []
    queue: deque[MaterialNode] = deque([root])
    while queue:
        node = queue.popleft()
        result.append(node)
        queue.extend(node.children)
    return result


def find_node_bfs(
    roots: list[MaterialNode],
    target_id: uuid.UUID,
) -> MaterialNode | None:
    """BFS across roots to find a node by ID.

    Args:
        roots: Root-level nodes with children eagerly loaded.
        target_id: UUID of the node to find.

    Returns:
        The matching node or ``None`` if not found.
    """
    queue: deque[MaterialNode] = deque(roots)
    while queue:
        node = queue.popleft()
        if node.id == target_id:
            return node
        queue.extend(node.children)
    return None


def resolve_target_nodes(
    root_nodes: list[MaterialNode],
    course_id: uuid.UUID,
    node_id: uuid.UUID | None,
) -> tuple[MaterialNode | None, list[MaterialNode]]:
    """Resolve target node and flatten its subtree.

    Args:
        root_nodes: Root-level nodes from get_tree().
        course_id: Course UUID (for error messages).
        node_id: Target node UUID, or None for whole course.

    Returns:
        Tuple of (target_node_or_None, flat_node_list).

    Raises:
        NodeNotFoundError: If node_id is given but not found.
    """
    from course_supporter.errors import NodeNotFoundError

    if node_id is not None:
        target = find_node_bfs(root_nodes, node_id)
        if target is None:
            msg = f"Node {node_id} not found in course {course_id}"
            raise NodeNotFoundError(msg)
        return target, flatten_subtree(target)

    flat: list[MaterialNode] = []
    for rn in root_nodes:
        flat.extend(flatten_subtree(rn))
    return None, flat


def build_material_tree_summary(
    flat_nodes: list[MaterialNode],
) -> list[MaterialNodeSummary]:
    """Build tree summary with material titles for LLM context.

    Rebuilds the parent-child hierarchy from ``flat_nodes`` and collects
    filenames (or source URLs) from READY materials on each node.  The
    result is a lightweight Pydantic tree suitable for serialisation
    inside ``CourseContext``.

    Args:
        flat_nodes: Flat list of nodes from :func:`resolve_target_nodes`
            (root first, children eagerly loaded).

    Returns:
        List of root-level :class:`MaterialNodeSummary` nodes.
    """
    from course_supporter.models.course import MaterialNodeSummary
    from course_supporter.storage.orm import MaterialState

    node_ids = {n.id for n in flat_nodes}

    def _material_titles(node: MaterialNode) -> list[str]:
        titles: list[str] = []
        for entry in node.materials:
            if entry.state == MaterialState.READY and (
                title := (entry.filename or entry.source_url)
            ):
                titles.append(title)
        return titles

    def _node_to_summary(node: MaterialNode) -> MaterialNodeSummary:
        return MaterialNodeSummary(
            title=node.title,
            description=node.description,
            order=node.order,
            material_titles=_material_titles(node),
            children=[_node_to_summary(c) for c in node.children if c.id in node_ids],
        )

    roots = [
        n for n in flat_nodes if n.parent_id is None or n.parent_id not in node_ids
    ]
    return [_node_to_summary(r) for r in roots]


def serialize_tree_for_guided(
    flat_nodes: list[MaterialNode],
) -> str:
    """Serialize node tree into a nested JSON outline for guided-mode prompt.

    Rebuilds the parent-child hierarchy using each node's ``.children``
    relationship so the LLM can see which lessons belong to which modules.

    Args:
        flat_nodes: Flat list of nodes from resolve_target_nodes
            (root first, children eagerly loaded).

    Returns:
        JSON string representing the nested tree.
    """
    import json

    def _node_to_dict(node: MaterialNode) -> dict[str, object]:
        result: dict[str, object] = {
            "title": node.title,
            "description": node.description,
            "order": node.order,
        }
        if node.children:
            result["children"] = [_node_to_dict(c) for c in node.children]
        return result

    # Rebuild from roots: nodes without parent_id among flat_nodes
    root_ids = {n.id for n in flat_nodes}
    roots = [
        n for n in flat_nodes if n.parent_id is None or n.parent_id not in root_ids
    ]
    tree = [_node_to_dict(r) for r in roots]
    return json.dumps(tree, ensure_ascii=False, indent=2)
