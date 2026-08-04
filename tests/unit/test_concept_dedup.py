"""Tests for ``course_supporter.concept_dedup`` (concept-quality phase 1, Д1).

One test function per PHASE-1 §3.4 item: the three normalization
dimensions, each of the four plural guards, winner-by-vote-count (not
alphabetical), tie-by-first-occurrence, verbatim winner, empty input, and
idempotence on a variant-free list.
"""

from __future__ import annotations

from course_supporter.concept_dedup import (
    dedupe_concepts,
    normalization_key,
    subtract_by_key,
)

# ── Normalization key: three dimensions ─────────────────────────────


def test_normalization_key_trims_and_collapses_whitespace() -> None:
    assert normalization_key("  event   loop  ") == "event loop"


def test_normalization_key_maps_hyphen_to_space() -> None:
    assert normalization_key("fan-out") == "fan out"
    assert normalization_key("fan-out") == normalization_key("fan out")


def test_normalization_key_lowercases() -> None:
    assert normalization_key("Coroutine") == "coroutine"


# ── Plural guards: four kinds (each blocks the trailing-s strip) ─────


def test_plural_guard_all_caps_abbreviation() -> None:
    # HTTPS is all-caps and longer than 3 chars, so only the abbreviation
    # guard applies; without it the key would strip to "http".
    assert normalization_key("HTTPS") == "https"


def test_plural_guard_short_word() -> None:
    # "its" is exactly 3 chars and lowercase, so only the short-word guard
    # applies; without it the key would strip to "it".
    assert normalization_key("its") == "its"


def test_plural_guard_code_identifier() -> None:
    # Contains a dot -> code name; trailing s kept ("this.prop" otherwise).
    assert normalization_key("this.props") == "this.props"
    # Starts with a service character -> code name; trailing s kept.
    assert normalization_key("$refs") == "$refs"


def test_plural_guard_double_s_ending() -> None:
    # A natural double-s ending is not a plural marker.
    assert normalization_key("class") == "class"
    assert normalization_key("process") == "process"


# ── Winner selection ────────────────────────────────────────────────


def test_winner_chosen_by_vote_count_not_alphabetical() -> None:
    # "Apple" sorts before "apple" (uppercase first), but "apple" has more
    # votes, so vote count decides — not alphabetical order.
    assert dedupe_concepts(["Apple", "apple", "apple"]) == ["apple"]


def test_tie_broken_by_first_occurrence() -> None:
    # Equal votes (1 each): the spelling seen first in the input wins.
    assert dedupe_concepts(["Foo", "foo"]) == ["Foo"]


def test_winner_returned_verbatim() -> None:
    # Even when the normalization key mutates the spelling (ngOnChanges ->
    # key "ngonchange"), the returned value is the verbatim input.
    assert dedupe_concepts(["app.config"]) == ["app.config"]
    assert dedupe_concepts(["ngOnChanges"]) == ["ngOnChanges"]
    assert dedupe_concepts(["provideRouter"]) == ["provideRouter"]


# ── Boundary behaviour ──────────────────────────────────────────────


def test_empty_input_returns_empty() -> None:
    assert dedupe_concepts([]) == []


def test_idempotent_on_variant_free_list() -> None:
    concepts = ["variable", "function", "class"]
    once = dedupe_concepts(concepts)
    assert once == concepts
    assert dedupe_concepts(once) == once


# ── Conflict rule: subtraction by normalization key ─────────────────


def test_conflict_subtraction_empties_secondary_spelling_variant() -> None:
    # Main "HTML Template" and secondary "HTML templates" share a key, so the
    # conflict rule (subtract by key) empties the secondary side even though
    # the exact strings differ (PHASE-1 §3.4 mandatory case).
    main = dedupe_concepts(["HTML Template"])
    secondary = dedupe_concepts(["HTML templates"])
    assert subtract_by_key(secondary, main) == []


def test_subtract_by_key_keeps_non_matching_verbatim_in_order() -> None:
    # Entries whose key is absent from ``minus`` survive verbatim and in order.
    assert subtract_by_key(["loop", "State"], ["variable"]) == ["loop", "State"]
