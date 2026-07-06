"""Port protocols for the normalizer's replaceable strategies (KD18 P1).

Port-adapter architecture: the classifier and the text extractor are
swappable strategies so the deterministic core stays domain-neutral.
Concrete adapters land in later commits.
"""

from __future__ import annotations

from typing import Protocol

from course_supporter.normalizer.models import EntryClass


class EntryClassifier(Protocol):
    """Assigns an :class:`EntryClass` to an archive entry."""

    def classify(self, path: str, head: bytes) -> EntryClass:
        """Classify ``path`` (canonical) using ``head`` bytes if needed."""
        ...


class TextExtractor(Protocol):
    """Extracts review-facing text from an entry, at diff-time.

    One extractor serves both sides (base and submission) so version
    drift in docx/pdf tooling cannot desynchronize the two.
    """

    def extract(self, cls: EntryClass, raw: bytes) -> str | None:
        """Return extracted text, or ``None`` when not extractable."""
        ...
