"""DD-SP-CY probe: do prefix functions fail on toasted UTF-8 text?

Builds synthetic text values in a fresh UTF8 database, stores them under every
(storage mode x compression) combination and runs left/substring/substr/right
against each stored value. Every query runs on its own, so one failure does
not hide another. Each result is checked against the expected value computed
here in Python (md5 of the expected prefix/suffix).

Usage:
    uv run python probes/dd-sp-cy/probe.py --port 55008 --out results/17.8.csv
"""

# Intentional in this probe: Cyrillic test text (RUF001), md5/RNG used for
# comparison and reproducible data, not security (S311, S324), and table
# names interpolated from module constants (S608).
# ruff: noqa: RUF001, S311, S324, S608

from __future__ import annotations

import argparse
import csv
import hashlib
import random
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg

DB_NAME = "probe_utf8"
STORAGES = ("EXTENDED", "EXTERNAL", "MAIN")
COMPRESSIONS = ("pglz", "lz4")
SIZES = {"500B": 500, "1.5KB": 1536, "3KB": 3072, "10KB": 10240, "100KB": 102400}
NS = (1, 10, 100, 400, 1000)

# (name, SQL template, python expected(text, n)); "{n}" is bound as a parameter.
QUERIES = (
    ("left", "left(c, %(n)s)", lambda s, n: s[:n]),
    ("substring_from_for", "substring(c from 1 for %(n)s)", lambda s, n: s[:n]),
    ("substr", "substr(c, 1, %(n)s)", lambda s, n: s[:n]),
    ("right", "right(c, %(n)s)", lambda s, n: s[-n:]),
)
CONTROL = ("left_concat_400", "left(c || '', 400)", lambda s, n: s[:400])

UK_PARAGRAPH = (
    "Домашнє завдання перевіряє, чи студент розуміє цикли та функції. "
    "Спочатку прочитайте умову, потім напишіть розв'язок і перевірте його "
    "на кількох прикладах. Якщо тест не проходить, знайдіть помилку в логіці, "
    "а не в синтаксисі: інтерпретатор уже підказав, де саме вона виникла. "
)
CODE_BLOCK = (
    "def mean(values: list[float]) -> float:\n"
    "    return sum(values) / len(values)\n\n"
    "for i in range(10):\n"
    "    print(i, mean([i, i + 1]))\n"
)
ASCII_PARAGRAPH = (
    "The homework checks whether the student understands loops and functions. "
    "Read the task first, then write a solution and test it on a few examples. "
)
MB34_PARAGRAPH = (
    "Step one — read the task… then ship it \U0001f680 and wait… "
    "result — green \U0001f680 "
)
UK_LETTERS = "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя"
ASCII_LETTERS = "abcdefghijklmnopqrstuvwxyz"
PUNCT = (" ", " ", " ", " ", ", ", ". ")


@dataclass(frozen=True)
class Sample:
    kind: str
    size_label: str
    compressibility: str
    text: str


def _word(rng: random.Random, letters: str) -> str:
    return "".join(rng.choice(letters) for _ in range(rng.randint(2, 10)))


def _varied(kind: str, rng: random.Random, target: int) -> str:
    """Poorly compressible text: pseudo-words from random letters."""
    parts: list[str] = []
    size = 0
    while size < target + 64:
        if kind == "uk_prose":
            chunk = _word(rng, UK_LETTERS) + rng.choice(PUNCT)
        elif kind == "uk_code":
            if rng.random() < 0.25:
                name = _word(rng, ASCII_LETTERS)
                num, arg = rng.randint(0, 999), rng.randint(0, 99)
                chunk = f"\n    {name} = {name}_{num}({arg})\n"
            else:
                chunk = _word(rng, UK_LETTERS) + rng.choice(PUNCT)
        elif kind == "ascii":
            chunk = _word(rng, ASCII_LETTERS) + rng.choice(PUNCT)
        else:  # mb34
            chunk = _word(rng, ASCII_LETTERS) + rng.choice(
                (" ", " ", " — ", "… ", " \U0001f680 ")
            )
        parts.append(chunk)
        size += len(chunk.encode())
    return "".join(parts)


