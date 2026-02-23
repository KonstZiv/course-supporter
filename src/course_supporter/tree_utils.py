"""Utilities for traversing MaterialNode trees."""

from __future__ import annotations

import uuid
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
