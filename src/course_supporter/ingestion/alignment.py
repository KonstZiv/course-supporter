"""Cross-modal alignment: merge STT and VD chunks by temporal overlap.

Takes a SourceDocument containing TRANSCRIPT (STT) and VISUAL_SCENE (VD)
chunks and produces an AlignmentReport with temporally aligned segments,
coverage gaps, orphans, and conflict flags.

No LLM calls — purely algorithmic (temporal overlap + keyword matching).
"""

from __future__ import annotations

import re

import structlog

from course_supporter.models.source import ContentChunk
from course_supporter.vd.schemas import (
    AlignedSegment,
    AlignmentReport,
    CoverageGap,
)

logger = structlog.get_logger()

_WINDOW_SEC = 10.0
_OVERLAP_MIN_SEC = 2.0
_GAP_THRESHOLD_SEC = 30.0

# Pattern to extract code-like identifiers (function names, variables, etc.)
_IDENTIFIER_PATTERN = re.compile(r"\b[a-zA-Z_]\w*(?:\.\w+)*\b")

# Common words to exclude from identifier matching
_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "shall",
        "can",
        "must",
        "not",
        "no",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "for",
        "in",
        "on",
        "at",
        "to",
        "from",
        "with",
        "by",
        "of",
        "that",
        "this",
        "it",
        "its",
        "we",
        "you",
        "he",
        "she",
        "they",
        "them",
        "our",
        "your",
        "my",
        "his",
        "her",
        "their",
        "what",
        "which",
        "who",
        "how",
        "when",
        "where",
        "why",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "than",
        "too",
        "very",
        "just",
        "about",
        "above",
        "after",
        "before",
        "between",
        "into",
        "through",
        "during",
        "without",
        "again",
        "further",
        "once",
        "here",
        "there",
        "so",
        "up",
        "out",
        "only",
        "also",
        "now",
        "new",
        "one",
        "two",
        "three",
        "first",
        "true",
        "false",
        "none",
        "null",
        "return",
        "import",
        "class",
        "def",
        "self",
        "print",
        "type",
        "list",
        "dict",
        "str",
        "int",
        "float",
        "bool",
        "function",
        "method",
        "variable",
        "value",
        "used",
        "called",
        "using",
    }
)


def _extract_identifiers(text: str) -> set[str]:
    """Extract meaningful identifiers from text."""
    raw = _IDENTIFIER_PATTERN.findall(text)
    return {w.lower() for w in raw if len(w) > 2 and w.lower() not in _STOP_WORDS}


def _temporal_overlap(
    a_start: float,
    a_end: float,
    b_start: float,
    b_end: float,
) -> float:
    """Calculate temporal overlap in seconds between two intervals."""
    overlap_start = max(a_start, b_start)
    overlap_end = min(a_end, b_end)
    return max(0.0, overlap_end - overlap_start)


