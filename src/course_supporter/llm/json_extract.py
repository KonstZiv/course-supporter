"""Shared JSON-output normalization for LLM provider responses.

Some models (notably Gemini in free-text mode) wrap an otherwise valid
JSON object in a markdown ```json ... ``` fence. The ingestion StageRouter
path parses provider output as JSON directly, so an un-stripped fence makes
``model_validate_json`` fail at the leading backtick ("expected value at
line 1 column 1").

This module is the single home for that normalization (previously duplicated
in ``providers/anthropic.py`` and ``providers/dashscope.py``). Providers call
:func:`strip_markdown_json` on their output when the caller declares it
expects JSON (``LLMRequest.expects_json``), so downstream validators always
receive bare JSON regardless of which provider answered.
"""

from __future__ import annotations

import re

_MD_JSON_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def strip_markdown_json(text: str) -> str:
    """Return the inner JSON of a markdown fence, or the stripped text.

    If ``text`` contains a ```` ```json ... ``` ```` (or bare ```` ``` ```` )
    fenced block, return its inner content; otherwise return ``text`` with
    surrounding whitespace removed. A no-op (modulo whitespace) for bare
    JSON, so it is safe to apply unconditionally to any JSON-expecting
    provider output.

    >>> strip_markdown_json('```json\\n{"a": 1}\\n```')
    '{"a": 1}'
    >>> strip_markdown_json('{"a": 1}')
    '{"a": 1}'
    """
    match = _MD_JSON_RE.search(text)
    return match.group(1).strip() if match else text.strip()
