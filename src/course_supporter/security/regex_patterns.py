"""Regex pre-screen for prompt-injection attempts (vision §KD14).

Defense-in-depth proxy layer for naive / common prompt-injection
attacks. The Stage 2 LLM safety check is the primary defense
against sophisticated attempts; this module catches the cheap
obvious cases before any LLM cost is incurred.

## Language scope (intentional contract, not TODO)

Patterns cover three languages chosen on a security-profile basis,
not by audience size:

* **English** -- lingua franca of attackers in stolen-credential
  scenarios. The dominant prompt-injection corpus on the public
  internet is English; an attacker exfiltrating an API key and
  scripting attacks at scale is statistically English-speaking.
* **Ukrainian** -- primary user audience for prompt injection in
  legitimate-submissions misuse, where a homework body is
  weaponised against the Mentor agent.
* **Russian** -- statistical attacker linguistic profile observed
  across multiple Eastern-European platforms; included on
  technical grounds, independent of any political consideration.

The other 19 target-audience languages (Moldovan, Romanian,
Serbian, Montenegrin, Croatian, Bosnian, German, Spanish, French,
Czech, Slovak, Slovenian, Italian, Bulgarian, Turkish, Polish,
Georgian, Armenian, Hungarian) are **explicitly relegated to the
Stage 2 LLM safety check**. Adding regex coverage for all 22
would be a linguistic-review nightmare with high false-positive
risk; the Stage 2 LLM is the right tool for non-Eng/Ukr/Rus
content. The ``TestForeignLanguageScopeContract`` test class
in ``tests/unit/security/test_regex_patterns.py`` locks the scope
so future PRs broadening it without explicit vision approval
break tests.

## Out-of-scope by design (not a TODO)

Unicode obfuscation attacks (zero-width insertions, homoglyph
variants) are caught upstream by ``check_text_unicode_safety``
and ``nfkc_for_security`` before regex executes. This module
does not duplicate that detection.

## Pattern design discipline

* **Phrase-only, never single keywords.** "забудь" alone has high
  false-positive rate against legitimate prose ("забудь про це");
  patterns require a 2+ word window like "забудь інструкції".
* **Imperative + optional 2nd-person plural.** Russian / Ukrainian
  morphology is rich; covering all inflections yields a linguist's
  nightmare. Patterns anchor on imperative ("ігноруй", "игнорируй")
  and optionally 2nd-person plural ("ігноруйте", "игнорируйте")
  where the plural form is plausible in script-mass attack input.
  Infinitive, past tense, and nominalisations land in Stage 2.
* ``re.IGNORECASE | re.UNICODE`` flags. Patterns precompiled at
  module load.
* No nested unbounded quantifiers. Each pattern reviewed for
  catastrophic-backtracking resistance manually; ``re.compile``
  does not detect ReDoS automatically.
* ``\\b`` boundaries on both ends to avoid substring matches
  ("Ignoruj" in Polish must NOT match the English "ignore"
  pattern, "ignored" must NOT match either).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CompiledPattern:
    """Precompiled prompt-injection pattern with its log category.

    Attributes:
        pattern: ``re.Pattern`` precompiled with
            ``re.IGNORECASE | re.UNICODE``.
        category: Subcategory for log forensics. One of:
            ``instruction_override``, ``system_prompt_leak``,
            ``role_manipulation``.
    """

    pattern: re.Pattern[str]
    category: str


_FLAGS = re.IGNORECASE | re.UNICODE


def _compile(pattern: str, category: str) -> CompiledPattern:
    return CompiledPattern(re.compile(pattern, _FLAGS), category)


# ── English instruction override ───────────────────────────────────

_ENGLISH_INSTRUCTION_OVERRIDE: list[CompiledPattern] = [
    _compile(
        r"\bignore\s+(?:(?:all|the)\s+)?(?:previous|prior|above)\s+instructions?\b",
        "instruction_override",
    ),
    _compile(
        r"\bignore\s+all\s+instructions?\b",
        "instruction_override",
    ),
    _compile(
        r"\bdisregard\s+(?:(?:all|the)\s+)?(?:previous|prior|system)\s+(?:prompt|instructions?)\b",
        "instruction_override",
    ),
    _compile(
        r"\bforget\s+(?:(?:all|the)\s+)?(?:previous|prior)\s+instructions?\b",
        "instruction_override",
    ),
]

# ── English system prompt leak ─────────────────────────────────────

_ENGLISH_PROMPT_LEAK: list[CompiledPattern] = [
    _compile(
        r"\breveal\s+(?:your\s+)?system\s+prompt\b",
        "system_prompt_leak",
    ),
    _compile(
        r"\bshow\s+me\s+your\s+(?:system\s+)?(?:prompt|instructions)\b",
        "system_prompt_leak",
    ),
    _compile(
        r"\bwhat\s+(?:is|are)\s+your\s+(?:original\s+|system\s+)?instructions?\b",
        "system_prompt_leak",
    ),
]

# ── English role manipulation ──────────────────────────────────────

_ENGLISH_ROLE_MANIPULATION: list[CompiledPattern] = [
    _compile(
        r"\byou\s+are\s+now\s+(?:a\s+)?(?:different|new|jailbroken)\b",
        "role_manipulation",
    ),
    _compile(
        r"\bact\s+as\s+(?:a\s+)?(?:jailbroken|developer\s+mode|dan)\b",
        "role_manipulation",
    ),
]

# ── Ukrainian instruction override ─────────────────────────────────

_UKRAINIAN_INSTRUCTION_OVERRIDE: list[CompiledPattern] = [
    _compile(
        r"\bігноруй(?:те)?\s+(?:(?:усі|всі)\s+)?попередні\s+(?:інструкції|вказівки)\b",
        "instruction_override",
    ),
    _compile(
        r"\bне\s+зважай(?:те)?\s+на\s+(?:(?:усі|всі)\s+)?попередні\s+(?:інструкції|вказівки)\b",
        "instruction_override",
    ),
    _compile(
        r"\bзабудь(?:те)?\s+(?:(?:усі|всі)\s+)?(?:попередні\s+)?(?:інструкції|вказівки)\b",
        "instruction_override",
    ),
]

# ── Ukrainian system prompt leak ───────────────────────────────────

_UKRAINIAN_PROMPT_LEAK: list[CompiledPattern] = [
    _compile(
        r"\bпокажи\s+(?:мені\s+)?(?:свій\s+)?системний\s+(?:промпт|запит|prompt)\b",
        "system_prompt_leak",
    ),
    _compile(
        r"\bрозкрий\s+(?:свій\s+)?системний\s+(?:промпт|запит|prompt)\b",
        "system_prompt_leak",
    ),
]

# ── Russian instruction override ───────────────────────────────────

_RUSSIAN_INSTRUCTION_OVERRIDE: list[CompiledPattern] = [
    _compile(
        r"\bигнорируй(?:те)?\s+(?:все\s+)?(?:предыдущие|прежние)\s+(?:инструкции|указания)\b",
        "instruction_override",
    ),
    _compile(
        r"\bзабудь(?:те)?\s+(?:все\s+)?(?:предыдущие\s+)?(?:инструкции|указания)\b",
        "instruction_override",
    ),
    _compile(
        r"\bне\s+обращай(?:те)?\s+внимания\s+на\s+(?:предыдущие|системные)\s+(?:инструкции|указания)\b",
        "instruction_override",
    ),
]

# ── Russian system prompt leak ─────────────────────────────────────

_RUSSIAN_PROMPT_LEAK: list[CompiledPattern] = [
    _compile(
        r"\bпокажи\s+(?:мне\s+)?(?:свой\s+)?системный\s+(?:промпт|запрос|prompt)\b",
        "system_prompt_leak",
    ),
    _compile(
        r"\bраскрой\s+(?:свой\s+)?системный\s+(?:промпт|запрос|prompt)\b",
        "system_prompt_leak",
    ),
]


PROMPT_INJECTION_PATTERNS: list[CompiledPattern] = [
    *_ENGLISH_INSTRUCTION_OVERRIDE,
    *_ENGLISH_PROMPT_LEAK,
    *_ENGLISH_ROLE_MANIPULATION,
    *_UKRAINIAN_INSTRUCTION_OVERRIDE,
    *_UKRAINIAN_PROMPT_LEAK,
    *_RUSSIAN_INSTRUCTION_OVERRIDE,
    *_RUSSIAN_PROMPT_LEAK,
]


def match_text(text: str) -> CompiledPattern | None:
    """Return the first matching pattern, or ``None`` if the text is clean.

    Caller is responsible for passing NFKC-normalized text.
    Compatibility variants (full-width Latin, presentation forms)
    collapse to canonical bases through ``nfkc_for_security``
    upstream so this function only needs to consider canonical
    Latin / Cyrillic.

    The Stage 1 orchestrator wraps a non-``None`` return into
    ``SecurityRejectedError(ErrorCategory.PROMPT_INJECTION, ...)``;
    keeping this layer non-raising lets callers (and tests)
    inspect which pattern matched without try/except scaffolding.

    Args:
        text: NFKC-normalized text to scan. May span multiple lines;
            patterns operate on phrases that don't cross newlines,
            but ``re.search`` scans the entire string.

    Returns:
        The first :class:`CompiledPattern` matching ``text``, or
        ``None`` if no pattern matched.
    """
    for compiled in PROMPT_INJECTION_PATTERNS:
        if compiled.pattern.search(text):
            return compiled
    return None
