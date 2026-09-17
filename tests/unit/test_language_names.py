"""The native-names file is CLDR, not a hand-written table (task 04, DD-2.4-L).

``config/language_names.yaml`` exists because ``iso639`` carries no native
names. The danger of a generated file checked into the repo is that it stops
being generated: one hand-correction here, another there, and a year later it
is the hand-written table DD-2.4-L was raised against.

So this asks CLDR itself, for every code on the whitelist, and compares. One
code is allowed to differ, by name, with the reason written beside its value in
the file itself. A second divergence is a failure — the choice has to be argued
and named, never made quietly.
"""

from __future__ import annotations

from babel import Locale

from course_supporter.language import get_language_registry, load_native_names

# The one CLDR answer that is not usable as it comes back, with the reason
# recorded next to the value in ``config/language_names.yaml``: CLDR has no
# name for ``cnr`` and answers ``srpski`` — Serbian, a different language.
ALLOWED_DIVERGENCES = frozenset({"cnr"})


def _cldr_native_name(code: str) -> str | None:
    """What CLDR calls this language in its own language, or None if it cannot.

    ``get_language_name`` and not ``get_display_name``: the latter appends the
    territory and script of the locale the code resolves to (``українська
    (Україна)``), which is a description of a locale, not a language's name.
    """
    try:
        return str(Locale.parse(code).get_language_name())
    except Exception:  # unknown-to-CLDR is an answer here, not a crash
        return None


class TestNamesComeFromCLDR:
    def test_the_file_covers_the_whitelist_both_ways(self) -> None:
        allowed = set(get_language_registry().languages)
        names = set(load_native_names())

        assert names - allowed == set(), "names for languages not on the list"
        assert allowed - names == set(), "languages on the list with no name"

    def test_every_name_is_the_one_cldr_gives(self) -> None:
        """The lock: a new silent hand-correction fails here."""
        names = load_native_names()

        divergent = {
            code for code, native in names.items() if _cldr_native_name(code) != native
        }

        assert divergent == ALLOWED_DIVERGENCES, (
            "The file must be what CLDR returns, except for the codes named in "
            "ALLOWED_DIVERGENCES, each with its reason written in the file. "
            f"Unexpected: {sorted(divergent - ALLOWED_DIVERGENCES)}; "
            f"no longer needed: {sorted(ALLOWED_DIVERGENCES - divergent)}."
        )

    def test_the_exception_carries_its_reason_in_the_file(self) -> None:
        """Named in a constant here is not enough — the file must say why."""
        from course_supporter.config import get_settings

        lines = (
            get_settings().language_names_path.read_text(encoding="utf-8").split("\n")
        )
        for code in ALLOWED_DIVERGENCES:
            start = lines.index(f"{code}:")
            entry = lines[start + 1 :]
            # The entry ends where the next top-level key begins.
            end = next(
                (i for i, line in enumerate(entry) if line and not line[0].isspace()),
                len(entry),
            )
            reason = [line for line in entry[:end] if line.lstrip().startswith("#")]
            assert reason, f"{code} diverges from CLDR with no reason beside it"
