"""A test written as YAML: reading it with its checks, and writing it back (task 07b).

Purpose:
    A test the author writes outside the system arrives as YAML in the shape
    of PRE-FLIGHT section 6.1, and a test written inside it leaves the same
    way. Between the file and the draft stand the rules of the format, each
    with a refusal the author can act on: a code for the reason and a place —
    the line and the column in the file, and the question and the option the
    fault is in. The file is also read the way a person reads it: ``yes`` and
    ``1990`` in a text are that text, not a boolean and a number.

Interface:
    :func:`load_test_yaml` — the bytes of a YAML test → :class:`LoadedDraft`,
        its title and its draft, or :class:`DraftRefusedError`.
    :func:`screen_texts` — a draft's texts through the Stage 1 screens as
        they will be read, whatever format the draft came in.
    :func:`dump_test_yaml` — a draft and its title → the canonical YAML.
    :class:`DraftRefusalCode` and :class:`RefusalPlace` — what a refusal says.
    :data:`MAX_BODY_BYTES` and the other limits of sections 6.1 and 6.2.

How a file is read (section 6.2):
    1. Over :data:`MAX_BODY_BYTES` it is refused before anything else reads it
       (``TEST_TOO_LARGE``).
    2. The text screens of Stage 1 (KD14, decision 15) — the encoding, Unicode,
       attempts to steer a model — because the text of a test reaches the
       explanation prompt. Their refusal stays theirs:
       :class:`~course_supporter.security.exceptions.SecurityRejectedError`.
    3. ``yaml.compose`` builds a tree of nodes with a safe loader that
       constructs nothing, refuses an anchor or an alias at the place it is
       written (``TEST_YAML_ALIAS``) — a test repeats nothing, and a tree
       without aliases cannot expand — and refuses nesting deeper than a test
       ever goes, before the composer's recursion could run out of stack.
    4. A walk of the tree builds the draft. A text is the RAW value of its
       node, whatever tag YAML would give it; ``correct`` is only ``true`` or
       ``false``; ``pass_threshold`` only a whole number. A key given twice
       (``TEST_YAML_DUPLICATE_KEY``) or unknown to the schema is refused at the
       key; a missing field, at the set of fields that lacks it.
    5. The texts are screened again as they will be read
       (:func:`screen_texts`): a double-quoted string decodes its escapes —
       ``\\u200b``, ``\\x49`` — only in step 3, after the screens of step 2
       have read the file. What the screens refuse in a text written plainly,
       they refuse written as escapes too.

    >>> source = b'''questions:
    ... - text: Is 1 odd?
    ...   options:
    ...   - text: yes
    ...     correct: true
    ...   - text: no
    ...     correct: false
    ... '''
    >>> loaded = load_test_yaml(source, language="eng")
    >>> [option.text for option in loaded.body.questions[0].options]
    ['yes', 'no']
    >>> try:
    ...     load_test_yaml(source + b"questions: []\\n", language="eng")
    ... except DraftRefusedError as refusal:
    ...     print(refusal.code, refusal.place)
    TEST_YAML_DUPLICATE_KEY RefusalPlace(line=8, column=1, question=None, option=None)

Writing it back (section 6.4):
    :func:`dump_test_yaml` writes the keys in the order of the schema, the
    optional ones only when they are set, and the letters of the options
    nowhere — the system sets them (decision 16). PyYAML quotes whatever
    would read back as something else, so the dump reads back as the draft:

    >>> text = dump_test_yaml(loaded.body)
    >>> print(text, end="")
    questions:
    - text: Is 1 odd?
      options:
      - text: 'yes'
        correct: true
      - text: 'no'
        correct: false
    >>> load_test_yaml(text.encode(), language="eng") == loaded
    True

Extending:
    A new field is a name in the field set it belongs to, a reader in the walk
    and a line in the dump — and a case in the round-trip test, which is what
    proves the three agree.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import yaml
from yaml.error import Mark
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from course_supporter.homework.test_object import (
    MAX_OPTIONS,
    DraftBody,
    DraftOption,
    DraftQuestion,
)
from course_supporter.security.stage1 import run_stage1

__all__ = [
    "MAX_BODY_BYTES",
    "MAX_EXPLANATION_CHARS",
    "MAX_OPTION_CHARS",
    "MAX_PASS_THRESHOLD",
    "MAX_QUESTIONS",
    "MAX_QUESTION_CHARS",
    "MAX_TITLE_CHARS",
    "MIN_OPTIONS",
    "MIN_PASS_THRESHOLD",
    "DraftRefusalCode",
    "DraftRefusedError",
    "LoadedDraft",
    "RefusalPlace",
    "dump_test_yaml",
    "load_test_yaml",
    "screen_texts",
]

MAX_BODY_BYTES: Final[int] = 256 * 1024
"""The largest YAML test, in bytes (section 6.2)."""

MAX_TITLE_CHARS: Final[int] = 200
MAX_QUESTIONS: Final[int] = 200
MAX_QUESTION_CHARS: Final[int] = 2_000
MIN_OPTIONS: Final[int] = 2
MAX_OPTION_CHARS: Final[int] = 500
MAX_EXPLANATION_CHARS: Final[int] = 2_000
MIN_PASS_THRESHOLD: Final[int] = 1
MAX_PASS_THRESHOLD: Final[int] = 100

_FIELDS_FILENAME: Final = "test-fields.txt"
"""The name a draft's texts are screened under: plain text, whatever the format."""

