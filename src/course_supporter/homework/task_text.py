"""Shared budgeted stitcher for the mentor-facing ``task_text``.

task-code-materials commit 6 (ratified R5(b): every consumer guards its
own input). Code segments are verbatim source files, so the stitched
task_text of a project task can reach megabytes — where the previous
unguarded ``"\\n\\n".join`` would ride straight into the mentor stages'
context windows and die with a provider error. Both stitch sites —
:func:`course_supporter.homework.task_context.load_task_context` AND
its private twin ``CriteriaCacheService._task_text`` — now call
:func:`stitch_task_text`; a third site must too (grep lock below).

Managed degradation (mirror of the ``mentor_context`` drop-with-marker
pattern): segments are added whole, in reading order, until the budget
is hit; the remainder is dropped whole and a visible marker line
records how many segments were skipped. Never a hard failure.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

# Budget for the stitched task text. It used to mirror a same-sized cap inside
# the delta-context builder; step E removed that one (the project branch now
# refuses an oversize submission before assembly, at a smaller number), so this
# is the only 512 KiB ceiling left. It stays, and on its own reasons: this
# string is the TASK, it rides into the mentor prompts on every branch — not
# just the project one — and nothing upstream bounds it.
MENTOR_TASK_TEXT_MAX_BYTES: Final[int] = 512 * 1024

_SKIP_MARKER: Final[str] = (
    "[TASK_TEXT TRUNCATED: {skipped} of {total} segments dropped "
    "over the {budget}-byte budget]"
)


def stitch_task_text(
    rows: Iterable[str | None],
    *,
    max_bytes: int = MENTOR_TASK_TEXT_MAX_BYTES,
) -> str:
    """Join segment contents with ``"\\n\\n"`` under a byte budget.

    Empty/None rows are dropped (byte-identical to the previous
    unguarded joins for every under-budget input). Over budget,
    trailing segments are dropped WHOLE (a half-segment of source code
    misleads the mentor more than its absence) and a visible marker is
    appended so the degradation is never silent.
    """
    contents = [r for r in rows if r]
    included: list[str] = []
    used = 0
    skipped = 0
    for content in contents:
        block_bytes = len(content.encode("utf-8")) + 2  # the "\n\n" joiner
        if used + block_bytes <= max_bytes:
            included.append(content)
            used += block_bytes
        else:
            skipped += 1
    if skipped:
        included.append(
            _SKIP_MARKER.format(skipped=skipped, total=len(contents), budget=max_bytes)
        )
    return "\n\n".join(included)
