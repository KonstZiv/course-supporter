"""A written test: letters, published form, digests, prompt text (task 07b).

What is pinned here, and to what:

* **Letters** — to decision 16 of ``07b-test-object/TASK.md``, letter for letter.
* **Digests** — to golden values. A version's digests are stored beside it and
  compared at every publication, so a recipe that moved them would make every
  stored version differ from an identical draft: a version nobody asked for
  and, when the visible digest moves, explanations paid for twice. The fixture
  is the polygon's test (``06-reference/TEST-SOURCE.md``, byte for byte in
  ``tests/fixtures/reference/``) with the key of its first production reference
  version, a pass mark and one explanation of the author's own. Its key digest
  is not ours to choose: it is the digest measured on production for that key
  (``07-test-submission/PROBE-REPORT.md``, section 8), because the object
  reuses the answers axis of task 06 as it is. The other two were computed by
  this module when it was written (2026-09-25) and are pinned from then on.
* **The prompt text** — to that same file: the polygon's test, published, reads
  to the explanation model exactly as its author typed it.
* **The module's examples run** — explicitly, because the gate collects no
  doctests (``DD-SP-BC``).
"""

from __future__ import annotations

import doctest
import hashlib
import string
from pathlib import Path

import pytest
from pydantic import ValidationError

from course_supporter.homework import test_object
from course_supporter.homework.test_object import (
    MAX_OPTIONS,
    DraftBody,
    DraftOption,
    DraftQuestion,
    PublishedBody,
    VersionDigests,
    option_letters,
    published_form,
    render_for_prompt,
    version_digests,
)
from course_supporter.homework.test_text import canonical_label

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "reference" / "test_source.md"
_FIXTURE_SHA256 = "ed51186e7369897324997a032dd011597ef6467c4084bbc9b2960489199da72a"

_UKRAINIAN_LETTERS = tuple("абвгдеєжзиіїклмнопрстуфхцч")
"""Decision 16: the Ukrainian alphabet without three of its letters, first 26."""

_POLYGON_KEY: dict[str, list[str]] = {
    "1": ["б"],
    "2": ["в"],
    "3": ["а"],
    "4": ["г"],
    "5": ["в"],
}
"""The key of the polygon's first production reference version."""

_POLYGON_KEY_DIGEST = "f5ae072d8b297daacdd3904453ae8e0af8441246790c797f8620afb7fd01a47f"
"""Its answers digest, as measured on production."""

_CONTENT_DIGEST = "84888f1b0973cb8d38f4be8ce04c94d6eeb75c04da82fd8a9567037fba4f54a5"
_PUBLICATION_DIGEST = "497d9a904f93e8c5cf7a7f09760482f0bbc61dacd25a19463f659826b757efe5"

_OWN_EXPLANATION = "Тест одразу після зміни показує, чи вона нічого не зламала."

_POLYGON = DraftBody(
    pass_threshold=80,
    questions=(
        DraftQuestion(
            text="Що таке контекстне вікно моделі?",
            options=(
                DraftOption(
                    text="Вікно редактора, у якому агент показує зміни", correct=False
                ),
                DraftOption(
                    text="Обсяг тексту, який модель бачить за один виклик", correct=True
                ),
                DraftOption(
                    text="Час, протягом якого сесія агента лишається активною",
                    correct=False,
                ),
                DraftOption(
                    text="Перелік файлів, які агентові дозволено змінювати",
                    correct=False,
                ),
            ),
        ),
        DraftQuestion(
            text=(
                "Агент додав у код імпорт бібліотеки, про яку ви ніколи не чули. Що "
                "зробити насамперед?"
            ),
            options=(
                DraftOption(
                    text="Прийняти зміну: агент знає бібліотеки краще", correct=False
                ),
                DraftOption(
                    text="Попросити агента переписати код без імпортів", correct=False
                ),
                DraftOption(
                    text=(
                        "Перевірити, що бібліотека справді існує і має потрібну функцію"
                    ),
                    correct=True,
                ),
                DraftOption(text="Видалити імпорт і запустити програму", correct=False),
            ),
        ),
        DraftQuestion(
            text="Навіщо запускати тести після кожної зміни, яку зробив агент?",
            options=(
                DraftOption(
                    text="Щоб переконатися, що зміна нічого не зламала", correct=True
                ),
                DraftOption(
                    text="Щоб агент навчився на результатах тестів", correct=False
                ),
                DraftOption(text="Щоб збільшити покриття коду", correct=False),
                DraftOption(text="Тести потрібні лише перед випуском", correct=False),
            ),
            explanation=_OWN_EXPLANATION,
        ),
        DraftQuestion(
            text="Що з переліченого — виклик інструмента в циклі агента?",
            options=(
                DraftOption(
                    text="Модель повертає остаточну відповідь користувачеві",
                    correct=False,
                ),
                DraftOption(
                    text="Користувач копіює код із відповіді моделі у файл",
                    correct=False,
                ),
                DraftOption(text="Редактор підсвічує синтаксис", correct=False),
                DraftOption(
                    text=(
                        "Модель просить виконати дію, наприклад прочитати файл, і "
                        "отримує результат"
                    ),
                    correct=True,
                ),
            ),
        ),
        DraftQuestion(
            text="Яке формулювання завдання для агента найкраще?",
            options=(
                DraftOption(text="«Зроби код кращим»", correct=False),
                DraftOption(text="«Виправ усі помилки в проєкті»", correct=False),
                DraftOption(
                    text=(
                        "«Додай у функцію parse_date перевірку порожнього рядка і "
                        "тест на цей випадок»"
                    ),
                    correct=True,
                ),
                DraftOption(text="«Перепиши проєкт на сучасний лад»", correct=False),
            ),
        ),
    ),
)
"""The polygon's test as a draft, with a pass mark and question 3 explained."""