def _repetitive(kind: str, target: int) -> str:
    """Highly compressible text: one block repeated."""
    block = {
        "uk_prose": UK_PARAGRAPH,
        "uk_code": UK_PARAGRAPH + CODE_BLOCK,
        "ascii": ASCII_PARAGRAPH,
        "mb34": MB34_PARAGRAPH,
    }[kind]
    return block * (target // len(block.encode()) + 2)


def _cut_to_bytes(text: str, target: int) -> str:
    """Trim to at most ``target`` UTF-8 bytes on a character boundary."""
    return text.encode()[:target].decode(errors="ignore")


def samples() -> Iterator[Sample]:
    rng = random.Random(20260212)
    for kind in ("uk_prose", "uk_code", "ascii", "mb34"):
        for size_label, target in SIZES.items():
            yield Sample(
                kind,
                size_label,
                "high",
                _cut_to_bytes(_repetitive(kind, target), target),
            )
            yield Sample(
                kind,
                size_label,
                "low",
                _cut_to_bytes(_varied(kind, rng, target), target),
            )


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def prepare(conninfo: str) -> None:
    with psycopg.connect(conninfo, dbname="postgres", autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {DB_NAME}")
        conn.execute(f"CREATE DATABASE {DB_NAME} ENCODING 'UTF8' TEMPLATE template0")


def load(conn: psycopg.Connection, all_samples: list[Sample]) -> list[str]:
    tables = []
    for storage in STORAGES:
        for comp in COMPRESSIONS:
            table = f"t_{storage.lower()}_{comp}"
            conn.execute(
                f"CREATE TABLE {table} (id int PRIMARY KEY, kind text, size_label text,"
                f" compressibility text, c text)"
            )
            conn.execute(f"ALTER TABLE {table} ALTER COLUMN c SET STORAGE {storage}")
            conn.execute(f"ALTER TABLE {table} ALTER COLUMN c SET COMPRESSION {comp}")
            with conn.cursor() as cur:
                cur.executemany(
                    f"INSERT INTO {table} VALUES (%s, %s, %s, %s, %s)",
                    [
                        (i, s.kind, s.size_label, s.compressibility, s.text)
                        for i, s in enumerate(all_samples)
                    ],
                )
            tables.append(table)
    conn.commit()
    return tables


def run(conninfo: str, out: Path) -> None:
    prepare(conninfo)
    all_samples = list(samples())
    out.parent.mkdir(parents=True, exist_ok=True)
    with (
        psycopg.connect(conninfo, dbname=DB_NAME, autocommit=False) as conn,
        out.open("w", newline="") as fh,
    ):
        version = conn.execute("SELECT current_setting('server_version')").fetchone()[0]
        tables = load(conn, all_samples)
        conn.autocommit = True
        writer = csv.writer(fh)
        writer.writerow([
            "server_version", "table", "storage", "compression_setting", "id", "kind",
            "size_label", "compressibility", "octet_length", "length",
            "pg_column_compression", "pg_column_size", "toast_chunk_id", "form",
            "query", "n", "status", "sqlstate", "error",
        ])  # fmt: skip
        for table in tables:
            _, storage, comp = table.split("_")
            for i, sample in enumerate(all_samples):
                meta = conn.execute(
                    "SELECT octet_length(c), length(c), pg_column_compression(c),"
                    " pg_column_size(c), pg_column_toast_chunk_id(c)"
                    f" FROM {table} WHERE id = %s",
                    (i,),
                ).fetchone()
                octets, chars, col_comp, col_size, chunk_id = meta
                form = ("external" if chunk_id is not None else "inline") + (
                    f"-{col_comp}" if col_comp else "-plain"
                )
                row_meta = [
                    version, table, storage.upper(), comp, i, sample.kind,
                    sample.size_label, sample.compressibility, octets, chars,
                    col_comp or "", col_size, chunk_id or "", form,
                ]  # fmt: skip
                checks = [(q, n) for q in QUERIES for n in NS] + [(CONTROL, 400)]
                for (name, expr, expected), n in checks:
                    want = md5(expected(sample.text, n))
                    try:
                        got = conn.execute(
                            f"SELECT md5({expr}) FROM {table} WHERE id = %(id)s",
                            {"n": n, "id": i},
                        ).fetchone()[0]
                        status, sqlstate, err = (
                            ("ok" if got == want else "mismatch"),
                            "",
                            "",
                        )
                    except psycopg.Error as exc:
                        status = "error"
                        sqlstate = exc.sqlstate or ""
                        err = str(exc).splitlines()[0]
                    writer.writerow([*row_meta, name, n, status, sqlstate, err])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--user", default="postgres")
    parser.add_argument("--password", default="probe")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    conninfo = (
        f"host={args.host} port={args.port} user={args.user} password={args.password}"
    )
    run(conninfo, args.out)


if __name__ == "__main__":
    main()
