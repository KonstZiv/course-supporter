"""Unfinished drafts of a test — the forms task 07c saves as they are (decision 11).

One table for every entry that saves a draft — the JSON body, the YAML body and
the YAML file — so each is proven on the same forms: the draft as data, and
the ``incomplete`` its reading owes the author.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import yaml


def place(
    code: str, question: int | None = None, option: int | None = None
) -> dict[str, Any]:
    """An entry of ``incomplete`` as the routes answer it."""
    return {"code": code, "question": question, "option": option}


@dataclass(frozen=True)
class Unfinished:
    """A draft's questions as data, and the unfinished places its reading lists."""

    questions: list[dict[str, Any]]
    incomplete: list[dict[str, Any]]

    def document(self, title: str | None) -> dict[str, Any]:
        """The test as data, named when ``title`` is given."""
        named: dict[str, Any] = {} if title is None else {"title": title}
        return {**named, "questions": self.questions}

    def as_json(self, *, title: str | None = None) -> bytes:
        return json.dumps(self.document(title), ensure_ascii=False).encode("utf-8")

    def as_yaml(self, *, title: str | None = None) -> bytes:
        return yaml.safe_dump(
            self.document(title), allow_unicode=True, sort_keys=False
        ).encode("utf-8")


def _options(*marks: tuple[str, bool]) -> list[dict[str, Any]]:
    return [{"text": text, "correct": correct} for text, correct in marks]


UNFINISHED: dict[str, Unfinished] = {
    "no-questions": Unfinished([], [place("TEST_NO_QUESTIONS")]),
    "no-options": Unfinished(
        [{"text": "Що?", "options": []}], [place("TEST_OPTIONS_COUNT", 1)]
    ),
    "one-option": Unfinished(
        [{"text": "Що?", "options": _options(("так", True))}],
        [place("TEST_OPTIONS_COUNT", 1)],
    ),
    "no-mark": Unfinished(
        [{"text": "Що?", "options": _options(("так", False), ("ні", False))}],
        [place("TEST_NO_CORRECT_OPTION", 1)],
    ),
    "empty-question": Unfinished(
        [{"text": "", "options": _options(("так", True), ("ні", False))}],
        [place("TEST_TEXT_EMPTY", 1)],
    ),
    "empty-option": Unfinished(
        [{"text": "Що?", "options": _options(("", True), ("ні", False))}],
        [place("TEST_TEXT_EMPTY", 1, 1)],
    ),
}
"""The six forms of task 07c's commit B1, lock 1 — one unfinished rule each."""

EVERY_PLACE = Unfinished(
    [
        {"text": "", "options": _options(("", False))},
        {"text": "Що?", "options": []},
    ],
    [
        place("TEST_TEXT_EMPTY", 1),
        place("TEST_TEXT_EMPTY", 1, 1),
        place("TEST_OPTIONS_COUNT", 1),
        place("TEST_NO_CORRECT_OPTION", 1),
        place("TEST_OPTIONS_COUNT", 2),
    ],
)
"""Every rule at once, in the order the author reads the cards."""
