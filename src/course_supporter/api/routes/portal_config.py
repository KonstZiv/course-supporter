"""Read-only lookup endpoints for the student portal (step Г2, п.5).

Routes
------
- ``GET /portal/languages`` — the allowed language whitelist, so the
  submission form can offer the review language as a closed choice.
- ``GET /portal/submission-policy`` — the extension allowlist, size caps
  and archive-only rule per assignment kind, so the form stops carrying
  its own copies of them (``DD-SP-V``, step Д).

Portal twin of the author-side ``routes/config.py``, and a separate module
for the same reason that one exists: a lookup is neither authentication nor
a course read, and the author side already keeps this class of endpoint
apart. The submission policy landing here rather than in ``portal_auth`` is
that same argument, now spent.

Why a twin rather than reuse: the data is identical and NOT tenant-scoped,
but the author route's lock is contractual (``config.py:11-13`` — "so the
contract matches the rest of the API surface"), and it is an API-key scope
lock. A portal session carries a bearer token and no scope, so it cannot
pass that door at all. The twin serves the same list under the student's
own key rather than widening the author route, which would have traded a
real lock for a shared one.

Response shape is the author route's ``AllowedLanguagesResponse`` verbatim,
so the interface reuses one type for both surfaces.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from course_supporter.api.deps import get_current_student
from course_supporter.api.schemas import (
    AllowedLanguagesResponse,
    SubmissionPolicyEntry,
    SubmissionPolicyResponse,
)
from course_supporter.auth.context import StudentContext
from course_supporter.homework.submission_core import (
    ALLOWED_HOMEWORK_EXTENSIONS,
    MAX_HOMEWORK_SIZE,
    PROJECT_SUBMISSION_MAX_UPLOAD_BYTES,
)
from course_supporter.language import list_allowed
from course_supporter.models.source import AssignmentType

router = APIRouter(tags=["portal"])

StudentDep = Annotated[StudentContext, Depends(get_current_student)]


@router.get("/portal/languages")
async def get_portal_languages(_student: StudentDep) -> AllowedLanguagesResponse:
    """Return the language whitelist for the portal's review-language field.

    Same list, same shape and same source (``config/languages.yaml``) as
    the author route: the student picks the language their review is
    written in, and the server accepts only what is on this list (422
    otherwise). Serving it means the form can be a closed selector, so the
    422 is never reached by an ordinary choice.

    Authenticated but not otherwise gated: the list is the same for every
    student of every tenant, and requiring a session only keeps the
    endpoint off the public surface.
    """
    items = list_allowed()
    return AllowedLanguagesResponse(items=items, total=len(items))


@router.get("/portal/submission-policy")
async def get_portal_submission_policy(
    _student: StudentDep,
) -> SubmissionPolicyResponse:
    """Return what the submission door accepts, per assignment kind.

    The form used to hold its own copy of both the extension list and the
    size cap, with nothing failing when the server moved and it did not:
    before PR #49 the copy was 27 formats short, so a student could not
    even pick a ``.docx`` the server would have accepted, and the 10 MiB
    number it still carries cuts off a project archive the server allows at
    100 MB. Serving the numbers is what lets both copies go (``DD-SP-V``).

    Every field is READ from the door it describes — no constant is defined
    here, and none of the three sources is reshaped:

    * ``accept`` — :data:`ALLOWED_HOMEWORK_EXTENSIONS`, which is already
      dot-prefixed (``".py"``) because that is the form
      ``validate_homework_file`` compares against. Sorted for a stable body.
    * ``max_bytes`` — :data:`MAX_HOMEWORK_SIZE` or
      :data:`PROJECT_SUBMISSION_MAX_UPLOAD_BYTES`, chosen by the same
      ``task_type == PROJECT`` test the submission route makes.
    * ``archive_only`` — true exactly where ``project_preflight`` refuses a
      loose file with ``ARCHIVE_ONLY``.

    Authenticated but not otherwise gated, like the language list beside
    it: the policy is identical for every student of every tenant, and the
    session only keeps the endpoint off the public surface.
    """
    accept = sorted(ALLOWED_HOMEWORK_EXTENSIONS)
    policies = {
        kind: SubmissionPolicyEntry(
            max_bytes=(
                PROJECT_SUBMISSION_MAX_UPLOAD_BYTES
                if kind is AssignmentType.PROJECT
                else MAX_HOMEWORK_SIZE
            ),
            accept=accept,
            archive_only=kind is AssignmentType.PROJECT,
        )
        for kind in AssignmentType
    }
    return SubmissionPolicyResponse(policies=policies)