_TEST_FIELDS: Final = ("title", "pass_threshold", "questions")
_QUESTION_FIELDS: Final = ("text", "options", "explanation")
_OPTION_FIELDS: Final = ("text", "correct")

_BOOL_TAG: Final = "tag:yaml.org,2002:bool"
_INT_TAG: Final = "tag:yaml.org,2002:int"
_WHOLE_NUMBER: Final = re.compile(r"[0-9]+")

_MAX_DEPTH: Final[int] = 32
"""How deep the composer may nest.

A test is six levels deep — the test, its questions, a question, its options,
an option, a value — so a file nested deeper than this is no test with a wrong
field in it, and it is refused before the composer, which recurses once per
level, is walked into the recursion limit.
"""


class DraftRefusalCode(StrEnum):
    """Why a test's draft was refused — one code per reason (section 6.3)."""

    TEST_YAML_UNREADABLE = "TEST_YAML_UNREADABLE"
    TEST_YAML_DUPLICATE_KEY = "TEST_YAML_DUPLICATE_KEY"
    TEST_YAML_ALIAS = "TEST_YAML_ALIAS"
    TEST_FIELD_INVALID = "TEST_FIELD_INVALID"
    TEST_OPTIONS_COUNT = "TEST_OPTIONS_COUNT"
    TEST_NO_CORRECT_OPTION = "TEST_NO_CORRECT_OPTION"
    TEST_TOO_LARGE = "TEST_TOO_LARGE"


@dataclass(frozen=True, slots=True)
class RefusalPlace:
    """Where a refusal points, everything counted from 1; unknown is ``None``.

    ``line`` and ``column`` are in the file; ``question`` and ``option`` are the
    positions of the question and of its option the fault is in.
    """

    line: int | None = None
    column: int | None = None
    question: int | None = None
    option: int | None = None

    def to_json(self) -> dict[str, int | None]:
        """The ``place`` of a refusal body."""
        return {
            "line": self.line,
            "column": self.column,
            "question": self.question,
            "option": self.option,
        }


class DraftRefusedError(Exception):
    """A draft the author has to fix: the reason's code, what is wrong, where."""

    def __init__(
        self, code: DraftRefusalCode, details: str, place: RefusalPlace
    ) -> None:
        super().__init__(f"{code.value}: {details}")
        self.code = code
        self.details = details
        self.place = place


@dataclass(frozen=True, slots=True)
class LoadedDraft:
    """What a YAML test holds: its title, when it names one, and its draft."""

    title: str | None
    body: DraftBody


class _Loader(yaml.SafeLoader):
    """The safe loader, refusing what no test needs.

    An anchor or an alias is refused at the event that carries it, so the place
    is where the author wrote it: a composed alias IS the node it names, and a
    walk would only meet it again where the anchor stands.
    """

    def __init__(self, stream: str) -> None:
        super().__init__(stream)
        self._depth = 0

    def compose_node(self, parent: Node | None, index: int) -> Node | None:
        """Compose one node, unless it is anchored, an alias, or too deep."""
        # The stubs leave the parser's event API unannotated.
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if isinstance(event, yaml.NodeEvent) and event.anchor is not None:
            raise DraftRefusedError(
                DraftRefusalCode.TEST_YAML_ALIAS,
                "a test repeats nothing: anchors (&) and aliases (*) are not read",
                _place(event.start_mark),
            )
        if self._depth == _MAX_DEPTH:
            raise DraftRefusedError(
                DraftRefusalCode.TEST_FIELD_INVALID,
                f"the file nests deeper than {_MAX_DEPTH} levels; a test has six",
                _place(event.start_mark),
            )
        self._depth += 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._depth -= 1


