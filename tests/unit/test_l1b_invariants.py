"""L1b invariant locks — typed job subject + idempotency index.

Detectors with POSITIVE CONTROLS (the L1a discipline / №17-№18-№19 lesson: a
test that never fires is indistinguishable from a test that catches nothing).

* Totality: every ``JobType`` has a ``JOB_SUBJECT_TYPE`` entry.
* Derive-or-verify: the frozen status list in the ``uq_jobs_subject_in_flight``
  WHERE equals ``IN_FLIGHT_STATUSES`` (contract ratified point R4).
* Pairs: ``JOB_SUBJECT_TYPE_PAIRS`` equals the ``ck_jobs_subject_type_legal``
  whitelist frozen in the ORM (and, transitively, the migration).
* No JSONB identity addressing remains in ``src/`` (contract acceptance 9).
* No ``course_node_id`` augmentation remains in the three former hooks
  (contract acceptance 10).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import course_supporter
from course_supporter.jobs import (
    JOB_SUBJECT_TYPE,
    JOB_SUBJECT_TYPE_PAIRS,
    JobType,
)
from course_supporter.storage.job_repository import IN_FLIGHT_STATUSES
from course_supporter.storage.orm import Job

_SRC = Path(course_supporter.__file__).parent
_VERSIONS = Path(course_supporter.__file__).parents[2] / "migrations" / "versions"
_MIGRATION = _VERSIONS / "l1b_job_subject.py"
# №21 BE1+BE3 widened ck_jobs_subject_type_legal (drop + re-add with the
# document_preparation pair), so the prep migration — not l1b — is now the
# authority for the legal-pair set. l1b stays the authority for the
# uq_jobs_subject_in_flight index it also froze (untouched by №21).
_PREP_MIGRATION = _VERSIONS / "n21_prep_jobtype.py"


def _load_module(path: Path, name: str) -> ModuleType:
    """Load a migration module by path (migrations are not an importable package)."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_migration() -> ModuleType:
    """Load the l1b migration (authority for the in-flight index)."""
    return _load_module(_MIGRATION, "_l1b_migration")


def _load_prep_migration() -> ModuleType:
    """Load the №21 prep migration (current authority for the legal-pair CHECK)."""
    return _load_module(_PREP_MIGRATION, "_n21_prep_migration")


# ── helpers ──────────────────────────────────────────────────────────────


def _legal_pairs_from_check(sqltext: str) -> set[tuple[str, str]]:
    """Extract ``(job_type, subject_type)`` pairs from a legal-pair CHECK SQL."""
    return set(
        re.findall(
            r"job_type = '([^']+)' AND subject_type = '([^']+)'",
            sqltext,
        )
    )


def _statuses_from_where(where_sql: str) -> set[str]:
    """Extract the quoted status literals from an index WHERE predicate."""
    # Only the status IN (...) arm carries quoted lower-case tokens; the
    # ``deleted_at IS NULL`` arm has none.
    return set(re.findall(r"'([a-z]+)'", where_sql))


def _check_sqltext(name: str) -> str:
    for c in Job.__table__.constraints:
        if c.name == name:
            return str(c.sqltext)  # type: ignore[attr-defined]
    raise AssertionError(f"CheckConstraint {name!r} not found on jobs table")


def _index_where(name: str) -> str:
    for idx in Job.__table__.indexes:
        if idx.name == name:
            where = idx.dialect_options["postgresql"]["where"]
            return str(where)
    raise AssertionError(f"Index {name!r} not found on jobs table")


# ── totality (acceptance 8) ──────────────────────────────────────────────


def test_job_subject_type_totality() -> None:
    """Every JobType member has an explicit JOB_SUBJECT_TYPE entry.

    Mirrors the import-time guard in ``job_type.py`` — a new type without a
    subject_type (or an explicit None) cannot slip through silently.
    """
    assert set(JOB_SUBJECT_TYPE) == set(JobType)


def test_s3_cleanup_is_the_only_subjectless_type() -> None:
    """Only s3_cleanup legitimately has NULL subject_type (ratified point R2)."""
    subjectless = {jt for jt, st in JOB_SUBJECT_TYPE.items() if st is None}
    assert subjectless == {JobType.S3_CLEANUP}


# ── pairs (acceptance 8) ─────────────────────────────────────────────────


def test_subject_type_pairs_match_orm_check() -> None:
    """JOB_SUBJECT_TYPE_PAIRS == the pairs frozen in ck_jobs_subject_type_legal.

    Derive-or-verify: the code dictionary and the DB CHECK whitelist are two
    representations of the same fact; a drift between them fails here.
    """
    check_pairs = _legal_pairs_from_check(_check_sqltext("ck_jobs_subject_type_legal"))
    assert check_pairs == set(JOB_SUBJECT_TYPE_PAIRS)


def test_pair_parser_positive_control() -> None:
    """The pair parser actually extracts pairs (else the lock above is blind)."""
    synthetic = (
        "(job_type = 'x_type' AND subject_type = 'x_subj') "
        "OR (job_type = 'y_type' AND subject_type = 'y_subj') "
        "OR subject_type IS NULL"
    )
    assert _legal_pairs_from_check(synthetic) == {
        ("x_type", "x_subj"),
        ("y_type", "y_subj"),
    }


# ── derive-or-verify index statuses (acceptance 7) ───────────────────────


def test_index_where_statuses_match_in_flight() -> None:
    """The frozen status list in uq_jobs_subject_in_flight == IN_FLIGHT_STATUSES.

    The day a sixth in-flight status is added without amending the index — a
    failure, not silence (contract R4).
    """
    statuses = _statuses_from_where(_index_where("uq_jobs_subject_in_flight"))
    assert statuses == set(IN_FLIGHT_STATUSES)


