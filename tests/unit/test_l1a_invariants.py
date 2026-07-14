"""L1a acceptance invariants — permanent locks (job-lifecycle contract).

These are footgun-guards, not behaviour tests (mirror of №17/№18/№19): each
one fails loudly the moment a future change reintroduces the class of defect
L1a closed — a raw ``Job.status`` writer, a status-partition copy that drifts
from the machine, or a legacy ``job_type`` / dead-status string literal.

Detectors carry a POSITIVE CONTROL where the detector could silently rot into
a no-op: the literal grep-invariant ``update(Job).values(status=`` would have
returned zero forever (no writer uses that form), so a green detector proves
nothing unless it is also shown to FIRE on a synthetic positive.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from course_supporter.storage.job_repository import (
    _STATUS_CATEGORY,
    AT_REST_STATUSES,
    IN_FLIGHT_STATUSES,
    JOB_TRANSITIONS,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "course_supporter"
_TESTS = _REPO_ROOT / "tests"

# This detector file necessarily mentions the forbidden tokens (patterns +
# positive-control fixtures), so it must never scan itself.
_SELF = Path(__file__).name


def _py_files(*roots: Path) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        files.extend(
            p
            for p in root.rglob("*.py")
            if "__pycache__" not in p.parts and p.name != _SELF
        )
    return files


# ── Ownership lock (Acceptance #2) ────────────────────────────────────


class _StatusWriteFinder(ast.NodeVisitor):
    """Locate raw writes to ``Job.status`` in two forms, robust to var name.

    * ORM attribute-assignment ``<anything>.status = ...`` — on ANY variable
      name (the three original writers happened to be ``job``; ``j.status`` /
      ``row.status`` must not slip past). Never legal → flagged everywhere.
    * SQLAlchemy Core ``update(Job).values(...)`` that CAN write ``status``:
      an explicit ``status=`` kwarg, a ``"status"`` key in a dict literal, OR
      a ``**`` unpack (which can carry ``status`` — and is exactly the shape
      the owner uses). Keyed on ``update(Job)`` so ``update(HomeworkSubmission)``
      (its own machine) is excluded.

    The Core form is exempt ONLY inside ``JobRepository.update_status`` — the
    exemption is by SCOPE (a structural AST condition), NOT by form. Exempting
    the ``**values`` shape itself would leave the bypass open: copy the owner's
    ``update(Job).values(**d)`` into any other method and a form-based lock
    stays silent. The dead ``running`` token lived that way for years.
    """

    _OWNER_CLASS = "JobRepository"
    _OWNER_METHOD = "update_status"

    def __init__(self) -> None:
        self.attr_assigns: list[int] = []
        self.core_status: list[int] = []
        self._class_stack: list[str] = []
        self._func_stack: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()

    def _inside_owner(self) -> bool:
        return (
            bool(self._class_stack)
            and self._class_stack[-1] == self._OWNER_CLASS
            and bool(self._func_stack)
            and self._func_stack[-1] == self._OWNER_METHOD
        )

    def _record_attr_target(self, target: ast.expr, lineno: int) -> None:
        if isinstance(target, ast.Attribute) and target.attr == "status":
            self.attr_assigns.append(lineno)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record_attr_target(target, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._record_attr_target(node.target, node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_attr_target(node.target, node.lineno)
        self.generic_visit(node)

    @staticmethod
    def _values_can_write_status(node: ast.Call) -> bool:
        """A ``.values(...)`` call that could set ``status``: explicit kwarg,
        a ``"status"`` dict-literal key, or a ``**`` unpack (opaque → assume it
        can)."""
        if any(kw.arg is None or kw.arg == "status" for kw in node.keywords):
            return True
        return any(
            isinstance(arg, ast.Dict)
            and any(
                isinstance(k, ast.Constant) and k.value == "status"
                for k in arg.keys
                if k is not None
            )
            for arg in node.args
        )

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "values"
            and _chain_targets_update_job(node.func.value)
            and self._values_can_write_status(node)
            and not self._inside_owner()
        ):
            self.core_status.append(node.lineno)
        self.generic_visit(node)


def _chain_targets_update_job(expr: ast.expr) -> bool:
    """True if ``expr`` is a method chain rooted at ``update(Job)``."""
    current: ast.expr | None = expr
    while current is not None:
        if isinstance(current, ast.Call):
            func = current.func
            if isinstance(func, ast.Name) and func.id == "update":
                first = current.args[0] if current.args else None
                return isinstance(first, ast.Name) and first.id == "Job"
            # Walk down the receiver of a chained call: update(Job).where(...)
            current = func.value if isinstance(func, ast.Attribute) else None
        elif isinstance(current, ast.Attribute):
            current = current.value
        else:
            return False
    return False


def _find_status_writes(source: str) -> _StatusWriteFinder:
    finder = _StatusWriteFinder()
    finder.visit(ast.parse(source))
    return finder


def test_ownership_lock_positive_control() -> None:
    """The detector FIRES on a synthetic positive of EACH write form, and the
    exemption is by SCOPE not FORM.

    Without this, a green src scan proves nothing — a detector that never
    fires is indistinguishable from one that always passes.
    """
    # attr-assign fires on ANY variable name.
    attr_sample = "job.status = 'x'\nrow.status = 'y'\nvictim.status = 'z'\n"
    found_attr = _find_status_writes(attr_sample)
    assert len(found_attr.attr_assigns) == 3, "attr-assign detector missed a var name"
    assert not found_attr.core_status

    # All three Core status-write shapes fire OUTSIDE the owner.
    assert _find_status_writes(
        "stmt = update(Job).where(Job.id == j).values(status='cancelled')\n"
    ).core_status, "status= kwarg not caught"
    assert _find_status_writes(
        "stmt = update(Job).values({'status': 'failed', 'completed_at': n})\n"
    ).core_status, "status dict-literal not caught"
    assert _find_status_writes("stmt = update(Job).values(**payload)\n").core_status, (
        "**unpack not caught outside the owner"
    )

    # SCOPE exemption: the SAME **unpack is legal ONLY inside the owner method.
    owner = (
        "class JobRepository:\n"
        "    async def update_status(self, jid, status):\n"
        "        await s.execute(update(Job).where(Job.id == jid).values(**values))\n"
    )
    assert not _find_status_writes(owner).core_status, (
        "owner's **values wrongly flagged"
    )

    # The same shape in a DIFFERENT JobRepository method still fires — the
    # exemption is method-specific, so copying the owner's form does not evade.
    non_owner = (
        "class JobRepository:\n"
        "    async def sneaky(self, jid):\n"
        "        await s.execute(update(Job).values(**values))\n"
    )
    assert _find_status_writes(non_owner).core_status, "bypass via copied **values"

    # Negatives: a different model, and a non-status explicit kwarg → no fire.
    assert not _find_status_writes(
        "stmt = update(HomeworkSubmission).values(status='completed')\n"
    ).core_status
    assert not _find_status_writes(
        "stmt = update(Job).values(arq_job_id='x')\n"
    ).core_status


def test_job_status_has_zero_raw_writers_in_src() -> None:
    """No raw write to ``Job.status`` anywhere in src — the owner is the only
    writer (contract §3). Threshold is ZERO, not "exactly 3": the first new
    raw writer trips the lock instead of degrading silently."""
    offenders: list[str] = []
    for path in _py_files(_SRC):
        finder = _find_status_writes(path.read_text())
        rel = path.relative_to(_REPO_ROOT)
        offenders += [f"{rel}:{ln} (.status = ...)" for ln in finder.attr_assigns]
        offenders += [
            f"{rel}:{ln} (update(Job).values status)" for ln in finder.core_status
        ]
    assert not offenders, (
        "Raw Job.status writes must go through JobRepository.update_status:\n"
        + "\n".join(offenders)
    )


# ── Status-partition derivation (Acceptance #3) ───────────────────────


def test_status_category_is_total_over_the_machine() -> None:
    """Every JOB_TRANSITIONS status has a category, and vice versa — the
    footgun-guard on totality. A status added to the machine without a
    category (the way ``running`` was forgotten) fails HERE, loudly."""
    assert set(_STATUS_CATEGORY) == set(JOB_TRANSITIONS)


def test_partitions_are_derived_not_duplicated() -> None:
    """The in-flight / at-rest sets are the partition of the machine's
    statuses induced by _STATUS_CATEGORY (single source), and they partition
    it exactly — no overlap, no gap. ``failed`` is at-rest (retry gives it an
    outgoing edge, but no worker holds it — naive edge-derivation is wrong)."""
    assert IN_FLIGHT_STATUSES.isdisjoint(AT_REST_STATUSES)
    assert set(JOB_TRANSITIONS) == IN_FLIGHT_STATUSES | AT_REST_STATUSES
    assert {"queued", "active"} == IN_FLIGHT_STATUSES
    assert "failed" in AT_REST_STATUSES


# ── Legacy-token grep detectors (Acceptance #5, class-sweep G) ─────────


def _lines_matching(needle: str, token_pat: str) -> list[str]:
    """src+tests lines that mention ``needle`` (case-insensitive) and carry a
    quoted ``token``.

    Case-insensitive on ``needle`` so a scoping word like ``status`` also
    catches constant names (``_IN_PROGRESS_STATUSES``) while a prose/log line
    that mentions neither is left alone.
    """
    import re

    needle_re = re.compile(re.escape(needle), re.IGNORECASE)
    quoted = re.compile(rf"""['"]{token_pat}['"]""")
    hits: list[str] = []
    for path in _py_files(_SRC, _TESTS):
        rel = path.relative_to(_REPO_ROOT)
        for i, line in enumerate(path.read_text().splitlines(), start=1):
            if needle_re.search(line) and quoted.search(line):
                hits.append(f"{rel}:{i}: {line.strip()}")
    return hits


