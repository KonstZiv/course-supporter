"""The shipped way a generation is asked for (mentor-rebuild task 06).

The service names what it needs through
:class:`~course_supporter.homework.reference_service.ExplanationQueue` — one
method, two identifiers, no return value. This module is the implementation
that puts the work on the real queue; it lives outside the service on purpose,
so nothing in the service knows about ARQ, Redis or ``JobType``.

Replacing it is a matter of passing something else with a ``request`` method:
a counter in a test, an inline runner in a one-off script, a different broker
later. Nothing else changes — as long as the replacement, too, raises
:class:`~course_supporter.homework.reference_service.GenerationInProgressError`
when another job of the task is in flight.
"""

from __future__ import annotations

import uuid

from arq.connections import ArqRedis
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from course_supporter.enqueue import enqueue_key_explanation
from course_supporter.homework.reference_service import GenerationInProgressError


class ArqExplanationQueue:
    """Asks for a generation by creating its Job and dispatching it to ARQ."""

    def __init__(
        self, *, redis: ArqRedis, session: AsyncSession, tenant_id: uuid.UUID
    ) -> None:
        self._redis = redis
        self._session = session
        self._tenant_id = tenant_id

    async def request(
        self, *, authored_document_id: uuid.UUID, reference_id: uuid.UUID
    ) -> None:
        """Enqueue the work; the caller does not wait for it.

        The tenant comes from the request context the route already resolved,
        not from a second read of the task: the route proved ownership before
        the service was called, and reading it again would be a second answer
        to a question already answered.

        Raises:
            GenerationInProgressError: the database refused the job. For a
                well-formed job the one refusal it gives is
                ``uq_jobs_subject_in_flight`` — another job of this task is in
                flight — so the refusal is translated whole rather than read
                apart. The transaction is void by then; the caller rolls its
                session back before using it again.
        """
        try:
            await enqueue_key_explanation(
                redis=self._redis,
                session=self._session,
                tenant_id=self._tenant_id,
                authored_document_id=authored_document_id,
                reference_id=reference_id,
            )
        except IntegrityError as exc:
            raise GenerationInProgressError(authored_document_id) from exc
