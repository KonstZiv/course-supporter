"""The migration that admits the author's criteria composition job (DD-SP-DR).

``criteria_composition_jobtype`` widens two CHECKs of ``jobs``: the job types
and the legal (type, subject) pairs. What is locked here, against the real
schema:

* one head, and the revision follows the head it was written on;
* up, down one, up again on a clean database: the row of the new type is
  refused one revision below and admitted at the head, in its one legal pair
  and in no other;
* the downgrade refuses to run while a row of the new type exists — the way
  the key-explanation widening before it does, by the CHECK it re-adds — and
  leaves the database at the head with the row intact.

Requires ``docker compose up -d``; run with ``--run-db``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from course_supporter.config import get_settings
from course_supporter.jobs.job_type import JOB_SUBJECT_TYPE, JobType
from course_supporter.storage.orm import Job

pytestmark = pytest.mark.requires_db

REVISION = "criteria_composition_jobtype"
BELOW = "v1_author_interface_language"


@pytest.fixture()
def engine() -> Iterator[Engine]:
    engine = create_engine(get_settings().database_url)
    yield engine
    engine.dispose()


@contextmanager
def _at_head_afterwards(cfg: Config, engine: Engine) -> Iterator[None]:
    """Whatever a test does to the schema, the next one finds it at the head.

    The rows of the new type are cleared on both sides: a row another test left
    behind (it failed before its own cleanup) would make the downgrade refuse,
    and this test is about that refusal, not about their hygiene.
    """
    command.upgrade(cfg, "head")
    _clear(engine)
    try:
        yield
    finally:
        command.upgrade(cfg, "head")
        _clear(engine)


def _clear(engine: Engine) -> None:
    with Session(engine) as session:
        session.execute(
            delete(Job).where(Job.job_type == JobType.CRITERIA_COMPOSITION.value)
        )
        session.commit()


def _composition_job(subject_type: str | None = None) -> Job:
    return Job(
        job_type=JobType.CRITERIA_COMPOSITION.value,
        subject_type=subject_type or JOB_SUBJECT_TYPE[JobType.CRITERIA_COMPOSITION],
        subject_id=uuid.uuid4(),
        status="queued",
    )


def _insert(engine: Engine, job: Job) -> None:
    with Session(engine) as session:
        session.add(job)
        session.commit()


def _version(engine: Engine) -> str | None:
    with engine.connect() as conn:
        version: str | None = conn.scalar(
            text("SELECT version_num FROM alembic_version")
        )
    return version


def test_one_head_and_the_revision_follows_the_author_language() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = script.get_heads()
    revision = script.get_revision(REVISION)

    assert heads == [REVISION]
    assert revision is not None
    assert revision.down_revision == BELOW


def test_up_down_up_admits_the_new_type_only_at_the_head(engine: Engine) -> None:
    cfg = Config("alembic.ini")
    with _at_head_afterwards(cfg, engine):
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "-1")
        assert _version(engine) == BELOW
        with pytest.raises(IntegrityError, match="ck_jobs_job_type"):
            _insert(engine, _composition_job())

        command.upgrade(cfg, "head")
        assert _version(engine) == REVISION
        # Both CHECKs pass the row: the type is in the first, its pair in the
        # second. Mutating either out of the migration turns this red.
        _insert(engine, _composition_job())
        with Session(engine) as session:
            stored = session.scalar(
                text("SELECT count(*) FROM jobs WHERE job_type = :t"),
                {"t": JobType.CRITERIA_COMPOSITION.value},
            )
        assert stored == 1


@pytest.mark.parametrize("subject_type", ["homework_submission", "course_node"])
def test_the_new_type_has_one_legal_subject_and_no_other(
    engine: Engine, subject_type: str
) -> None:
    cfg = Config("alembic.ini")
    with _at_head_afterwards(cfg, engine):
        command.upgrade(cfg, "head")

        with pytest.raises(IntegrityError, match="ck_jobs_subject_type_legal"):
            _insert(engine, _composition_job(subject_type))


def test_the_downgrade_refuses_while_a_row_of_the_new_type_exists(
    engine: Engine,
) -> None:
    cfg = Config("alembic.ini")
    with _at_head_afterwards(cfg, engine):
        command.upgrade(cfg, "head")
        _insert(engine, _composition_job())

        with pytest.raises(IntegrityError, match="ck_jobs"):
            command.downgrade(cfg, "-1")

        # Postgres DDL is transactional: nothing of the failed step stands.
        assert _version(engine) == REVISION
        with Session(engine) as session:
            assert (
                session.scalar(
                    text("SELECT count(*) FROM jobs WHERE job_type = :t"),
                    {"t": JobType.CRITERIA_COMPOSITION.value},
                )
                == 1
            )
