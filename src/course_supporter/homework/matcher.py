"""Task matcher — identifies which course task(s) a homework submission addresses.

Scenarios:
- One file → one task
- One file → multiple tasks (same node)
- Multiple files → multiple tasks (same node)
- Many files/folders → one project-level task
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, cast

import structlog

from course_supporter.config import get_settings
from course_supporter.models.matching import (
    LLMMatchResponse,
    MatchResult,
    TaskMatch,
)
from course_supporter.security.schemas import SubmissionContent

if TYPE_CHECKING:
    from course_supporter.llm.router import ModelRouter
    from course_supporter.storage.orm import StructureNodeEditable

logger = structlog.get_logger()

_PROMPT_PATH = "prompts/task_matcher/v1.yaml"

# Node types that represent assignable tasks
_TASK_NODE_TYPES = frozenset({"exercise", "concept", "lesson"})


class TaskMatchError(Exception):
    """Raised when task matching fails."""


class TaskMatcher:
    """Matches homework submissions to course tasks via LLM.

    Collects task-like nodes from the editable tree, sends them
    along with the submission content to the LLM, and returns
    structured match results.
    """

    async def match(
        self,
        content: SubmissionContent,
        editable_nodes: list[StructureNodeEditable],
        router: ModelRouter,
        *,
        task_hint_id: uuid.UUID | None = None,
    ) -> MatchResult:
        """Match submission content to course tasks.

        Args:
            content: Extracted submission content.
            editable_nodes: All editable nodes for the course.
            router: ModelRouter for LLM calls.
            task_hint_id: Optional hint — if provided, validated first.

        Returns:
            MatchResult with matched tasks.

        Raises:
            TaskMatchError: If no tasks found or matching fails.
        """
        # Collect task-like nodes
        task_nodes = self._collect_tasks(editable_nodes)

        if not task_nodes:
            msg = "No task nodes found in course structure"
            raise TaskMatchError(msg)

        from collections import Counter

        type_counts = Counter(n.node_type for n in task_nodes)
        logger.debug(
            "task_matcher_tasks_collected",
            total=len(task_nodes),
            types=dict(type_counts),
        )

        # If hint provided, validate it exists in task list
        if task_hint_id is not None:
            hint_node = next((n for n in task_nodes if n.id == task_hint_id), None)
            if hint_node is not None:
                logger.info(
                    "task_hint_validated",
                    hint_id=str(task_hint_id),
                    title=hint_node.title,
                )

        # Build task catalog for LLM
        task_catalog = self._build_catalog(task_nodes)

        # LLM matching
        llm_result = await self._llm_match(content, task_catalog, task_nodes, router)

        # Convert LLM indices to real node IDs
        matches: list[TaskMatch] = []
        for lm in llm_result.matches:
            if not (0 <= lm.task_index < len(task_nodes)):
                logger.warning(
                    "task_matcher_invalid_index",
                    index=lm.task_index,
                    valid_range=f"0-{len(task_nodes) - 1}",
                )
                continue
            node = task_nodes[lm.task_index]
            matches.append(
                TaskMatch(
                    node_id=node.id,
                    task_title=node.title or "",
                    task_type=node.node_type or "",
                    confidence=lm.confidence,
                    section_hint=lm.section_hint,
                )
            )

        if not matches:
            msg = (
                f"LLM could not match submission to any task. "
                f"Explanation: {llm_result.explanation}"
            )
            raise TaskMatchError(msg)

        # Sort by confidence desc
        matches.sort(key=lambda m: m.confidence, reverse=True)

        result = MatchResult(
            matches=matches,
            primary_task_id=matches[0].node_id,
            explanation=llm_result.explanation,
        )

        logger.info(
            "task_matching_done",
            matches_count=len(matches),
            primary_task=matches[0].task_title,
            primary_confidence=matches[0].confidence,
        )

        return result

    def _collect_tasks(
        self,
        nodes: list[StructureNodeEditable],
    ) -> list[StructureNodeEditable]:
        """Filter nodes to task-assignable types."""
        return [
            n
            for n in nodes
            if n.node_type in _TASK_NODE_TYPES or self._has_tasks_in_method(n)
        ]

    def _has_tasks_in_method(self, node: StructureNodeEditable) -> bool:
        """Check if methodological_content contains task definitions."""
        mc = node.methodological_content
        if not mc or not isinstance(mc, dict):
            return False
        tasks = mc.get("tasks", [])
        return bool(tasks)

    def _build_catalog(
        self,
        task_nodes: list[StructureNodeEditable],
    ) -> str:
        """Build a numbered task catalog string for the LLM prompt."""
        lines: list[str] = []
        for idx, node in enumerate(task_nodes):
            entry: dict[str, object] = {
                "index": idx,
                "title": node.title or "",
                "type": node.node_type or "",
                "description": node.description or "",
            }
            if node.learning_goal:
                entry["learning_goal"] = node.learning_goal
            if node.success_criteria:
                entry["success_criteria"] = node.success_criteria

            # Include methodological tasks if available
            mc = node.methodological_content
            if mc and isinstance(mc, dict):
                tasks = mc.get("tasks", [])
                if tasks:
                    entry["methodological_tasks"] = tasks

            lines.append(json.dumps(entry, ensure_ascii=False))

        return "\n".join(lines)

    async def _llm_match(
        self,
        content: SubmissionContent,
        task_catalog: str,
        task_nodes: list[StructureNodeEditable],
        router: ModelRouter,
    ) -> LLMMatchResponse:
        """Call LLM to match submission to tasks."""
        from course_supporter.agents.prompt_loader import (
            format_user_prompt,
            load_prompt,
        )

        prompt_data = load_prompt(_PROMPT_PATH)

        # Truncate content for LLM
        full_text = content.full_text
        settings = get_settings()
        max_chars = settings.homework_max_content_chars
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n[... truncated ...]"

        user_prompt = format_user_prompt(
            prompt_data.user_prompt_template,
            full_text,
            task_catalog=task_catalog,
            tasks_count=str(len(task_nodes)),
        )

        match_data, response = await router.complete_structured(
            action="task_matching",
            prompt=user_prompt,
            response_schema=LLMMatchResponse,
            system_prompt=prompt_data.system_prompt,
            temperature=0.0,
        )

        logger.info(
            "task_matcher_llm_call",
            model=response.model_id,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            cost_usd=response.cost_usd,
        )

        return cast(LLMMatchResponse, match_data)
