"""The styles of the Mentor's joke on a hostile reply (task 12a, K4).

The shipped set is the product owner's (2026-11-16), approved by the operator
after the live measurement; the code, never the model, picks a style from the
reply's id — the same reply, the same style, in every process.
"""

from __future__ import annotations

import doctest
import hashlib
from pathlib import Path

import pytest

from course_supporter.homework import reply_humor
from course_supporter.homework.reply_humor import (
    HUMOR_STYLES_PATH,
    HumorStyle,
    choose_style,
    get_humor_styles,
    load_humor_styles,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


class TestTheShippedSet:
    def test_it_is_the_product_owners_four_in_their_order(self) -> None:
        styles = load_humor_styles(_REPO_ROOT / HUMOR_STYLES_PATH)

        assert [style.id for style in styles] == [
            "odesa",
            "shed",
            "self_irony",
            "honest_program",
        ]

    def test_each_example_is_the_one_the_product_owner_wrote(self) -> None:
        examples = {
            style.id: style.example
            for style in load_humor_styles(_REPO_ROOT / HUMOR_STYLES_PATH)
        }

        assert "дві великі різниці" in examples["odesa"]
        # Reworded by the product owner on 2026-11-17, word for word.
        assert examples["shed"] == (
            "Знаєте жарт про напис на сараї і те, що там лежить? Ми дивимося на те, "
            "що лежить. Тому під час оцінювання враховується лише файл чи архів, а "
            "не коментарі."
        )
        assert examples["self_irony"] == (
            "Я, звісно, люблю добре слово, але бали ставлю лише за роботу."
        )
        assert "програма в мене одна" in examples["honest_program"]

    def test_the_styles_speak_of_the_work_not_of_code(self) -> None:
        """The platform checks more than programming (2026-11-17)."""
        styles = load_humor_styles(_REPO_ROOT / HUMOR_STYLES_PATH)
        self_irony = next(s for s in styles if s.id == "self_irony")

        assert "work" in self_irony.description
        assert "code" not in self_irony.description
        assert "за код" not in self_irony.example

    def test_the_cache_reads_the_file_once(self) -> None:
        path = _REPO_ROOT / HUMOR_STYLES_PATH

        assert get_humor_styles(path) is get_humor_styles(path)


class TestTheChoice:
    def test_the_same_reply_gets_the_same_style_in_every_process(self) -> None:
        styles = load_humor_styles(_REPO_ROOT / HUMOR_STYLES_PATH)
        comment_id = "0196f0e2-6c1a-7a3b-9d55-4c2f8f0a1b2c"
        digest = hashlib.sha256(comment_id.encode("utf-8")).digest()

        expected = styles[int.from_bytes(digest[:8], "big") % len(styles)]

        assert choose_style(comment_id, styles) == expected

    def test_every_style_is_reached(self) -> None:
        styles = load_humor_styles(_REPO_ROOT / HUMOR_STYLES_PATH)

        chosen = {choose_style(f"reply-{n}", styles).id for n in range(200)}

        assert chosen == {style.id for style in styles}

    def test_no_style_to_choose_from_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no humor style"):
            choose_style("x", [])


class TestAFileThatIsRefused:
    @pytest.mark.parametrize(
        ("body", "fault"),
        [
            ("styles: []", "at least 1"),
            (
                "styles:\n"
                "  - {id: a, description: d, example: e}\n"
                "  - {id: a, description: d, example: e}\n",
                "style ids repeat",
            ),
            ("styles:\n  - {id: Odesa, description: d, example: e}\n", "pattern"),
            ("styles:\n  - {id: a, description: ' ', example: e}\n", "at least 1"),
            ("styles:\n  - {id: a, description: d}\n", "example"),
            ("styles:\n  - {id: a, description: d, example: e, tone: x}\n", "tone"),
            ("styles: [", "Failed to parse"),
        ],
    )
    def test_with_every_fault_named(
        self, tmp_path: Path, body: str, fault: str
    ) -> None:
        path = tmp_path / "styles.yaml"
        path.write_text(body, encoding="utf-8")

        with pytest.raises(ValueError, match=fault):
            load_humor_styles(path)

    def test_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_humor_styles(tmp_path / "none.yaml")


def test_a_style_is_frozen() -> None:
    style = HumorStyle(id="a", description="d", example="e")

    with pytest.raises(ValueError):
        style.id = "b"


def test_the_modules_examples() -> None:
    """The gate does not collect doctests; this runs them."""
    assert doctest.testmod(reply_humor).failed == 0
