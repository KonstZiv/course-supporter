"""The old file name, replaced wherever the platform wrote it (Б-2, доповнення 1).

A review names the submitted file where a remark points — ``quote_file`` and
the position ``name:12-14`` in ``review_result`` (``homework/text_result.py``),
the italic position line in ``review_markdown``
(``homework/review_assembler.py``), the model's answers in the call register —
and free text may mention it. Owner's decision of 26.11: the command replaces
the old name everywhere except in the stored file's own content and the
container logs.

The rule is an EXACT match of the whole name (with its extension), standing on
its own: not part of a longer word or name (``a.py`` is not replaced inside
``data.py``) and not after a path separator — ``src/main.py`` inside a
project's archive is a path of the work, not the name of the upload, and is
not touched. A position's ``:12`` after the name is not part of the name and
stays. In JSON both keys and string values are rewritten; numbers, booleans and
nulls never hold a name.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class NameReplacer:
    """Replaces each old name of a mapping by its new name (module docstring)."""

    pattern: re.Pattern[str] | None
    mapping: Mapping[str, str]

    def text(self, value: str) -> tuple[str, int]:
        """The text with every old name replaced, and how many were."""
        if self.pattern is None:
            return value, 0
        return self.pattern.subn(lambda m: self.mapping[m.group(0)], value)

    def value(self, value: Any) -> tuple[Any, int]:
        """A column's value (text or JSON) rewritten, and the replacements."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            items = [self.value(item) for item in value]
            return [v for v, _ in items], sum(n for _, n in items)
        if isinstance(value, dict):
            rewritten: dict[Any, Any] = {}
            total = 0
            for key, item in value.items():
                new_key, in_key = self.text(key) if isinstance(key, str) else (key, 0)
                new_item, in_item = self.value(item)
                rewritten[new_key] = new_item
                total += in_key + in_item
            return rewritten, total
        return value, 0


def name_replacer(mapping: Mapping[str, str]) -> NameReplacer:
    """A replacer for ``mapping`` (old name → new name); names equal to their
    new name, and empty ones, are left out."""
    kept = {old: new for old, new in mapping.items() if old and old != new}
    if not kept:
        return NameReplacer(None, {})
    # The longest first: a name that is the tail of another must not win.
    names = sorted(kept, key=len, reverse=True)
    alternatives = "|".join(re.escape(name) for name in names)
    pattern = re.compile(rf"(?<![\w.\-/\\])(?:{alternatives})(?![\w\-]|\.\w)")
    return NameReplacer(pattern, kept)
