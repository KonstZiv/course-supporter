"""Tests for the per-language phrase files (mentor-rebuild task 04).

Fixtures are real YAML files in a temporary directory, not mocks: the loader's
subject IS the file shape, and a mock would assert the shape we imagined
(impl-rules#13).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from course_supporter.phrasebook import (
    SOURCE_LANGUAGE,
    load_language_file,
    load_phrasebook,
    phrases_for,
    validate_phrasebook,
)


def _write(directory: Path, code: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{code}.yaml"
    path.write_text(body, encoding="utf-8")
    return path


_SOURCE = "section.fixed: Виправлено\nsection.open: Відкрите\n"


class TestLanguageFile:
    def test_a_plain_string_is_a_machine_translation(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "pol", "section.fixed: Poprawione\n")

        phrases = load_language_file(path)

        assert phrases["section.fixed"].text == "Poprawione"
        assert phrases["section.fixed"].reviewed is False

    def test_a_record_carries_the_review_mark(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            "pol",
            "section.fixed:\n  text: Poprawione\n  reviewed: true\n"
            "section.open:\n  text: Otwarte\n",
        )

        phrases = load_language_file(path)

        assert phrases["section.fixed"].reviewed is True
        # Absent mark reads as "not reviewed", never as "reviewed".
        assert phrases["section.open"].reviewed is False

    @pytest.mark.parametrize(
        "body",
        [
            "section.fixed: 42\n",
            "section.fixed:\n  text: 42\n  reviewed: true\n",
            "section.fixed:\n  text: Poprawione\n  reviewed: maybe\n",
        ],
        ids=["not-a-phrase", "text-not-a-string", "mark-not-a-boolean"],
    )
    def test_a_phrase_of_neither_shape_is_refused(
        self, tmp_path: Path, body: str
    ) -> None:
        path = _write(tmp_path, "pol", body)

        with pytest.raises(ValueError, match=re.escape("section.fixed")):
            load_language_file(path)

    def test_a_file_that_is_not_a_mapping_is_refused(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "pol", "- Poprawione\n")

        with pytest.raises(ValueError, match="mapping of phrase key"):
            load_language_file(path)

    def test_a_missing_file_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_language_file(tmp_path / "pol.yaml")


class TestPhrasebook:
    def test_every_file_of_the_directory_by_code(self, tmp_path: Path) -> None:
        _write(tmp_path, SOURCE_LANGUAGE, _SOURCE)
        _write(tmp_path, "eng", "section.fixed: Fixed\nsection.open: Open\n")

        book = load_phrasebook(tmp_path)

        assert set(book) == {SOURCE_LANGUAGE, "eng"}
        assert book["eng"]["section.open"].text == "Open"

    def test_a_missing_directory_says_so(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_phrasebook(tmp_path / "nowhere")

    def test_the_review_language_wins(self, tmp_path: Path) -> None:
        _write(tmp_path, SOURCE_LANGUAGE, _SOURCE)
        _write(tmp_path, "eng", "section.fixed: Fixed\nsection.open: Open\n")
        _write(tmp_path, "fas", "section.fixed: اصلاح‌شده\nsection.open: باز\n")

        assert phrases_for("fas", tmp_path)["section.fixed"] == "اصلاح‌شده"

    def test_no_language_resolved_reads_english(self, tmp_path: Path) -> None:
        _write(tmp_path, SOURCE_LANGUAGE, _SOURCE)
        _write(tmp_path, "eng", "section.fixed: Fixed\nsection.open: Open\n")

        # ``resolve_review_language`` returns None when all three sources are
        # empty or unusable; that is the one case this fallback is for.
        assert phrases_for(None, tmp_path)["section.fixed"] == "Fixed"
        assert phrases_for("pol", tmp_path)["section.fixed"] == "Fixed"

    def test_english_missing_falls_to_the_source(self, tmp_path: Path) -> None:
        _write(tmp_path, SOURCE_LANGUAGE, _SOURCE)

        assert phrases_for(None, tmp_path)["section.fixed"] == "Виправлено"

    def test_nothing_to_fall_back_on_says_so(self, tmp_path: Path) -> None:
        _write(tmp_path, "pol", "section.fixed: Poprawione\n")

        with pytest.raises(ValueError, match="No phrases to fall back on"):
            phrases_for(None, tmp_path)


class TestStartupCheck:
    def test_a_complete_phrasebook_passes(self, tmp_path: Path) -> None:
        _write(tmp_path, SOURCE_LANGUAGE, _SOURCE)
        _write(tmp_path, "eng", "section.fixed: Fixed\nsection.open: Open\n")

        validate_phrasebook(tmp_path, [SOURCE_LANGUAGE, "eng"])

    def test_a_missing_phrase_stops_the_boot(self, tmp_path: Path) -> None:
        _write(tmp_path, SOURCE_LANGUAGE, _SOURCE)
        _write(tmp_path, "eng", "section.fixed: Fixed\n")

        with pytest.raises(ValueError) as exc_info:
            validate_phrasebook(tmp_path, [SOURCE_LANGUAGE, "eng"])

        assert "'eng' is missing the phrase 'section.open'" in str(exc_info.value)

    def test_a_phrase_the_source_does_not_have_stops_the_boot(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, SOURCE_LANGUAGE, _SOURCE)
        _write(
            tmp_path,
            "eng",
            "section.fixed: Fixed\nsection.open: Open\nsection.ghost: Ghost\n",
        )

        with pytest.raises(ValueError, match=re.escape("section.ghost")):
            validate_phrasebook(tmp_path, [SOURCE_LANGUAGE, "eng"])

    def test_an_allowed_language_without_a_file_stops_the_boot(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, SOURCE_LANGUAGE, _SOURCE)

        with pytest.raises(ValueError, match="'eng' is allowed but has no phrase file"):
            validate_phrasebook(tmp_path, [SOURCE_LANGUAGE, "eng"])

    def test_a_file_outside_the_language_list_stops_the_boot(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, SOURCE_LANGUAGE, _SOURCE)
        _write(tmp_path, "pol", "section.fixed: Poprawione\nsection.open: Otwarte\n")

        with pytest.raises(ValueError, match="'pol' is not an allowed language"):
            validate_phrasebook(tmp_path, [SOURCE_LANGUAGE])

    def test_the_source_file_missing_stops_the_boot(self, tmp_path: Path) -> None:
        _write(tmp_path, "eng", "section.fixed: Fixed\n")

        with pytest.raises(ValueError, match="source language"):
            validate_phrasebook(tmp_path, ["eng"])

    def test_every_fault_is_reported_at_once(self, tmp_path: Path) -> None:
        _write(tmp_path, SOURCE_LANGUAGE, _SOURCE)
        _write(tmp_path, "eng", "section.fixed: Fixed\n")  # one key short
        _write(tmp_path, "pol", "section.fixed: Poprawione\n")  # short and unlisted

        with pytest.raises(ValueError) as exc_info:
            validate_phrasebook(tmp_path, [SOURCE_LANGUAGE, "eng", "fas"])

        message = str(exc_info.value)
        # An operator reading a refused boot should not have to fix one fault,
        # restart, and find the next.
        assert "'fas' is allowed but has no phrase file" in message
        assert "'pol' is not an allowed language" in message
        assert "'eng' is missing the phrase 'section.open'" in message
