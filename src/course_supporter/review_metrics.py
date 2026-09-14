"""Review metrics: authenticity and completeness (mentor-rebuild task 01).

Purpose:
    Two numbers that say how trustworthy one review is, stored in the call
    register next to cost, tokens and latency:

    * **authenticity** — the share of the review's claims supported by a
      reference (a resolved pointer into the course);
    * **completeness** — the share of claims covered by a verdict (every
      applicable criterion judged, every remark tied to a criterion).

Interface:
    Input: a sequence of :class:`Claim`, each carrying exactly two flags —
    ``supported_by_reference`` and ``covered_by_verdict``. That is the whole
    input, on purpose: the calculator knows nothing of the review structure,
    the stage, the path or the provider. Turning a real review into claims
    (and resolving references, and judging applicability) is the assembler's
    job — task 04 — so the review's shape can change without touching this.

    Output: :class:`ReviewMetrics` — both shares in ``[0, 1]``, or both
    ``None`` for an empty claim list. ``None`` is not zero: zero would state
    "a review with no supported reference", which is a different, and false,
    claim about a review that made no claims at all.

Replacing the implementation:
    Anything with a ``calculate(claims) -> ReviewMetrics`` method satisfies
    :class:`ReviewMetricsCalculator` (a structural ``Protocol``; no base class
    to inherit). Pass the replacement wherever the default
    :class:`ShareMetricsCalculator` is passed. The register write lives
    elsewhere — :func:`course_supporter.service_logging.record_review_metrics`
    takes the finished :class:`ReviewMetrics`, not a calculator — so a new
    formula never touches where or how the numbers are stored.

>>> calculator = ShareMetricsCalculator()
>>> calculator.calculate(
...     [Claim(supported_by_reference=True, covered_by_verdict=True),
...      Claim(supported_by_reference=False, covered_by_verdict=True)]
... )
ReviewMetrics(authenticity=0.5, completeness=1.0)
>>> calculator.calculate([])
ReviewMetrics(authenticity=None, completeness=None)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

REVIEW_METRICS_ACTION: Final = "review_metrics"
"""``ExternalServiceCall.action`` of the per-review metrics row."""


@dataclass(frozen=True, slots=True)
class Claim:
    """One claim a review makes, reduced to the two facts the metrics read.

    Attributes:
        supported_by_reference: The claim cites a reference that resolved.
        covered_by_verdict: The claim is tied to a criterion that received a
            verdict.
    """

    supported_by_reference: bool
    covered_by_verdict: bool


@dataclass(frozen=True, slots=True)
class ReviewMetrics:
    """Per-review metrics; both ``None`` when the review made no claims."""

    authenticity: float | None
    completeness: float | None


class ReviewMetricsCalculator(Protocol):
    """The replaceable port: claims in, metrics out."""

    def calculate(self, claims: Sequence[Claim]) -> ReviewMetrics:
        """Compute the metrics of one review from its claims."""
        ...


class ShareMetricsCalculator:
    """Default implementation: plain shares of flagged claims."""

    def calculate(self, claims: Sequence[Claim]) -> ReviewMetrics:
        """Shares of supported and of covered claims; ``None`` when empty."""
        if not claims:
            return ReviewMetrics(authenticity=None, completeness=None)
        total = len(claims)
        return ReviewMetrics(
            authenticity=sum(c.supported_by_reference for c in claims) / total,
            completeness=sum(c.covered_by_verdict for c in claims) / total,
        )
