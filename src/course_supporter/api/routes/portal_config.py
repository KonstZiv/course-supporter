"""Read-only lookup endpoints for the student portal (step Г2, п.5).

Routes
------
- ``GET /portal/languages`` — the allowed language whitelist, so the
  submission form can offer the review language as a closed choice.

Portal twin of the author-side ``routes/config.py``, and a separate module
for the same reason that one exists: a lookup is neither authentication nor
a course read, and the author side already keeps this class of endpoint
apart. It is also where the next portal lookup belongs (``DD-SP-V`` — the
submission policy the form now mirrors by hand), which is the whole argument
against folding a lookup into ``portal_auth``.

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
from course_supporter.api.schemas import AllowedLanguagesResponse
from course_supporter.auth.context import StudentContext
from course_supporter.language import list_allowed

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
