"""JSON for model input: the one rule for data serialised into a prompt.

A prompt is plain text for a model, not HTML. Text inside it must reach the
model the way a person reads it, so every place that puts JSON into a prompt
goes through :func:`prompt_json` — the Jinja2 filter ``tojson_unicode`` in
:mod:`course_supporter.llm.prompt_loader_md` and the ingestion passes that
serialise transcript words and concepts in Python.
"""

from __future__ import annotations

import json
from typing import Any


def prompt_json(
    value: Any,
    *,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
) -> str:
    """Serialise ``value`` as JSON for a prompt.

    The rule:

    * ``ensure_ascii=False`` — Cyrillic and any other letters stay as they
      are. A ``\\uXXXX`` escape costs six characters per letter and several
      tokens, and the model reads escaped words worse than plain ones.
    * Only ``<`` and ``>`` are escaped, as ``\\u003c`` and ``\\u003e``: they
      are the two characters that could close a data tag such as
      ``</node_canonical>`` around the JSON. The apostrophe of Ukrainian
      words and ``&`` stay as they are — no tag can be closed with them. The
      escapes keep the JSON valid: ``<`` and ``>`` can only occur inside
      strings, where ``\\u003c`` and ``\\u003e`` decode back to the same text.

    ``json.dumps`` still escapes control characters (U+0000-U+001F) — JSON
    requires it.

    Args:
        value: Any JSON-serialisable value.
        sort_keys: Passed to ``json.dumps``; each caller keeps the key order
            its prompt already shows.
        separators: Passed to ``json.dumps``; ``None`` is its default
            ``(", ", ": ")``, ``(",", ":")`` is the compact form.
    """
    text = json.dumps(
        value, ensure_ascii=False, sort_keys=sort_keys, separators=separators
    )
    return text.replace("<", "\\u003c").replace(">", "\\u003e")
