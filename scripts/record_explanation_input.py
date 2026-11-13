"""Record the explanation stage's input of one submission, from production.

Read-only: a READ ONLY transaction on the database (``DATABASE_URL``) and one
download of the stored file from S3 (the app's S3 settings). Nothing is
written, no model is called. The logic is
``course_supporter.homework.explanation_input_rebuild``.

The stage does not keep its input whole, so it is rebuilt from the rows the
submission left — the verdict rows, the list they were judged against, the
course links kept, the task's texts — and from the stored file read again by
the door. Then each request the call register holds for the stage's first
rung is rebuilt from it and its hash compared with the register's: a match
means the record is byte for byte the input the stage was sent.

Usage (from the repository root)::

    uv run python scripts/record_explanation_input.py SUBMISSION_ID --out RECORD.json

``RECORD.json`` is what ``scripts/measure_explanation_prompt.py`` reads. It
holds a student's work and the answer the stage kept: keep it out of the
repository.

Exit code: 0 when the register confirms the record, 2 when it does not
(the record is still written; ``provenance.verification`` says why).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from course_supporter.config import get_settings
from course_supporter.homework.explanation_input_rebuild import (
    rebuild_input,
    verify_against_register,
)
from course_supporter.homework.explanation_measure import STAGE_NAME, record_of
from course_supporter.homework.path_config import ModelStage, load_path_config
from course_supporter.llm.registry import load_registry
from course_supporter.storage.s3 import S3Client


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("submission_id", type=uuid.UUID)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args(argv)


async def _record(submission_id: uuid.UUID) -> dict[str, Any]:
    settings = get_settings()
    stage = load_path_config(settings.submission_paths_config_path).stages[STAGE_NAME]
    if not isinstance(stage, ModelStage):
        sys.exit(f"{STAGE_NAME} is not a model stage")
    registry = load_registry(settings.external_services_path)

    engine = create_async_engine(settings.database_url)
    try:
        async with (
            AsyncSession(engine) as session,
            S3Client(
                endpoint_url=settings.s3_endpoint,
                access_key=settings.s3_access_key,
                secret_key=settings.s3_secret_key.get_secret_value(),
                bucket=settings.s3_bucket,
            ) as s3,
        ):
            await session.execute(text("SET TRANSACTION READ ONLY"))
            rebuilt = await rebuild_input(session, s3, submission_id)
            await session.rollback()
    finally:
        await engine.dispose()

    verification = await verify_against_register(
        rebuilt, stage=stage, registry=registry
    )
    return record_of(
        rebuilt.shown,
        submission_id=str(submission_id),
        provenance={
            "recorded_at": datetime.now(UTC).isoformat(),
            "task_type": rebuilt.task_type,
            "language_code": rebuilt.language_code,
            "verification": verification,
            "stored_explanation": rebuilt.stored_explanation,
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    record = asyncio.run(_record(args.submission_id))
    args.out.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    verification = record["provenance"]["verification"]
    print(json.dumps(verification, ensure_ascii=False, indent=2))
    print(f"record: {args.out}")
    return 0 if verification["matched"] else 2


if __name__ == "__main__":
    sys.exit(main())
