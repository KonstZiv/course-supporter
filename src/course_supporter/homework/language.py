"""Language detection cascade for Mentor review responses.

Resolution order:
1. Explicit request parameter (response_language on submission)
2. Student preference (from last review interaction)
3. Auto-detect from submission content (comments, docstrings)
4. Course language (from MaterialNode description)
5. Fallback: "en"
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from course_supporter.models.safety import SubmissionContent
    from course_supporter.storage.orm import MaterialNode, Student

logger = structlog.get_logger()

# Common Unicode ranges for language hints
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_LATIN_RE = re.compile(r"[a-zA-Z]")

# Language-specific markers (frequent words in comments/text)
_LANG_MARKERS: dict[str, list[str]] = {
    "uk": [
        "функція",
        "змінна",
        "повертає",
        "виконує",
        "перевірка",
        "результат",
        "завдання",
        "клас",
        "метод",
        "масив",
        "рядок",
        "цикл",
    ],
    "ru": [
        "функция",
        "переменная",
        "возвращает",
        "выполняет",
        "проверка",
        "результат",
        "задание",
        "массив",
        "строка",
        "цикл",
    ],
    "de": [
        "Funktion",
        "Variable",
        "Ergebnis",
        "Aufgabe",
        "Klasse",
        "Methode",
        "Schleife",
        "Zeichenkette",
    ],
    "sr": [
        "функција",
        "променљива",
        "резултат",
        "задатак",
        "класа",
        "метода",
        "петља",
        "низ",
    ],
}

# Regex to extract comment text from common programming languages
_COMMENT_RE = re.compile(
    r"""
    (?://\s*(.+)$)        |  # C-style single line
    (?:\#\s*(.+)$)        |  # Python/shell
    (?:/\*\s*([\s\S]*?)\s*\*/) |  # C-style multiline
    (?:\"\"\"([\s\S]*?)\"\"\")  |  # Python docstring double
    (?:\'\'\'([\s\S]*?)\'\'\')     # Python docstring single
    """,
    re.MULTILINE | re.VERBOSE,
)


def _extract_human_text(content: str) -> str:
    """Extract comments and docstrings from code."""
    parts: list[str] = []
    for match in _COMMENT_RE.finditer(content):
        for group in match.groups():
            if group:
                parts.append(group.strip())
    return " ".join(parts)


def _detect_from_text(text: str) -> str | None:
    """Detect language from human-readable text using markers.

    Returns ISO 639-1 code or None if inconclusive.
    """
    if not text or len(text) < 10:
        return None

    text_lower = text.lower()

    scores: dict[str, int] = {}
    for lang, markers in _LANG_MARKERS.items():
        score = sum(1 for m in markers if m.lower() in text_lower)
        if score > 0:
            scores[lang] = score

    if not scores:
        # No markers matched — try script detection
        cyrillic_count = len(_CYRILLIC_RE.findall(text))
        latin_count = len(_LATIN_RE.findall(text))
        if cyrillic_count > latin_count and cyrillic_count > 5:
            return "uk"  # Default Cyrillic to Ukrainian (most likely for our context)
        if latin_count > 20:
            return "en"
        return None

    return max(scores, key=scores.get)  # type: ignore[arg-type]


def detect_response_language(
    *,
    request_language: str | None,
    student: Student | None,
    submission_content: SubmissionContent | None,
    course_node: MaterialNode | None,
) -> str:
    """Determine the language for Mentor review response.

    Cascade:
    1. Explicit request parameter
    2. Student preferred_language (from last interaction)
    3. Auto-detect from submission content
    4. Course node description language
    5. Fallback: "en"

    Args:
        request_language: Explicit language from API request.
        student: Student record (may have preferred_language).
        submission_content: Extracted submission for auto-detection.
        course_node: Course root node for fallback language.

    Returns:
        ISO 639-1 language code.
    """
    # 1. Explicit request
    if request_language:
        logger.debug("language_source", source="request", language=request_language)
        return request_language

    # 2. Student preference
    if student and student.preferred_language:
        logger.debug(
            "language_source",
            source="student_preference",
            language=student.preferred_language,
        )
        return student.preferred_language

    # 3. Auto-detect from submission
    if submission_content and submission_content.files:
        full_text = submission_content.full_text
        human_text = _extract_human_text(full_text)
        detected = _detect_from_text(human_text)
        if detected:
            logger.debug("language_source", source="auto_detect", language=detected)
            return detected

    # 4. Course language
    if course_node and course_node.description:
        detected = _detect_from_text(course_node.description)
        if detected:
            logger.debug(
                "language_source",
                source="course_node",
                language=detected,
            )
            return detected

    # 5. Fallback
    logger.debug("language_source", source="fallback", language="en")
    return "en"