def test_no_legacy_job_type_literals() -> None:
    """Zero legacy ``job_type`` string literals in src/ or tests/. Scoped to
    lines mentioning ``job_type`` so the security upload-context and ARQ
    queue namespaces (``context="homework"`` / ``queue_name="homework"``) —
    a different vocabulary — are correctly left alone."""
    offenders = _lines_matching("job_type", "ingest|homework")
    assert not offenders, (
        "Legacy job_type literals (migrate to canonical):\n" + "\n".join(offenders)
    )


def test_no_running_status_literal() -> None:
    """Zero ``"running"`` status literals in src/ or tests/ — SCOPED to
    status-context lines (mirror of the job_type detector), so prose/log
    strings like ``logger.info("worker running")`` are not false positives.
    ``running`` is a dead token in BOTH status vocabularies (Job and
    HomeworkSubmission never held it); ``completed`` is NOT (a live
    HomeworkSubmission milestone) and is covered by the DB CHECK instead.

    Residual edge (accepted, POST-MR-NOTES): a line that mentions ``status``
    AND quotes ``running`` in prose (e.g. a log message) would still trip —
    rare, and arguably a smell worth surfacing."""
    offenders = _lines_matching("status", "running")
    assert not offenders, "Dead 'running' status literal:\n" + "\n".join(offenders)


@pytest.mark.parametrize("status", sorted(JOB_TRANSITIONS))
def test_machine_statuses_are_canonical(status: str) -> None:
    """The machine's keys are exactly the five canonical statuses — no
    ``running`` / ``completed`` leaked back into JOB_TRANSITIONS."""
    assert status in {"queued", "active", "complete", "failed", "cancelled"}
