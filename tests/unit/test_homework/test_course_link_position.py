"""Where a course link points, from a segment's anchor (task 10, K4, lock v).

Every kind of source on the list, both sides of the hour for the two that are
timed, and the anchors that hold no place to name.
"""

from __future__ import annotations

import doctest

import pytest

from course_supporter.homework import course_link_position
from course_supporter.homework.course_link_position import position_of
from course_supporter.models.review_structure import Position
from course_supporter.storage.document_segment_repository import SegmentAnchor


class TestEachKindOfSource:
    @pytest.mark.parametrize(
        ("source_type", "anchor", "expected"),
        [
            (
                "video",
                SegmentAnchor("time", 221.7, 260.0),
                Position(kind="video", value="03:41"),
            ),
            (
                "audio",
                SegmentAnchor("time", 221.7, 260.0),
                Position(kind="audio", value="03:41"),
            ),
            (
                "presentation",
                SegmentAnchor("slide", 12, 14),
                Position(kind="slide", value="12"),
            ),
            (
                "text",
                SegmentAnchor("paragraph", 7, 9),
                Position(kind="paragraph", value="7"),
            ),
            (
                "web",
                SegmentAnchor("paragraph", 3, 3),
                Position(kind="paragraph", value="3"),
            ),
            (
                "code",
                SegmentAnchor("file", None, None, file_path="src/app/main.py"),
                Position(kind="file", value="src/app/main.py"),
            ),
        ],
        ids=["video", "audio", "presentation", "text", "web", "code"],
    )
    def test_the_source_type_decides_the_kind(
        self, source_type: str, anchor: SegmentAnchor, expected: Position
    ) -> None:
        assert position_of(anchor, source_type) == expected


class TestTime:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "00:00"),
            (5.9, "00:05"),
            (221.7, "03:41"),
            (3599.99, "59:59"),
            (3600, "1:00:00"),
            (3725, "1:02:05"),
            (36125.5, "10:02:05"),
        ],
    )
    def test_minutes_below_the_hour_hours_from_it(
        self, seconds: float, expected: str
    ) -> None:
        """``mm:ss`` below an hour, ``h:mm:ss`` from it; seconds rounded down,
        so the student never lands past the fragment's first words."""
        position = position_of(SegmentAnchor("time", seconds, None), "video")

        assert position == Position(kind="video", value=expected)

    def test_a_recording_is_not_called_a_video(self) -> None:
        position = position_of(SegmentAnchor("time", 3725, 3800), "audio")

        assert position == Position(kind="audio", value="1:02:05")


class TestNoPlaceToName:
    @pytest.mark.parametrize("source_type", ["text", "web"])
    def test_a_paragraph_never_counted_has_no_position(self, source_type: str) -> None:
        """The KD-C edge: a text segment whose ordinal was never computed."""
        assert position_of(SegmentAnchor("paragraph", None, None), source_type) is None

    @pytest.mark.parametrize(
        ("source_type", "anchor"),
        [
            ("video", SegmentAnchor("time", None, None)),
            ("presentation", SegmentAnchor("slide", None, None)),
            ("code", SegmentAnchor("file", None, None)),
        ],
        ids=["time", "slide", "file"],
    )
    def test_an_empty_anchor_of_another_kind_has_none_either(
        self, source_type: str, anchor: SegmentAnchor
    ) -> None:
        assert position_of(anchor, source_type) is None


class TestRefusals:
    def test_a_source_type_with_no_place_is_refused(self) -> None:
        """A test object has no fragments to point at; guessing a kind would
        print a phrase about a place that does not exist."""
        with pytest.raises(ValueError, match="test_object"):
            position_of(SegmentAnchor("paragraph", 1, 1), "test_object")

    def test_an_anchor_of_the_wrong_kind_is_refused(self) -> None:
        with pytest.raises(ValueError, match="does not belong"):
            position_of(SegmentAnchor("slide", 3, 3), "video")


class TestDocstrings:
    def test_the_examples_in_the_module_run(self) -> None:
        results = doctest.testmod(course_link_position)

        assert results.failed == 0
        assert results.attempted > 0