def test_index_where_parser_positive_control() -> None:
    """The WHERE status parser detects a divergent set (else the lock is blind).

    Uses a synthetic non-canonical token (``paused``) rather than a real dead
    status so the L1a global ``test_no_running_status_literal`` lock stays true.
    """
    divergent = "status IN ('queued', 'active', 'paused') AND deleted_at IS NULL"
    assert _statuses_from_where(divergent) == {"queued", "active", "paused"}
    assert _statuses_from_where(divergent) != set(IN_FLIGHT_STATUSES)


# ── 3-point cross-doc: migration frozen SQL (invariant 7 / R4) ───────────
#
# The ORM-side locks above cover ORM↔code. These cover the migration↔code
# and migration↔ORM legs so all three points are guarded — Alembic does not
# round-trip a partial WHERE / CHECK predicate (the GIN 3.3b lesson the
# migration docstring cites), so drift between the migration's frozen SQL and
# the ORM/code would otherwise pass silently.


def test_migration_index_where_matches_in_flight() -> None:
    """The migration's frozen index WHERE statuses == IN_FLIGHT_STATUSES."""
    mig = _load_migration()
    assert _statuses_from_where(mig._INFLIGHT_WHERE) == set(IN_FLIGHT_STATUSES)


def test_migration_pairs_match_code() -> None:
    """The prep migration's frozen legal-pair CHECK == JOB_SUBJECT_TYPE_PAIRS.

    №21 widened ck_jobs_subject_type_legal, so the authority is now the prep
    migration's ``_SUBJECT_TYPE_CONDITION_NEW`` (the SQL it re-adds), not l1b's
    superseded ``_TYPE_CONDITION``.
    """
    mig = _load_prep_migration()
    assert _legal_pairs_from_check(mig._SUBJECT_TYPE_CONDITION_NEW) == set(
        JOB_SUBJECT_TYPE_PAIRS
    )


def test_migration_and_orm_predicates_agree() -> None:
    """Migration frozen SQL and ORM declaration agree on the index WHERE and
    the legal-pair CHECK — keep the two representations identical or model↔DB
    drifts (GIN 3.3b). Each leg reads its current authority: l1b for the
    in-flight index, the №21 prep migration for the widened legal-pair CHECK."""
    l1b_mig = _load_migration()
    prep_mig = _load_prep_migration()
    assert _statuses_from_where(l1b_mig._INFLIGHT_WHERE) == _statuses_from_where(
        _index_where("uq_jobs_subject_in_flight")
    )
    assert _legal_pairs_from_check(
        prep_mig._SUBJECT_TYPE_CONDITION_NEW
    ) == _legal_pairs_from_check(_check_sqltext("ck_jobs_subject_type_legal"))


# ── no JSONB identity addressing (acceptance 9) ──────────────────────────
#
# Catch every CLASS-form addressing of the JSONB column: containment
# (``.contains(`` / ``.op(``) AND subscript (``Job.input_params[...]`` — e.g.
# ``.astext == x`` query expressions). Anchored on capital ``Job`` so
# INSTANCE-side payload reads (``p = job.input_params or {}``; ``p["source_type"]``)
# — which are legal task-parameter access, not identity addressing — do NOT match.
_JSONB_CONTAINMENT = re.compile(r"\bJob\.input_params(\.(contains|op)\(|\[)")


def test_no_jsonb_identity_addressing_in_src() -> None:
    """No runtime JSONB addressing of Job.input_params remains in queries.

    L1b moved identity to the typed subject columns; the ``@>`` / ``.contains``
    / column-subscript addressing is gone. Class-form ``.contains`` on a
    DIFFERENT JSONB column (e.g. ``Job.stage_progress``) is legal — the regex
    targets ``input_params`` only; instance-side dict access stays legal.
    """
    offenders: list[str] = []
    for py in _SRC.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if _JSONB_CONTAINMENT.search(line):
                offenders.append(f"{py.relative_to(_SRC)}:{i}: {line.strip()}")
    assert not offenders, "JSONB identity addressing survived L1b:\n" + "\n".join(
        offenders
    )


def test_jsonb_addressing_detector_positive_control() -> None:
    """The detector fires on every class-form addressing shape and stays blind
    to legal instance-side payload access (else the lock catches nothing)."""
    # Containment forms.
    assert _JSONB_CONTAINMENT.search(
        'Job.input_params.contains({"material_id": str(mid)})'
    )
    assert _JSONB_CONTAINMENT.search('Job.input_params.op("@>")({"course_node_id": x})')
    # Class-form subscript addressing (new).
    assert _JSONB_CONTAINMENT.search('Job.input_params["material_id"].astext == x')
    # Instance-side payload read — legal, must NOT match.
    assert not _JSONB_CONTAINMENT.search("p = job.input_params or {}")
    # A different JSONB column — must NOT match.
    assert not _JSONB_CONTAINMENT.search("Job.stage_progress.contains([str(x)])")


# ── no augmentation in the former hooks (acceptance 10) ──────────────────

_AUGMENT = re.compile(r"augmented\s*=\s*\[\*victim_ids")


def test_no_augmented_cancel_hooks_in_src() -> None:
    """None of the three former hooks concatenate course_node_id onto victims."""
    offenders: list[str] = []
    for py in _SRC.rglob("*.py"):
        for i, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if _AUGMENT.search(line):
                offenders.append(f"{py.relative_to(_SRC)}:{i}: {line.strip()}")
    assert not offenders, "augmented cancel-hook survived L1b:\n" + "\n".join(offenders)


def test_augment_detector_positive_control() -> None:
    """The augmentation detector fires on a synthetic fragment (else it is blind)."""
    assert _AUGMENT.search("        augmented = [*victim_ids, course_node_id]")