def _digests(draft: DraftBody, language: str = "ukr") -> VersionDigests:
    return version_digests(published_form(draft, language), language)


def _edited(draft: DraftBody, index: int, **changes: object) -> DraftBody:
    """The draft with one of its questions changed."""
    questions = list(draft.questions)
    questions[index] = questions[index].model_copy(update=changes)
    return draft.model_copy(update={"questions": tuple(questions)})


def _key_moved(draft: DraftBody) -> DraftBody:
    """The draft with the right option of question 1 moved to its first option."""
    options = tuple(
        option.model_copy(update={"correct": position == 0})
        for position, option in enumerate(draft.questions[0].options)
    )
    return _edited(draft, 0, options=options)


def _one_question(*marks: bool, text: str = "Which one?") -> DraftBody:
    """A draft of one question with an option per mark."""
    options = tuple(
        DraftOption(text=f"option {n}", correct=mark)
        for n, mark in enumerate(marks, start=1)
    )
    return DraftBody(questions=(DraftQuestion(text=text, options=options),))


@pytest.fixture(scope="module")
def typed_questions() -> str:
    """The questions of the polygon's test as its author typed them."""
    raw = _FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == _FIXTURE_SHA256
    _title, _instructions, questions = raw.decode("utf-8").split("\n\n", 2)
    return questions.rstrip("\n")


class TestLetters:
    def test_a_ukrainian_test_is_lettered_as_decision_16_says(self) -> None:
        assert option_letters("ukr") == _UKRAINIAN_LETTERS

    @pytest.mark.parametrize("language", ["eng", "deu", "pol", None])
    def test_any_other_language_gets_the_small_latin_letters(
        self, language: str | None
    ) -> None:
        assert option_letters(language) == tuple(string.ascii_lowercase)

    @pytest.mark.parametrize("language", ["ukr", "eng"])
    def test_no_two_letters_share_a_canonical_label(self, language: str) -> None:
        """The doors and the scoring compare canonical labels: two letters of
        one canonical form would be one answer."""
        letters = option_letters(language)

        assert len(letters) == MAX_OPTIONS
        assert len({canonical_label(letter) for letter in letters}) == MAX_OPTIONS


class TestPublishedForm:
    def test_questions_are_numbered_and_options_lettered_in_order(self) -> None:
        published = published_form(_POLYGON, "ukr")

        assert [q.number for q in published.questions] == ["1", "2", "3", "4", "5"]
        assert {tuple(o.label for o in q.options) for q in published.questions} == {
            _UKRAINIAN_LETTERS[:4]
        }
        assert published.answer_key() == _POLYGON_KEY
        assert [q.text for q in published.questions] == [
            q.text for q in _POLYGON.questions
        ]
        assert [[o.text for o in q.options] for q in published.questions] == [
            [o.text for o in q.options] for q in _POLYGON.questions
        ]
        assert published.pass_threshold == 80
        assert published.explanations() == {"3": _OWN_EXPLANATION}

    def test_the_course_language_chooses_the_letters(self) -> None:
        published = published_form(_POLYGON, "eng")

        assert published.answer_key() == {
            "1": ["b"],
            "2": ["c"],
            "3": ["a"],
            "4": ["d"],
            "5": ["c"],
        }

    def test_the_key_lists_every_right_option_in_order(self) -> None:
        published = published_form(_one_question(True, False, True), "ukr")

        assert published.answer_key() == {"1": list(_UKRAINIAN_LETTERS[0:3:2])}

    def test_every_letter_can_be_used_and_no_more(self) -> None:
        widest = published_form(_one_question(*[False] * MAX_OPTIONS), "eng")

        assert widest.questions[0].options[-1].label == "z"
        with pytest.raises(ValueError, match="Question 1 has 27 options"):
            published_form(_one_question(*[False] * (MAX_OPTIONS + 1)), "eng")

    def test_a_draft_carries_no_letters_and_no_numbers(self) -> None:
        """Both are the system's to set (decision 16)."""
        with_letter = _POLYGON.to_jsonb()
        with_letter["questions"][0]["options"][0]["label"] = "a"
        with_number = _POLYGON.to_jsonb()
        with_number["questions"][0]["number"] = "1"

        with pytest.raises(ValidationError, match="label"):
            DraftBody.from_jsonb(with_letter)
        with pytest.raises(ValidationError, match="number"):
            DraftBody.from_jsonb(with_number)

    def test_a_mark_is_a_boolean_not_a_word(self) -> None:
        payload = _POLYGON.to_jsonb()
        payload["questions"][0]["options"][0]["correct"] = "false"

        with pytest.raises(ValidationError, match="correct"):
            DraftBody.from_jsonb(payload)

    def test_both_bodies_survive_their_columns(self) -> None:
        published = published_form(_POLYGON, "ukr")

        assert DraftBody.from_jsonb(_POLYGON.to_jsonb()) == _POLYGON
        assert PublishedBody.from_jsonb(published.to_jsonb()) == published
        stored = published.to_jsonb()
        assert sorted(stored) == ["pass_threshold", "questions"]
        assert sorted(stored["questions"][0]) == [
            "explanation",
            "number",
            "options",
            "text",
        ]
        assert sorted(stored["questions"][0]["options"][0]) == [
            "correct",
            "label",
            "text",
        ]


