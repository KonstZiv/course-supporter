"""Acceptance check for concept-quality phase 1, lever 1 (PHASE-1 §3.5).

Runs the ratified consolidation tool over a captured concept slice and reports
before/after counts per object and in total. It proves the GROUPING rule and
the target numbers only — it deliberately does NOT apply ``subtract_by_key``
(the conflict rule), sorting, the segment validator, or any pipeline step. The
ratified numbers measure grouping; the conflict rule would yield a different
count.

Usage::

    uv run python -m scripts.concept_dedup_check <slice.csv>

Slice row format (no header): id, filename, description, main (JSON array),
secondary (JSON array), main_count, secondary_count, timestamp.

A filename present more than once (practice-1 appears twice as a determinism
control) is printed for every occurrence, but only its first occurrence
contributes to the totals; later occurrences are marked EXCLUDED.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from course_supporter.concept_dedup import dedupe_concepts

_NAME_COL = 1
_MAIN_COL = 3
_SECONDARY_COL = 4


def run(slice_path: str) -> int:
    """Print per-object and total before/after dedup counts for the slice."""
    with Path(slice_path).open(newline="", encoding="utf-8") as handle:
        rows = list(csv.reader(handle))

    print(f"Concept-dedup acceptance — {slice_path}")
    print(f"rows: {len(rows)}")
    print("-" * 72)

    main_before = main_after = 0
    secondary_before = secondary_after = 0
    seen: set[str] = set()

    for index, row in enumerate(rows):
        name = row[_NAME_COL]
        main: list[str] = json.loads(row[_MAIN_COL])
        secondary: list[str] = json.loads(row[_SECONDARY_COL])
        main_from, main_to = len(main), len(dedupe_concepts(main))
        sec_from, sec_to = len(secondary), len(dedupe_concepts(secondary))

        duplicate = name in seen
        seen.add(name)
        mark = "  [EXCLUDED from totals: duplicate]" if duplicate else ""
        print(
            f"[{index}] {name}: "
            f"main {main_from} -> {main_to}, "
            f"secondary {sec_from} -> {sec_to}{mark}"
        )
        if not duplicate:
            main_before += main_from
            main_after += main_to
            secondary_before += sec_from
            secondary_after += sec_to

    print("-" * 72)
    print(f"TOTAL main:      {main_before} -> {main_after}")
    print(f"TOTAL secondary: {secondary_before} -> {secondary_after}")
    return 0


def main() -> int:
    """Parse the slice-path argument and run the check."""
    parser = argparse.ArgumentParser(
        description="Concept-dedup acceptance check (PHASE-1 §3.5).",
    )
    parser.add_argument("slice", help="Path to the concept slice CSV.")
    args = parser.parse_args()
    return run(args.slice)


if __name__ == "__main__":
    sys.exit(main())
