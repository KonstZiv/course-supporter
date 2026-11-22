"""The public numbers (BIL-PUBLIC-NUMBERS): no sign-in, no key.

Routes
------
- ``GET /public/numbers`` — what a review costs and how far a package goes,
  from the platform's data (``billing.public_numbers``): the site, the about
  page, the portal and the author's app show these. Computed once per start
  of the app; every process serves the same bytes, so the ``ETag`` is the
  same too. ``If-None-Match`` with it — 304. Not computed yet — 503
  ``numbers_unavailable``, and the next request tries again.

No rate limit of its own: the app has none for public addresses by source
(the throttles of the login and the reset link are per login), and the answer
costs one Redis read.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response

from course_supporter.api.routes._billing_shared import Refusal, refusal, responses
from course_supporter.api.schemas import PublicNumbersResponse
from course_supporter.billing.public_numbers import PublicNumbersStore, Published

router = APIRouter(tags=["public"])

CACHE_CONTROL: Final = "public, max-age=300"
NOT_COMPUTED = "The numbers are not computed yet; try again later."


async def current_numbers(request: Request) -> Published | None:
    """The public numbers of this start; None while there are none."""
    store: PublicNumbersStore | None = getattr(
        request.app.state, "public_numbers", None
    )
    return await store.get() if store is not None else None


NumbersDep = Annotated[Published | None, Depends(current_numbers)]


def _matches(if_none_match: str | None, etag: str) -> bool:
    if if_none_match is None:
        return False
    tags = {tag.strip().removeprefix("W/") for tag in if_none_match.split(",")}
    return etag in tags or "*" in tags


@router.get(
    "/public/numbers",
    response_model=PublicNumbersResponse,
    responses={
        304: {"description": "Not modified: If-None-Match names the ETag."},
        **responses(Refusal.NUMBERS_UNAVAILABLE),
    },
)
async def get_public_numbers(request: Request, found: NumbersDep) -> Response:
    """What a review costs and how far a package goes (module docstring).

    The same answer until the server's next start; ``Cache-Control: public,
    max-age=300`` and an ``ETag``.
    """
    if found is None:
        raise refusal(Refusal.NUMBERS_UNAVAILABLE, NOT_COMPUTED)
    headers = {"ETag": found.etag, "Cache-Control": CACHE_CONTROL}
    if _matches(request.headers.get("if-none-match"), found.etag):
        return Response(status_code=304, headers=headers)
    return Response(found.body, media_type="application/json", headers=headers)
