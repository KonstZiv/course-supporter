"""S3 cleanup ARQ worker (vision §3 KD13).

Async hard-delete of S3 files after a soft-delete cascade flips the
owning entity (``AuthoredDocument`` / ``HomeworkSubmission``) to
``deleted_at IS NOT NULL``. Per KD13:

* Input is ``file_keys`` only — the task does **not** read the DB.
  The originating record may already be scrubbed by the time this
  worker runs; the cleanup must work from the explicit list.
* **Idempotent**: missing object in S3 (``NoSuchKey`` / ``404`` /
  ``NotFound``) is success — re-running with the same ``file_keys``
  produces the same result on the second invocation.
* **Per-key non-fatal errors** (any other ``ClientError`` returned
  by S3, e.g. ``AccessDenied``, throttling codes like ``SlowDown``)
  are recorded in the result's ``errors`` list and the task
  continues; the operator inspects ``Job.result_data`` for follow-up.
* **Connection-level failures** (``BotoCoreError``,
  ``EndpointConnectionError``, network/timeout) are *not* caught
  and propagate as exceptions, triggering ARQ's existing retry
  policy. The semantic distinction is that ``ClientError`` is a
  successful HTTP transaction with an AWS-level error code (an
  answer from the service, even if negative), while non-
  ``ClientError`` means we never got an answer (try again).

**Cascade integration is Phase 1+ work.** When ``AuthoredDocument``
(formerly ``MaterialEntry``) or ``HomeworkSubmission`` cascades
soft-delete the entity row, the cascade enqueues this task with
the entity's ``file_keys``::

    await arq.enqueue_job(
        "s3_cleanup_task",
        file_keys=["bucket/path1", "bucket/path2"],
        job_id="<job-uuid-str>",
    )

0.3 ships the worker only — no cascade binding lands here.
"""

from __future__ import annotations

from typing import Any

import structlog
from botocore.exceptions import ClientError

from course_supporter.storage.s3 import S3Client

logger = structlog.get_logger(__name__)

# S3 / S3-compatible "object does not exist" error codes. Treated as
# success per KD13 idempotency contract. botocore normalises some of
# these (NoSuchKey from native AWS; "404" / "NotFound" from B2 /
# certain S3-compatible providers).
_NOT_FOUND_ERROR_CODES: frozenset[str] = frozenset({"NoSuchKey", "404", "NotFound"})


async def s3_cleanup_task(
    ctx: dict[str, Any],
    *,
    file_keys: list[str],
    job_id: str,  # UUID as string (ARQ JSON serialization)
) -> dict[str, Any]:
    """Hard-delete S3 files for a soft-deleted entity.

    See module docstring for the full error-handling contract.
    Empty ``file_keys`` short-circuits to a no-op return; otherwise
    iterates the list, attempting one delete per key.

    ``job_id`` is logged for audit traceability only — the task does
    NOT read or write the ``Job`` row (the row may have been scrubbed
    by the cascade transaction by the time this task executes).

    Returns:
        A dict ``{"deleted": [<key>, ...], "errors": [{"key": <key>,
        "error": <str>}, ...]}``. ``deleted`` includes both
        successfully-deleted keys and keys that were already missing
        from S3 (idempotent).
    """
    log = logger.bind(job_id=job_id, file_keys_count=len(file_keys))

    if not file_keys:
        log.info("s3_cleanup.noop")
        return {"deleted": [], "errors": []}

    s3: S3Client = ctx["s3_client"]
    deleted: list[str] = []
    errors: list[dict[str, str]] = []

    for key in file_keys:
        try:
            await s3.delete_object(key)
            deleted.append(key)
        except ClientError as exc:
            error_code = str(exc.response.get("Error", {}).get("Code", ""))
            if error_code in _NOT_FOUND_ERROR_CODES:
                # Idempotent: missing file is success per KD13.
                deleted.append(key)
                log.debug("s3_cleanup.already_deleted", key=key, error_code=error_code)
            else:
                # Per-key error: AWS responded with an error code that
                # retry won't fix (AccessDenied) or signals throttling
                # (SlowDown). Record and continue; the operator sees
                # these in Job.result_data for triage.
                errors.append({"key": key, "error": f"{error_code}: {exc}"})
                log.warning(
                    "s3_cleanup.client_error",
                    key=key,
                    error_code=error_code,
                    error=str(exc),
                )

    log.info(
        "s3_cleanup.complete",
        deleted_count=len(deleted),
        error_count=len(errors),
    )
    return {"deleted": deleted, "errors": errors}