class TestDigests:
    def test_the_digests_of_the_fixture_are_golden(self) -> None:
        assert _digests(_POLYGON) == VersionDigests(
            content_digest=_CONTENT_DIGEST,
            answers_digest=_POLYGON_KEY_DIGEST,
            publication_digest=_PUBLICATION_DIGEST,
        )

    @pytest.mark.parametrize(
        "edited",
        [
            pytest.param(
                _POLYGON.model_copy(update={"pass_threshold": 60}),
                id="pass-mark-changed",
            ),
            pytest.param(
                _POLYGON.model_copy(update={"pass_threshold": None}),
                id="pass-mark-removed",
            ),
            pytest.param(
                _edited(_POLYGON, 2, explanation="Тест ловить зламане одразу."),
                id="explanation-changed",
            ),
            pytest.param(
                _edited(_POLYGON, 0, explanation="Вікно — межа того, що видно."),
                id="explanation-added",
            ),
        ],
    )
    def test_editing_only_the_pass_mark_or_own_explanations_moves_only_the_full_digest(
        self, edited: DraftBody
    ) -> None:
        """Decision 9: a new version, the same ``version`` for the student, and
        the same axes for the explanations — so no new explanations."""
        before, after = _digests(_POLYGON), _digests(edited)

        assert after.content_digest == before.content_digest
        assert after.answers_digest == before.answers_digest
        assert after.publication_digest != before.publication_digest

    def test_editing_a_text_moves_the_visible_digest_and_not_the_key(self) -> None:
        before = _digests(_POLYGON)
        after = _digests(_edited(_POLYGON, 0, text="Що таке вікно моделі?"))

        assert after.content_digest != before.content_digest
        assert after.answers_digest == before.answers_digest
        assert after.publication_digest != before.publication_digest

    def test_moving_the_key_moves_its_digest_and_not_the_visible_one(self) -> None:
        before, after = _digests(_POLYGON), _digests(_key_moved(_POLYGON))

        assert after.content_digest == before.content_digest
        assert after.answers_digest != before.answers_digest
        assert after.publication_digest != before.publication_digest

    def test_the_letters_are_part_of_what_the_student_sees(self) -> None:
        assert (
            _digests(_POLYGON, "ukr").content_digest
            != _digests(_POLYGON, "eng").content_digest
        )

    def test_the_language_itself_enters_the_full_digest_only(self) -> None:
        english, german = _digests(_POLYGON, "eng"), _digests(_POLYGON, "deu")

        assert english.content_digest == german.content_digest
        assert english.answers_digest == german.answers_digest
        assert english.publication_digest != german.publication_digest


class TestPromptText:
    def test_the_polygon_test_reads_as_its_author_typed_it(
        self, typed_questions: str
    ) -> None:
        """No mark, no pass mark and no explanation of the author's reach the
        model through the text: it gets the key separately, and the author's
        explanations not at all."""
        assert render_for_prompt(published_form(_POLYGON, "ukr")) == typed_questions

    def test_no_line_inside_a_text_can_pass_for_a_question_or_an_option(self) -> None:
        draft = DraftBody(
            questions=(
                DraftQuestion(
                    text="What does it print?\n2. print(1)\n\nand then?",
                    options=(
                        DraftOption(text="b) 1\n2", correct=True),
                        DraftOption(text="1", correct=False),
                    ),
                ),
            )
        )

        assert render_for_prompt(published_form(draft, "eng")) == (
            "1. What does it print?\n"
            "   2. print(1)\n"
            "\n"
            "   and then?\n"
            "a) b) 1\n"
            "   2\n"
            "b) 1"
        )


def test_the_module_examples_are_executed() -> None:
    """Run the docstring examples explicitly — the gate does not (``DD-SP-BC``).

    Both halves are asserted: ``failed == 0`` is also true of a module whose
    examples were all deleted.
    """
    results = doctest.testmod(test_object, verbose=False)
    assert results.attempted > 0, "the module's documentation lost its examples"
    assert results.failed == 0