class CrossModalAligner:
    """Align STT transcript chunks with VD visual scene chunks.

    Operates on ContentChunks from a single SourceDocument.
    All chunks must have start_sec/end_sec set.

    Args:
        window_sec: Temporal tolerance for matching (default 10s).
        overlap_min_sec: Minimum overlap to consider a match (default 2s).
        gap_threshold_sec: Gaps larger than this are flagged (default 30s).
    """

    def __init__(
        self,
        *,
        window_sec: float = _WINDOW_SEC,
        overlap_min_sec: float = _OVERLAP_MIN_SEC,
        gap_threshold_sec: float = _GAP_THRESHOLD_SEC,
    ) -> None:
        self._window = window_sec
        self._min_overlap = overlap_min_sec
        self._gap_threshold = gap_threshold_sec

    def align(
        self,
        stt_chunks: list[ContentChunk],
        vd_chunks: list[ContentChunk],
    ) -> AlignmentReport:
        """Run full alignment pipeline.

        1. Temporal matching (overlap-based).
        2. Semantic cross-reference (identifier matching).
        3. Conflict detection.
        4. Verification (gaps, orphans, coverage).
        """
        segments = self._temporal_match(stt_chunks, vd_chunks)
        self._semantic_link(segments, stt_chunks, vd_chunks)
        self._detect_conflicts(segments, stt_chunks, vd_chunks)

        # Verification
        gaps = self._find_gaps(segments, stt_chunks, vd_chunks)
        vd_orphans = self._find_vd_orphans(segments, vd_chunks)
        stt_orphans = self._find_stt_orphans(segments, stt_chunks)
        coverage = self._semantic_coverage(segments)

        logger.info(
            "alignment_done",
            segments=len(segments),
            gaps=len(gaps),
            vd_orphans=len(vd_orphans),
            stt_orphans=len(stt_orphans),
            semantic_coverage=round(coverage, 2),
        )

        return AlignmentReport(
            segments=segments,
            coverage_gaps=gaps,
            vd_orphans=vd_orphans,
            stt_orphans=stt_orphans,
            semantic_coverage=coverage,
        )

    def _temporal_match(
        self,
        stt_chunks: list[ContentChunk],
        vd_chunks: list[ContentChunk],
    ) -> list[AlignedSegment]:
        """Phase 1: Match STT and VD chunks by temporal overlap.

        Pre-sorts STT by start_sec for efficient scanning.
        """
        # Pre-sort STT chunks by start time
        sorted_stt = sorted(
            (
                s
                for s in stt_chunks
                if s.start_sec is not None and s.end_sec is not None
            ),
            key=lambda s: s.start_sec or 0.0,
        )

        segments: list[AlignedSegment] = []

        for vd in vd_chunks:
            if vd.start_sec is None or vd.end_sec is None:
                continue

            vd_start = vd.start_sec - self._window
            vd_end = vd.end_sec + self._window

            # Scan sorted STT — skip those that end before our window
            matching_stt: list[ContentChunk] = []
            for stt in sorted_stt:
                assert stt.start_sec is not None
                assert stt.end_sec is not None
                if stt.end_sec < vd_start:
                    continue
                if stt.start_sec > vd_end:
                    break
                overlap = _temporal_overlap(
                    vd_start,
                    vd_end,
                    stt.start_sec,
                    stt.end_sec,
                )
                if overlap >= self._min_overlap:
                    matching_stt.append(stt)

            stt_text = " ".join(s.text for s in matching_stt) if matching_stt else None

            segments.append(
                AlignedSegment(
                    start_sec=vd.start_sec,
                    end_sec=vd.end_sec,
                    stt_text=stt_text,
                    vd_scene_id=vd.metadata.get("scene_id"),
                    vd_summary=vd.text,
                ),
            )

        return segments

    @staticmethod
    def _semantic_link(
        segments: list[AlignedSegment],
        stt_chunks: list[ContentChunk],
        vd_chunks: list[ContentChunk],
    ) -> None:
        """Phase 2: Score semantic overlap via identifier matching."""
        for seg in segments:
            if not seg.stt_text or not seg.vd_summary:
                continue

            stt_ids = _extract_identifiers(seg.stt_text)
            vd_ids = _extract_identifiers(seg.vd_summary)

            if not stt_ids or not vd_ids:
                continue

            common = stt_ids & vd_ids
            union = stt_ids | vd_ids
            seg.semantic_overlap = len(common) / len(union) if union else 0.0
            seg.alignment_confidence = min(1.0, seg.semantic_overlap * 2)

    @staticmethod
    def _detect_conflicts(
        segments: list[AlignedSegment],
        stt_chunks: list[ContentChunk],
        vd_chunks: list[ContentChunk],
    ) -> None:
        """Phase 3: Flag potential conflicts between STT and VD."""
        for seg in segments:
            if not seg.stt_text or not seg.vd_summary:
                continue

            stt_ids = _extract_identifiers(seg.stt_text)
            vd_ids = _extract_identifiers(seg.vd_summary)

            # Identifiers in VD but not in STT could be misheard
            vd_only = vd_ids - stt_ids
            stt_only = stt_ids - vd_ids

            # Look for near-matches (potential mishearing)
            for v_id in vd_only:
                for s_id in stt_only:
                    if _is_near_match(v_id, s_id):
                        seg.conflicts.append(
                            f"Possible mishearing: STT '{s_id}' vs VD '{v_id}' "
                            f"(VD wins for code)"
                        )

    def _find_gaps(
        self,
        segments: list[AlignedSegment],
        stt_chunks: list[ContentChunk],
        vd_chunks: list[ContentChunk],
    ) -> list[CoverageGap]:
        """Phase 4a: Find time intervals without any coverage."""
        if not segments and not stt_chunks and not vd_chunks:
            return []

        # Collect all covered intervals
        all_intervals: list[tuple[float, float]] = []
        for s in segments:
            all_intervals.append((s.start_sec, s.end_sec))
        for c in stt_chunks:
            if c.start_sec is not None and c.end_sec is not None:
                all_intervals.append((c.start_sec, c.end_sec))

        if not all_intervals:
            return []

        all_intervals.sort()

        # Find gaps between covered intervals
        gaps: list[CoverageGap] = []
        covered_until = 0.0
        for start, end in all_intervals:
            if start > covered_until + self._gap_threshold:
                gaps.append(
                    CoverageGap(
                        start_sec=covered_until,
                        end_sec=start,
                        gap_type="neither",
                    ),
                )
            covered_until = max(covered_until, end)

        return gaps

    @staticmethod
    def _find_vd_orphans(
        segments: list[AlignedSegment],
        vd_chunks: list[ContentChunk],
    ) -> list[int]:
        """Phase 4b: VD scenes with no STT match."""
        matched_scene_ids = {s.vd_scene_id for s in segments if s.stt_text is not None}
        orphans: list[int] = []
        for vd in vd_chunks:
            scene_id = vd.metadata.get("scene_id")
            if scene_id is not None and scene_id not in matched_scene_ids:
                orphans.append(scene_id)
        return orphans

    def _find_stt_orphans(
        self,
        segments: list[AlignedSegment],
        stt_chunks: list[ContentChunk],
    ) -> list[int]:
        """Phase 4c: STT chunks with no VD match."""
        covered: list[tuple[float, float]] = [
            (s.start_sec, s.end_sec) for s in segments
        ]

        orphans: list[int] = []
        for idx, stt in enumerate(stt_chunks):
            if stt.start_sec is None or stt.end_sec is None:
                continue
            matched = any(
                _temporal_overlap(stt.start_sec, stt.end_sec, c_start, c_end)
                >= self._min_overlap
                for c_start, c_end in covered
            )
            if not matched:
                orphans.append(idx)

        return orphans

    @staticmethod
    def _semantic_coverage(segments: list[AlignedSegment]) -> float:
        """Fraction of segments with semantic overlap > 0."""
        if not segments:
            return 0.0
        linked = sum(1 for s in segments if s.semantic_overlap > 0)
        return linked / len(segments)


def _is_near_match(a: str, b: str) -> bool:
    """Check if two identifiers are similar (edit distance 1-2).

    Uses simple Levenshtein-like row-based DP, bounded to distance 2.
    """
    if a == b:
        return False
    if abs(len(a) - len(b)) > 2:
        return False
    if len(a) < 3 or len(b) < 3:
        return False

    # Bounded Levenshtein distance
    n, m = len(a), len(b)
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        curr = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(curr[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = curr
    dist = prev[m]
    return 1 <= dist <= 2