def load_test_yaml(
    content: bytes, *, language: str | None, filename: str = "test.yaml"
) -> LoadedDraft:
    """Read a YAML test into its title and its draft, or refuse it with a place.

    Args:
        content: The YAML as it arrived — a file, or the body of a request.
        language: The course language, ISO 639-3: Stage 1 checks a file that is
            not UTF-8 against it. ``None`` checks nothing, and such a file is
            then refused rather than guessed at.
        filename: The name Stage 1 screens the text under. A request body has
            none, so a YAML name stands in for it.

    Returns:
        The title, when the file gives one, and the checked draft.

    Raises:
        DraftRefusedError: The file breaks a rule of the format.
        SecurityRejectedError: A Stage 1 screen refused the file, or a text
            read out of it (:func:`screen_texts`).
    """
    if len(content) > MAX_BODY_BYTES:
        raise DraftRefusedError(
            DraftRefusalCode.TEST_TOO_LARGE,
            f"the test is {len(content)} bytes; a test may have {MAX_BODY_BYTES}",
            RefusalPlace(),
        )
    screened = run_stage1(
        filename=filename,
        content=content,
        context="authored",
        languages=(language,) if language else (),
    )
    if screened.nfc_text is None:
        msg = f"{filename!r} is not screened as text; name a YAML test .yaml or .yml"
        raise ValueError(msg)
    try:
        root: Node | None = yaml.compose(screened.nfc_text, Loader=_Loader)
    except yaml.MarkedYAMLError as exc:
        raise DraftRefusedError(
            DraftRefusalCode.TEST_YAML_UNREADABLE,
            f"the file is not YAML: {exc.problem}",
            _place(exc.problem_mark),
        ) from exc
    except yaml.YAMLError as exc:
        raise DraftRefusedError(
            DraftRefusalCode.TEST_YAML_UNREADABLE,
            f"the file is not YAML: {exc}",
            RefusalPlace(),
        ) from exc
    return screen_texts(_read_test(root), language=language)


def screen_texts(loaded: LoadedDraft, *, language: str | None) -> LoadedDraft:
    """Screen a draft's texts as they will be read; return them in NFC.

    The screens of Stage 1 read a file as it is written, and a YAML file is not
    yet what it says: a double-quoted string decodes its escapes only when it
    is parsed, after those screens. A JSON body decodes its own the same way.
    So the texts are screened again, as they will be stored and read: each in
    NFC, all of them one text, a field per line — the title, then each
    question's text, its options' texts and its explanation.

    Only the texts are read, never the format, so a draft read out of a JSON
    body takes the same call (task 07b, commit E1).

    Args:
        loaded: The title and the draft, as a format was read into them.
        language: The course language, ISO 639-3, as for the file's own screen.

    Returns:
        The same title and draft, every text in NFC.

    Raises:
        SecurityRejectedError: A screen refused a text; the category is the
            screen's.
    """
    normalized = LoadedDraft(
        title=None if loaded.title is None else _nfc(loaded.title),
        body=DraftBody(
            pass_threshold=loaded.body.pass_threshold,
            questions=tuple(
                DraftQuestion(
                    text=_nfc(question.text),
                    options=tuple(
                        DraftOption(text=_nfc(option.text), correct=option.correct)
                        for option in question.options
                    ),
                    explanation=(
                        None
                        if question.explanation is None
                        else _nfc(question.explanation)
                    ),
                )
                for question in loaded.body.questions
            ),
        ),
    )
    run_stage1(
        filename=_FIELDS_FILENAME,
        content="\n".join(_texts(normalized)).encode("utf-8"),
        context="authored",
        languages=(language,) if language else (),
    )
    return normalized


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _texts(loaded: LoadedDraft) -> list[str]:
    """Every text of a draft in reading order, the title first when it has one."""
    texts = [] if loaded.title is None else [loaded.title]
    for question in loaded.body.questions:
        texts.append(question.text)
        texts.extend(option.text for option in question.options)
        if question.explanation is not None:
            texts.append(question.explanation)
    return texts


