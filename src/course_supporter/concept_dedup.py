"""Deterministic concept-spelling consolidation (concept-quality phase 1, Д1).

The methodist pipeline records a *concept* as a free-form string exactly as
the LLM emitted it. Across segments and source types the same idea surfaces
under trivially different spellings — case, hyphen-vs-space, and
singular-vs-plural — which fragments the concept index and breaks
exact-match concept navigation.

This module is the deterministic lever: two pure functions, with **no**
knowledge of source types, segments, or tree levels.

* :func:`normalization_key` maps a concept string to a grouping key. The key
  is *only* a grouping handle: it never appears in any output and may be
  ungrammatical.
* :func:`dedupe_concepts` collapses a list that may contain repeats and
  spelling variants into one entry per group, choosing the surviving
  spelling by vote and returning it **verbatim**.

Deliberate limit (measured on the 2026-08-02 production slice; PHASE-1
§3.1): the plural rule strips a trailing ``s`` only — ``es`` / ``ies`` forms
are left untouched, because the measured contribution of the plural rule is
2 of 261 main and 1 of 205 secondary concepts, and widening it raises the
false-merge risk without matching payoff.
"""

from __future__ import annotations

__all__ = ["dedupe_concepts", "normalization_key"]


def _plural_rule_guarded(word: str) -> bool:
    """Return whether the trailing-``s`` plural rule must NOT apply to ``word``.

    Guards are evaluated on the word in its **original case** (before the
    lowercasing step of :func:`normalization_key`) and gate *only* the plural
    rule — never the case or hyphen normalization.

    Args:
        word: A single non-empty token, hyphens already replaced by spaces
            upstream so a token never contains one.

    Returns:
        ``True`` if any guard fires — all-caps abbreviation, short word
        (<= 3 chars), code identifier (contains a dot or starts with a
        service character), or a natural double-``s`` ending — else ``False``.
    """
    if word.isupper():  # all-caps abbreviation: CSS, HTML, DOM, SPA
        return True
    if len(word) <= 3:  # short word: app, it
        return True
    if "." in word or not word[0].isalnum():  # code name: app.config, *ngFor, $refs
        return True
    return word.endswith("ss")  # natural double-s: class, process


def normalization_key(value: str) -> str:
    """Build the case/hyphen/plural-insensitive grouping key for a concept.

    Steps, in order:

    1. Trim the edges and collapse runs of whitespace to single spaces.
    2. Replace hyphens with spaces.
    3. Lowercase.
    4. For each word, strip a trailing ``s`` unless a plural guard fires
       (see :func:`_plural_rule_guarded`).

    The key exists solely to group spelling variants; it is never emitted.
    An ungrammatical key is harmless because only the verbatim winner chosen
    by :func:`dedupe_concepts` is ever returned.

    Args:
        value: A raw concept string.

    Returns:
        The grouping key.

    Examples:
        >>> normalization_key("HTML-Template")
        'html template'
        >>> normalization_key("HTML Templates")
        'html template'
        >>> normalization_key("app.config")
        'app.config'
    """
    collapsed = " ".join(value.split())  # 1. trim + collapse whitespace
    spaced = collapsed.replace("-", " ")  # 2. hyphen -> space
    words: list[str] = []
    for word in spaced.split():
        guarded = _plural_rule_guarded(word)  # on original case, pre-lowercase
        lowered = word.lower()  # 3. lowercase
        if not guarded and lowered.endswith("s"):  # 4. strip trailing plural s
            lowered = lowered[:-1]
        words.append(lowered)
    return " ".join(words)


def dedupe_concepts(concepts: list[str]) -> list[str]:
    """Collapse spelling variants and repeats to one verbatim entry per group.

    Groups the input by :func:`normalization_key`. Every occurrence of a
    spelling is a vote; the surviving spelling of a group is the one with the
    most votes, ties broken by earliest first occurrence in the input order.
    The winner is returned **verbatim** — no cosmetic touch-up.

    Output order is the order in which each group's key was first seen; the
    caller is responsible for any sorting of the result (PHASE-1 §3.1 keeps
    existing output sorting untouched at the merge sites).

    Args:
        concepts: A concept list that may contain repeats and spelling
            variants (case, hyphen-vs-space, singular-vs-plural).

    Returns:
        One entry per group, verbatim winners, in first-seen key order.
    """
    key_order: list[str] = []
    votes: dict[str, dict[str, int]] = {}
    first_seen: dict[str, dict[str, int]] = {}
    for index, concept in enumerate(concepts):
        key = normalization_key(concept)
        if key not in votes:
            votes[key] = {}
            first_seen[key] = {}
            key_order.append(key)
        votes[key][concept] = votes[key].get(concept, 0) + 1
        if concept not in first_seen[key]:
            first_seen[key][concept] = index

    result: list[str] = []
    for key in key_order:
        group = votes[key]
        seen = first_seen[key]
        best_spelling = ""
        best_rank: tuple[int, int] | None = None
        for spelling, count in group.items():
            # Rank ascending: most votes first (-count), then earliest
            # first occurrence — a plain, total, deterministic order.
            rank = (-count, seen[spelling])
            if best_rank is None or rank < best_rank:
                best_rank = rank
                best_spelling = spelling
        result.append(best_spelling)
    return result
