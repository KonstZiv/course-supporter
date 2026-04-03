"""Tests for cross-modal alignment (STT ↔ VD)."""

from __future__ import annotations

from course_supporter.ingestion.alignment import (
    CrossModalAligner,
    _extract_identifiers,
    _is_near_match,
)
from course_supporter.ingestion.stop_words import build_stop_words
from course_supporter.models.source import ChunkType, ContentChunk

_TEST_STOP = build_stop_words(natural_lang="en", programming_lang="python")


def _stt(text: str, start: float, end: float, idx: int = 0) -> ContentChunk:
    return ContentChunk(
        chunk_type=ChunkType.TRANSCRIPT,
        text=text,
        index=idx,
        start_sec=start,
        end_sec=end,
    )


def _vd(
    text: str,
    start: float,
    end: float,
    scene_id: int = 0,
) -> ContentChunk:
    return ContentChunk(
        chunk_type=ChunkType.VISUAL_SCENE,
        text=text,
        index=0,
        start_sec=start,
        end_sec=end,
        metadata={"scene_id": scene_id, "scene_type": "screen_recording"},
    )


class TestExtractIdentifiers:
    def test_extracts_function_names(self) -> None:
        ids = _extract_identifiers("def calculate_total(items):", _TEST_STOP)
        assert "calculate_total" in ids
        assert "items" in ids

    def test_excludes_stop_words(self) -> None:
        ids = _extract_identifiers(
            "the function is not very important",
            _TEST_STOP,
        )
        assert "function" not in ids
        assert "important" in ids

    def test_excludes_short(self) -> None:
        ids = _extract_identifiers("a b cd", _TEST_STOP)
        assert len(ids) == 0

    def test_dotted_identifiers(self) -> None:
        ids = _extract_identifiers("os.path.join works here", _TEST_STOP)
        assert "os.path.join" in ids


class TestIsNearMatch:
    def test_exact_no_match(self) -> None:
        assert _is_near_match("hello", "hello") is False

    def test_one_char_diff(self) -> None:
        assert _is_near_match("fa12", "faa12") is True

    def test_two_char_diff(self) -> None:
        assert _is_near_match("abcde", "abxye") is True

    def test_too_different(self) -> None:
        assert _is_near_match("abc", "xyz") is False

    def test_short_words_ignored(self) -> None:
        assert _is_near_match("ab", "ac") is False


class TestTemporalMatch:
    def test_basic_overlap(self) -> None:
        aligner = CrossModalAligner()
        stt = [_stt("hello world", 0.0, 10.0)]
        vd = [_vd("Code editor", 2.0, 8.0, scene_id=0)]

        report = aligner.align(stt, vd)

        assert len(report.segments) == 1
        assert report.segments[0].stt_text == "hello world"
        assert report.segments[0].vd_scene_id == 0

    def test_no_overlap(self) -> None:
        aligner = CrossModalAligner(window_sec=5.0)
        stt = [_stt("early", 0.0, 5.0)]
        vd = [_vd("late scene", 100.0, 110.0, scene_id=0)]

        report = aligner.align(stt, vd)

        assert len(report.segments) == 1
        assert report.segments[0].stt_text is None

    def test_multiple_stt_for_one_vd(self) -> None:
        aligner = CrossModalAligner()
        stt = [
            _stt("part one", 0.0, 15.0, idx=0),
            _stt("part two", 15.0, 30.0, idx=1),
        ]
        vd = [_vd("Long scene", 5.0, 25.0, scene_id=0)]

        report = aligner.align(stt, vd)

        assert len(report.segments) == 1
        assert "part one" in report.segments[0].stt_text
        assert "part two" in report.segments[0].stt_text

    def test_window_tolerance(self) -> None:
        """STT slightly before VD still matches with window."""
        aligner = CrossModalAligner(window_sec=10.0, overlap_min_sec=1.0)
        stt = [_stt("preparing", 0.0, 8.0)]
        vd = [_vd("Code appears", 10.0, 20.0, scene_id=0)]

        report = aligner.align(stt, vd)

        # VD window extends to [0, 30], STT [0, 8] overlaps
        assert report.segments[0].stt_text == "preparing"


