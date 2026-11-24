"""What the open-course routes share (O-1).

Refusals:
    Every 4xx of these routes carries a code a screen picks its words by, in
    the project's coded form (``_billing_shared``)::

        {"detail": {"code": "<code>", "details": "<text for the logs>"}}

    and never a value from the request (condition of Mentor 5, 23.11): no
    email, login, token or password, not even echoed back. :class:`JoinRefusal`
    is every code; the code fixes the status. Request validation (422) answers
    too without the values: :class:`PrivateValidationRoute` drops ``input`` from
    FastAPI's error list — for these routes only, the rest of the API is as it
    was.

Rate limits:
    :func:`limit` counts a request in Redis (``auth.rate_limits``) and turns an
    excess into 429 ``rate_limited`` with ``Retry-After``; the numbers of the
    limits are not in the answer.

Client address:
    :func:`client_ip` is ``request.client.host`` — the application has no
    proxy-header rule of its own; uvicorn rewrites ``client`` from
    ``X-Forwarded-For`` only for the proxies in ``FORWARDED_ALLOW_IPS``
    (default ``127.0.0.1``).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from enum import StrEnum
from typing import Any, Final

from fastapi import HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from course_supporter.auth.rate_limits import RateLimitedError, RedisRateLimiter
from course_supporter.config import RateLimit


class JoinRefusal(StrEnum):
    """Why an open-course route refused — the code a screen reads."""

    COURSE_NOT_FOUND = "course_not_found"
    """No open course by this link: an unknown or closed key, a deleted
    course, an inactive author — one answer for all (404)."""

    SIGNUP_NOT_FOUND = "signup_not_found"
    """No pending request: unknown, already confirmed or expired (404)."""

    LOGIN_TAKEN = "login_taken"
    """The login is taken in this author's portal (409)."""

    EMAIL_CONFIRMATION_REQUIRED = "email_confirmation_required"
    """Joining a course needs a confirmed email first (409)."""

    RATE_LIMITED = "rate_limited"
    """Too many requests; try again after ``Retry-After`` seconds (429)."""


STATUS: Final[dict[JoinRefusal, int]] = {
    JoinRefusal.COURSE_NOT_FOUND: 404,
    JoinRefusal.SIGNUP_NOT_FOUND: 404,
    JoinRefusal.LOGIN_TAKEN: 409,
    JoinRefusal.EMAIL_CONFIRMATION_REQUIRED: 409,
    JoinRefusal.RATE_LIMITED: 429,
}

_DETAILS: Final[dict[JoinRefusal, str]] = {
    JoinRefusal.COURSE_NOT_FOUND: "No open course by this link.",
    JoinRefusal.SIGNUP_NOT_FOUND: "No pending enrollment request.",
    JoinRefusal.LOGIN_TAKEN: "This login is taken; choose another.",
    JoinRefusal.EMAIL_CONFIRMATION_REQUIRED: (
        "Confirm an email address first, then join the course."
    ),
    JoinRefusal.RATE_LIMITED: "Too many requests; try again later.",
}


def refusal(
    code: JoinRefusal,
    *,
    headers: dict[str, str] | None = None,
    signup_id: str | None = None,
) -> HTTPException:
    """The refusal of ``code`` (module docstring): raise it.

    ``signup_id`` — the server's own id of a request, never a value the client
    sent — goes into the detail when the client needs it to continue (a login
    taken at confirm: the portal asks for another login by that id).
    """
    detail: dict[str, str] = {"code": code.value, "details": _DETAILS[code]}
    if signup_id is not None:
        detail["signup_id"] = signup_id
    return HTTPException(status_code=STATUS[code], detail=detail, headers=headers)


def responses(*codes: JoinRefusal) -> dict[int | str, dict[str, Any]]:
    """The OpenAPI ``responses`` of a route that refuses with ``codes``."""
    found: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        listed = found.setdefault(
            STATUS[code], {"description": "Refused; detail.code:"}
        )
        listed["description"] += f" {code.value}"
    return found


def client_ip(request: Request) -> str:
    """The client's address (module docstring)."""
    return request.client.host if request.client is not None else "unknown"


async def limit(
    limiter: RedisRateLimiter, name: str, subject: str, rule: RateLimit
) -> None:
    """Count one request; 429 ``rate_limited`` over the limit."""
    try:
        await limiter.hit(name, subject, rule)
    except RateLimitedError as exc:
        raise refusal(
            JoinRefusal.RATE_LIMITED, headers={"Retry-After": str(exc.retry_after)}
        ) from exc


class PrivateValidationRoute(APIRoute):
    """A route whose 422 names the fields and the rules, not the values."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def private_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                errors = [
                    {"type": e.get("type"), "loc": e.get("loc"), "msg": e.get("msg")}
                    for e in exc.errors()
                ]
                return JSONResponse(status_code=422, content={"detail": errors})

        return private_handler
