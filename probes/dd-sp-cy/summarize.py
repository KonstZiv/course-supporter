"""Summarize probe CSVs into results/summary.md.

Besides counting failures, checks one mechanism prediction: for a toasted
value text_substring() fetches only the first (start + n) * 4 bytes
(4 = max bytes per UTF-8 char). If that cut lands inside a multibyte char,
the slice ends with a partial char. The prediction "error iff toasted and the
cut splits a char" is compared with what each server actually did.

Usage:
    uv run python probes/dd-sp-cy/summarize.py
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

from probe import samples

RESULTS = Path(__file__).parent / "results"
VERSIONS = ("17.7", "17.8", "pgvector-0.8.1-pg17", "17.9", "17.10", "17.11")
PREFIX_QUERIES = {"left", "substring_from_for", "substr"}


def load() -> dict[str, list[dict[str, str]]]:
    return {
        v: list(csv.DictReader((RESULTS / f"{v}.csv").open()))
        for v in VERSIONS
        if (RESULTS / f"{v}.csv").exists()
    }


def cut_splits_char(text: str, n: int) -> bool:
    data = text.encode()
    cut = (1 + n) * 4
    return cut < len(data) and (data[cut] & 0xC0) == 0x80


def predicted_error(row: dict[str, str], texts: list[str]) -> bool:
    if row["query"] not in PREFIX_QUERIES or row["form"] == "inline-plain":
        return False
    return cut_splits_char(texts[int(row["id"])], int(row["n"]))


def fmt(errors: int, total: int) -> str:
    return f"**{errors}/{total}**" if errors else f"0/{total}"


def table(
    rows_by_version: dict[str, list[dict[str, str]]], key: str, keys: list[str]
) -> list[str]:
    versions = list(rows_by_version)
    out = [
        f"| {key} | " + " | ".join(versions) + " |",
        "|---" * (len(versions) + 1) + "|",
    ]
    for k in keys:
        cells = []
        for v in versions:
            rows = [r for r in rows_by_version[v] if r[key] == k]
            cells.append(fmt(sum(r["status"] != "ok" for r in rows), len(rows)))
        out.append(f"| {k} | " + " | ".join(cells) + " |")
    return out


def main() -> None:
    data = load()
    texts = [s.text for s in samples()]
    any_rows = next(iter(data.values()))
    lines = ["# DD-SP-CY probe summary", "", "Cells: failed checks / all checks.", ""]

    lines += ["## Storage mode x data kind x version", ""]
    versions = list(data)
    lines += [
        "| storage | kind | " + " | ".join(versions) + " |",
        "|---" * (len(versions) + 2) + "|",
    ]
    grid: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for v, rows in data.items():
        for r in rows:
            grid[(r["storage"], r["kind"], v)].append(r["status"])
    for storage in ("EXTENDED", "EXTERNAL", "MAIN"):
        for kind in ("uk_prose", "uk_code", "ascii", "mb34"):
            cells = [
                fmt(
                    sum(s != "ok" for s in grid[(storage, kind, v)]),
                    len(grid[(storage, kind, v)]),
                )
                for v in versions
            ]
            lines.append(f"| {storage} | {kind} | " + " | ".join(cells) + " |")

    for key in ("form", "compression_setting", "size_label", "query", "n"):
        keys = list(dict.fromkeys(r[key] for r in any_rows))
        lines += ["", f"## By {key}", "", *table(data, key, keys)]

    lines += ["", "## Error messages", ""]
    for v, rows in data.items():
        for (state, msg), count in Counter(
            (r["sqlstate"], r["error"]) for r in rows if r["status"] != "ok"
        ).most_common():
            lines.append(f"- {v}: {count} x `{state}` {msg}")
        if all(r["status"] == "ok" for r in rows):
            lines.append(f"- {v}: no errors, no mismatches")

    lines += ["", "## Mechanism check (slice cut inside a multibyte char)", ""]
    for v, rows in data.items():
        confusion = Counter(
            (predicted_error(r, texts), r["status"] != "ok") for r in rows
        )
        lines.append(
            f"- {v}: predicted&failed={confusion[(True, True)]}, "
            f"predicted&ok={confusion[(True, False)]}, "
            f"not-predicted&failed={confusion[(False, True)]}, "
            f"not-predicted&ok={confusion[(False, False)]}"
        )

    (RESULTS / "summary.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