class TestSemanticLink:
    def test_shared_identifiers(self) -> None:
        aligner = CrossModalAligner()
        stt = [_stt("now let's use calculate_total function", 0.0, 10.0)]
        vd = [_vd("def calculate_total(items): return sum(items)", 0.0, 10.0)]

        report = aligner.align(stt, vd)

        assert report.segments[0].semantic_overlap > 0.0
        assert report.segments[0].alignment_confidence > 0.0

    def test_no_shared_identifiers(self) -> None:
        aligner = CrossModalAligner()
        stt = [_stt("good morning everyone", 0.0, 10.0)]
        vd = [_vd("PyCharm IDE toolbar buttons", 0.0, 10.0)]

        report = aligner.align(stt, vd)

        assert report.segments[0].semantic_overlap == 0.0


class TestConflictDetection:
    def test_near_match_flagged(self) -> None:
        """fa12 vs faa12 scenario."""
        aligner = CrossModalAligner()
        stt = [_stt("variable fa12 is used", 0.0, 10.0)]
        vd = [_vd("faa12 = compute()", 0.0, 10.0)]

        report = aligner.align(stt, vd)

        assert len(report.segments[0].conflicts) > 0
        assert "VD wins" in report.segments[0].conflicts[0]

    def test_no_conflict_on_exact_match(self) -> None:
        aligner = CrossModalAligner()
        stt = [_stt("variable total is used", 0.0, 10.0)]
        vd = [_vd("total = sum(items)", 0.0, 10.0)]

        report = aligner.align(stt, vd)

        assert len(report.segments[0].conflicts) == 0


class TestVerification:
    def test_coverage_gap_detected(self) -> None:
        aligner = CrossModalAligner(gap_threshold_sec=10.0)
        stt = [
            _stt("start", 0.0, 5.0, idx=0),
            _stt("end", 50.0, 55.0, idx=1),
        ]
        vd = [
            _vd("Scene A", 0.0, 5.0, scene_id=0),
            _vd("Scene B", 50.0, 55.0, scene_id=1),
        ]

        report = aligner.align(stt, vd)

        assert len(report.coverage_gaps) >= 1
        gap = report.coverage_gaps[0]
        assert gap.start_sec < 50.0
        assert gap.end_sec >= 50.0

    def test_vd_orphan(self) -> None:
        aligner = CrossModalAligner(window_sec=2.0)
        stt = [_stt("early talk", 0.0, 5.0)]
        vd = [
            _vd("Matched scene", 0.0, 5.0, scene_id=0),
            _vd("Orphan scene", 100.0, 110.0, scene_id=1),
        ]

        report = aligner.align(stt, vd)

        assert 1 in report.vd_orphans

    def test_stt_orphan(self) -> None:
        aligner = CrossModalAligner(window_sec=2.0, overlap_min_sec=1.0)
        stt = [
            _stt("matched", 0.0, 10.0, idx=0),
            _stt("orphan talk", 200.0, 210.0, idx=1),
        ]
        vd = [_vd("Scene", 0.0, 10.0, scene_id=0)]

        report = aligner.align(stt, vd)

        assert 1 in report.stt_orphans

    def test_semantic_coverage(self) -> None:
        aligner = CrossModalAligner()
        stt = [
            _stt("calculate_total function", 0.0, 10.0, idx=0),
            _stt("nothing relevant", 10.0, 20.0, idx=1),
        ]
        vd = [
            _vd("def calculate_total():", 0.0, 10.0, scene_id=0),
            _vd("UI toolbar", 10.0, 20.0, scene_id=1),
        ]

        report = aligner.align(stt, vd)

        # First segment has overlap, second doesn't
        assert 0.0 < report.semantic_coverage < 1.0

    def test_empty_input(self) -> None:
        aligner = CrossModalAligner()
        report = aligner.align([], [])

        assert len(report.segments) == 0
        assert len(report.coverage_gaps) == 0
        assert report.semantic_coverage == 0.0