def dump_test_yaml(draft: DraftBody, *, title: str | None = None) -> str:
    """The canonical YAML of a draft (section 6.4).

    Args:
        draft: The draft to write out.
        title: The test's title — a column of its document, not of the draft.

    Returns:
        YAML that :func:`load_test_yaml` reads back as the same title and draft.
    """
    document: dict[str, object] = {}
    if title is not None:
        document["title"] = title
    if draft.pass_threshold is not None:
        document["pass_threshold"] = draft.pass_threshold
    document["questions"] = [_dumped(question) for question in draft.questions]
    return yaml.safe_dump(
        document,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=10_000,
    )


def _dumped(question: DraftQuestion) -> dict[str, object]:
    dumped: dict[str, object] = {
        "text": question.text,
        "options": [
            {"text": option.text, "correct": option.correct}
            for option in question.options
        ],
    }
    if question.explanation is not None:
        dumped["explanation"] = question.explanation
    return dumped


# ── the walk ──────────────────────────────────────────────────────────


def _read_test(root: Node | None) -> LoadedDraft:
    if root is None:
        raise DraftRefusedError(
            DraftRefusalCode.TEST_FIELD_INVALID,
            "the file holds no test: a test needs questions",
            RefusalPlace(),
        )
    fields = _fields(root, _TEST_FIELDS, required=("questions",))
    title = (
        _text(fields["title"], "title", MAX_TITLE_CHARS) if "title" in fields else None
    )
    threshold = (
        _pass_threshold(fields["pass_threshold"])
        if "pass_threshold" in fields
        else None
    )
    listed = fields["questions"]
    items = _items(listed, "questions")
    if not 1 <= len(items) <= MAX_QUESTIONS:
        raise DraftRefusedError(
            DraftRefusalCode.TEST_FIELD_INVALID,
            f"a test has 1 to {MAX_QUESTIONS} questions; this one has {len(items)}",
            _place(listed.start_mark),
        )
    questions = tuple(
        _question(item, number) for number, item in enumerate(items, start=1)
    )
    return LoadedDraft(
        title=title, body=DraftBody(pass_threshold=threshold, questions=questions)
    )


def _question(node: Node, number: int) -> DraftQuestion:
    fields = _fields(
        node, _QUESTION_FIELDS, required=("text", "options"), question=number
    )
    text = _text(fields["text"], "text", MAX_QUESTION_CHARS, question=number)
    listed = fields["options"]
    items = _items(listed, "options", question=number)
    if not MIN_OPTIONS <= len(items) <= MAX_OPTIONS:
        raise DraftRefusedError(
            DraftRefusalCode.TEST_OPTIONS_COUNT,
            f"question {number} lists {len(items)}; a question has "
            f"{MIN_OPTIONS} to {MAX_OPTIONS} options",
            _place(listed.start_mark, question=number),
        )
    options = tuple(
        _option(item, number, position) for position, item in enumerate(items, start=1)
    )
    if not any(option.correct for option in options):
        raise DraftRefusedError(
            DraftRefusalCode.TEST_NO_CORRECT_OPTION,
            f"question {number} marks no option as correct",
            _place(listed.start_mark, question=number),
        )
    explanation = (
        _text(
            fields["explanation"], "explanation", MAX_EXPLANATION_CHARS, question=number
        )
        if "explanation" in fields
        else None
    )
    return DraftQuestion(text=text, options=options, explanation=explanation)


def _option(node: Node, question: int, option: int) -> DraftOption:
    fields = _fields(
        node,
        _OPTION_FIELDS,
        required=("text", "correct"),
        question=question,
        option=option,
    )
    return DraftOption(
        text=_text(
            fields["text"], "text", MAX_OPTION_CHARS, question=question, option=option
        ),
        correct=_mark(fields["correct"], question, option),
    )


