"""Convert LLM CourseStructure output into StructureNode ORM objects."""

from __future__ import annotations

import uuid

from course_supporter.models.course import CourseStructure
from course_supporter.storage.orm import StructureNode, StructureNodeType, _uuid7


def convert_to_structure_nodes(
    structure: CourseStructure,
    snapshot_id: uuid.UUID,
) -> list[StructureNode]:
    """Flatten CourseStructure hierarchy into a list of StructureNode ORM objects.

    Traverses Module → Lesson → Concept/Exercise, assigning
    correct parent-child IDs and order values.

    Args:
        structure: Parsed LLM output (CourseStructure Pydantic model).
        snapshot_id: The StructureSnapshot these nodes belong to.

    Returns:
        Flat list of StructureNode objects, parents before children.
    """
    nodes: list[StructureNode] = []

    for mod_idx, module in enumerate(structure.modules):
        mod_id = _uuid7()
        nodes.append(
            StructureNode(
                id=mod_id,
                structuresnapshot_id=snapshot_id,
                parent_structurenode_id=None,
                node_type=StructureNodeType.MODULE,
                order=mod_idx,
                title=module.title,
                description=module.description or None,
                learning_goal=module.learning_goal or None,
                expected_knowledge=(
                    [{"summary": k, "details": ""} for k in module.expected_knowledge]
                    if module.expected_knowledge
                    else None
                ),
                expected_skills=(
                    [{"summary": s, "details": ""} for s in module.expected_skills]
                    if module.expected_skills
                    else None
                ),
                difficulty=module.difficulty,
            )
        )

        for les_idx, lesson in enumerate(module.lessons):
            les_id = _uuid7()
            nodes.append(
                StructureNode(
                    id=les_id,
                    structuresnapshot_id=snapshot_id,
                    parent_structurenode_id=mod_id,
                    node_type=StructureNodeType.LESSON,
                    order=les_idx,
                    title=lesson.title,
                    timecodes=_build_timecodes(lesson),
                    slide_references=_build_slide_refs(lesson),
                )
            )

            for con_idx, concept in enumerate(lesson.concepts):
                nodes.append(
                    StructureNode(
                        id=_uuid7(),
                        structuresnapshot_id=snapshot_id,
                        parent_structurenode_id=les_id,
                        node_type=StructureNodeType.CONCEPT,
                        order=con_idx,
                        title=concept.title,
                        description=concept.definition,
                        key_concepts=(
                            [{"summary": concept.title, "details": concept.definition}]
                        ),
                        timecodes=(
                            [{"timecode": t} for t in concept.timecodes]
                            if concept.timecodes
                            else None
                        ),
                        slide_references=(
                            [{"slide": s} for s in concept.slide_references]
                            if concept.slide_references
                            else None
                        ),
                        web_references=(
                            [r.model_dump() for r in concept.web_references]
                            if concept.web_references
                            else None
                        ),
                    )
                )

            for ex_idx, exercise in enumerate(lesson.exercises):
                nodes.append(
                    StructureNode(
                        id=_uuid7(),
                        structuresnapshot_id=snapshot_id,
                        parent_structurenode_id=les_id,
                        node_type=StructureNodeType.EXERCISE,
                        order=ex_idx,
                        title=f"Exercise {ex_idx + 1}",
                        description=exercise.description,
                        difficulty=_map_difficulty_level(exercise.difficulty_level),
                        success_criteria=exercise.grading_criteria,
                    )
                )

    return nodes


def _build_timecodes(
    lesson: object,
) -> list[dict[str, str]] | None:
    """Extract timecode range from LessonOutput."""
    start = getattr(lesson, "video_start_timecode", None)
    end = getattr(lesson, "video_end_timecode", None)
    if start:
        return [{"start": start, "end": end or ""}]
    return None


def _build_slide_refs(
    lesson: object,
) -> list[dict[str, int]] | None:
    """Extract slide range from LessonOutput."""
    slide_range = getattr(lesson, "slide_range", None)
    if slide_range:
        return [{"start": slide_range.start, "end": slide_range.end}]
    return None


def _map_difficulty_level(level: int) -> str:
    """Map numeric difficulty (1-5) to string."""
    if level <= 2:
        return "easy"
    if level <= 3:
        return "medium"
    return "hard"
