"""Where in a course material a course link points (mentor-rebuild task 10, K4).

Purpose:
    A course link sends the student to the place in the course where a topic
    is explained. The place is known as a segment's anchor
    (:class:`~course_supporter.storage.document_segment_repository.SegmentAnchor`):
    seconds, a slide number, a paragraph ordinal or a file path. The review
    says it in the material's own terms (:class:`Position`), which the
    assembler then puts into the phrase of its kind.

Interface:
    :func:`position_of` — anchor and source type to a position, or ``None``
    when the anchor holds no place to name.

Pure: no database, no clock. The source type decides the kind, as it does for
the anchor itself, so a recording is never called a video.

>>> from course_supporter.storage.document_segment_repository import SegmentAnchor
>>> position_of(SegmentAnchor("time", 221.7, 260.0), "audio")
Position(kind='audio', value='03:41')
>>> position_of(SegmentAnchor("paragraph", None, None), "text") is None
True
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from course_supporter.models.review_structure import Position

if TYPE_CHECKING:
    from course_supporter.storage.document_segment_repository import SegmentAnchor

__all__ = ["position_of"]

_SECONDS_IN_HOUR = 3600


def _clock(seconds: float) -> str:
    """Seconds as a reader looks for them in a player: ``mm:ss``, ``h:mm:ss``.

    Rounded down: the place a fragment starts is never shown later than it is,
    so the student does not land past its first words.
    """
    whole = int(seconds)
    hours, rest = divmod(whole, _SECONDS_IN_HOUR)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def position_of(anchor: SegmentAnchor, source_type: str) -> Position | None:
    """The place a segment starts at, in its material's terms.

    * video, audio — the start time, ``mm:ss`` or from an hour ``h:mm:ss``;
    * presentation — the first slide;
    * text, web — the first paragraph;
    * code — the file's path.

    Args:
        anchor: The segment's anchor, as the segment repository builds it.
        source_type: The material's source type; it decides the kind.

    Returns:
        The position, or ``None`` when the anchor has no value to name — a
        text segment whose paragraph was never counted, for one. A link
        without a position still names its material.

    Raises:
        ValueError: a source type with no kind of place, or an anchor of
            another kind than the source type makes — a mismatch is a bug
            upstream, not a place to guess.
    """
    if source_type in ("video", "audio"):
        expected, kind = "time", source_type
    elif source_type == "presentation":
        expected, kind = "slide", "slide"
    elif source_type in ("text", "web"):
        expected, kind = "paragraph", "paragraph"
    elif source_type == "code":
        expected, kind = "file", "file"
    else:
        msg = f"No kind of place for source_type {source_type!r}"
        raise ValueError(msg)

    if anchor.kind != expected:
        msg = (
            f"An anchor of kind {anchor.kind!r} does not belong to a "
            f"{source_type!r} material (expected {expected!r})"
        )
        raise ValueError(msg)

    if kind == "file":
        return (
            Position(kind="file", value=anchor.file_path) if anchor.file_path else None
        )
    if anchor.start is None:
        return None
    if kind in ("video", "audio"):
        return Position(kind=kind, value=_clock(anchor.start))
    return Position(kind=kind, value=str(int(anchor.start)))
