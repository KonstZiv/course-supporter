"""Regex patterns for prompt injection detection."""

from __future__ import annotations

import re

from course_supporter.models.safety import PatternMatch

# Each pattern: (name, compiled regex)
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # System/assistant role injection
    (
        "role_injection",
        re.compile(
            r"(?:^|\s)(?:system\s*:|<\|system\|>|###\s*System|\[INST\]|"
            r"Human:|Assistant:|<\|im_start\|>)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    # Instruction override
    (
        "instruction_override",
        re.compile(
            r"(?:ignore|disregard|forget|override|bypass)\s+"
            r"(?:all\s+)?(?:\w+\s+)*?"
            r"(?:instructions?|rules?|prompts?|context|guidelines?)",
            re.IGNORECASE,
        ),
    ),
    # Jailbreak markers
    (
        "jailbreak",
        re.compile(
            r"\b(?:Do\s+Anything\s+Now|DAN\s+mode|"
            r"you\s+are\s+now\s+(?:an?|the)\s+|"
            r"pretend\s+(?:you\s+are|to\s+be)\s+|"
            r"act\s+as\s+(?:an?|the|my)\s+(?:\w+\s+)*"
            r"(?:assistant|ai|grader|evaluator|teacher|mentor|bot))",
            re.IGNORECASE,
        ),
    ),
    # Exfiltration attempts
    (
        "exfiltration",
        re.compile(
            r"(?:repeat|show|reveal|print|output|display)\s+"
            r"(?:the\s+)?(?:\w+\s+)*?"
            r"(?:prompt|instructions?|rules?|message)",
            re.IGNORECASE,
        ),
    ),
    # Direct score manipulation
    (
        "score_manipulation",
        re.compile(
            r"(?:grade|score|rate|evaluate)\s+(?:this|me|my\s+work)\s+"
            r"(?:as\s+)?(?:100|perfect|excellent|full\s+marks|maximum)",
            re.IGNORECASE,
        ),
    ),
]


def scan_for_injections(text: str) -> list[PatternMatch]:
    """Scan text for known prompt injection patterns.

    Args:
        text: Full text content to scan.

    Returns:
        List of pattern matches found. Empty if clean.
    """
    matches: list[PatternMatch] = []
    lines = text.split("\n")

    for line_idx, line in enumerate(lines, start=1):
        for pattern_name, pattern in _PATTERNS:
            for m in pattern.finditer(line):
                matches.append(
                    PatternMatch(
                        pattern_name=pattern_name,
                        matched_text=m.group().strip(),
                        line_number=line_idx,
                    )
                )

    return matches


def has_critical_matches(matches: list[PatternMatch]) -> bool:
    """Check if any matches are critical enough to reject without LLM.

    Currently, role_injection and instruction_override are critical.
    """
    critical_patterns = {"role_injection", "instruction_override"}
    return any(m.pattern_name in critical_patterns for m in matches)