def _fields(
    node: Node,
    allowed: tuple[str, ...],
    *,
    required: tuple[str, ...],
    question: int | None = None,
    option: int | None = None,
) -> dict[str, Node]:
    """The fields of a mapping node, each named once and every one known."""
    where = _where(question, option)
    if not isinstance(node, MappingNode):
        raise DraftRefusedError(
            DraftRefusalCode.TEST_FIELD_INVALID,
            f"{where}expected fields ({', '.join(allowed)})",
            _place(node.start_mark, question, option),
        )
    fields: dict[str, Node] = {}
    for key, value in node.value:
        if not isinstance(key, ScalarNode):
            raise DraftRefusedError(
                DraftRefusalCode.TEST_FIELD_INVALID,
                f"{where}a field name must be a plain name",
                _place(key.start_mark, question, option),
            )
        name: str = key.value
        if name in fields:
            raise DraftRefusedError(
                DraftRefusalCode.TEST_YAML_DUPLICATE_KEY,
                f"{where}{name!r} is given twice",
                _place(key.start_mark, question, option),
            )
        if name not in allowed:
            raise DraftRefusedError(
                DraftRefusalCode.TEST_FIELD_INVALID,
                f"{where}unknown field {name!r}; the fields are {', '.join(allowed)}",
                _place(key.start_mark, question, option),
            )
        fields[name] = value
    for name in required:
        if name not in fields:
            raise DraftRefusedError(
                DraftRefusalCode.TEST_FIELD_INVALID,
                f"{where}{name!r} is missing",
                _place(node.start_mark, question, option),
            )
    return fields


def _items(node: Node, name: str, *, question: int | None = None) -> list[Node]:
    if not isinstance(node, SequenceNode):
        raise DraftRefusedError(
            DraftRefusalCode.TEST_FIELD_INVALID,
            f"{_where(question, None)}{name} must be a list",
            _place(node.start_mark, question),
        )
    items: list[Node] = node.value
    return items


def _text(
    node: Node,
    name: str,
    limit: int,
    *,
    question: int | None = None,
    option: int | None = None,
) -> str:
    """A text as written, trimmed: never what its tag would make of it."""
    where = _where(question, option)
    if not isinstance(node, ScalarNode):
        raise DraftRefusedError(
            DraftRefusalCode.TEST_FIELD_INVALID,
            f"{where}{name} must be plain text",
            _place(node.start_mark, question, option),
        )
    raw: str = node.value
    text = raw.strip()
    if not text:
        raise DraftRefusedError(
            DraftRefusalCode.TEST_FIELD_INVALID,
            f"{where}{name} is empty",
            _place(node.start_mark, question, option),
        )
    if len(text) > limit:
        raise DraftRefusedError(
            DraftRefusalCode.TEST_FIELD_INVALID,
            f"{where}{name} has {len(text)} characters; at most {limit}",
            _place(node.start_mark, question, option),
        )
    return text


def _mark(node: Node, question: int, option: int) -> bool:
    """``correct``: the boolean ``true`` or ``false``, and nothing that reads as one."""
    if isinstance(node, ScalarNode) and node.tag == _BOOL_TAG:
        raw: str = node.value
        if raw in ("true", "false"):
            return raw == "true"
    raise DraftRefusedError(
        DraftRefusalCode.TEST_FIELD_INVALID,
        f"{_where(question, option)}correct must be true or false",
        _place(node.start_mark, question, option),
    )


def _pass_threshold(node: Node) -> int:
    """``pass_threshold``: a whole number written in digits, within its range."""
    if isinstance(node, ScalarNode) and node.tag == _INT_TAG:
        raw: str = node.value
        if (
            _WHOLE_NUMBER.fullmatch(raw)
            and MIN_PASS_THRESHOLD <= int(raw) <= MAX_PASS_THRESHOLD
        ):
            return int(raw)
    raise DraftRefusedError(
        DraftRefusalCode.TEST_FIELD_INVALID,
        f"pass_threshold must be a whole number from {MIN_PASS_THRESHOLD} to "
        f"{MAX_PASS_THRESHOLD}",
        _place(node.start_mark),
    )


def _where(question: int | None, option: int | None) -> str:
    """The question and option a detail is about, as its prefix."""
    if question is None:
        return ""
    if option is None:
        return f"question {question}: "
    return f"question {question}, option {option}: "


def _place(
    mark: Mark | None, question: int | None = None, option: int | None = None
) -> RefusalPlace:
    """A place from a mark of the composer, which counts from 0."""
    if mark is None:
        return RefusalPlace(question=question, option=option)
    return RefusalPlace(
        line=mark.line + 1, column=mark.column + 1, question=question, option=option
    )
