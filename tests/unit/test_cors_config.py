"""Guard: CORS must admit every HTTP method the API's routes use.

A browser asks the API's permission (a CORS preflight) before a cross-origin
request that uses a method other than GET, HEAD or POST, or a custom header
such as ``X-API-Key``. A method missing from ``Access-Control-Allow-Methods``
is refused there, the request is never sent, and the SPA sees only "CORS
error".
That happened twice: PATCH (2026-08-28, node rename) and PUT (2026-09-30,
saving a test draft). The guards compare the method lists with the route
table, so a route with a new method fails here instead of in a browser.

Two lists are in reach: the code default and the prod template. An override
in the environment (``CORS_ALLOWED_METHODS``, e.g. the server's ``.env.prod``)
replaces the default whole and is outside any test.
"""

import json
import uuid
from collections.abc import Iterable
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.middleware.cors import CORSMiddleware
from starlette.routing import BaseRoute

from course_supporter.api.app import app
from course_supporter.config import Settings

# The framework adds HEAD to GET routes and OPTIONS is the preflight itself;
# neither is a method the SPA sends.
_NOT_SENT_BY_SPA = frozenset({"HEAD", "OPTIONS"})
_PROD_TEMPLATE = Path(__file__).resolve().parents[2] / ".env.prod.example"
_PANEL_ORIGIN = "https://panel.pythoncourse.me"
_TEST_DRAFT = "/api/v1/tests/{document_id}/draft"


def _route_methods(routes: Iterable[BaseRoute]) -> dict[str, set[str]]:
    """Map each HTTP method the routes use to the paths that use it."""
    found: dict[str, set[str]] = {}
    for route in routes:
        for method in getattr(route, "methods", None) or ():
            if method not in _NOT_SENT_BY_SPA:
                found.setdefault(method, set()).add(getattr(route, "path", ""))
        # A Mount carries its sub-application's routes.
        for method, paths in _route_methods(getattr(route, "routes", ())).items():
            found.setdefault(method, set()).update(paths)
    return found


def _missing(allowed: Iterable[str]) -> dict[str, list[str]]:
    """Route methods absent from ``allowed``, each with the paths using it."""
    used = _route_methods(app.routes)
    # A guard over an empty route table would pass without measuring anything.
    assert {"GET", "POST", "PUT", "PATCH", "DELETE"} <= used.keys(), used.keys()
    allowed_set = set(allowed)
    return {m: sorted(p) for m, p in used.items() if m not in allowed_set}


def test_default_allows_every_route_method() -> None:
    default = Settings.model_fields["cors_allowed_methods"].default
    assert _missing(default) == {}


def test_prod_template_allows_every_route_method() -> None:
    key = "CORS_ALLOWED_METHODS="
    lines = [ln for ln in _PROD_TEMPLATE.read_text().splitlines() if ln.startswith(key)]
    assert len(lines) == 1, lines
    assert _missing(json.loads(lines[0].removeprefix(key))) == {}


@pytest.mark.asyncio
async def test_put_preflight_on_test_draft_passes_cors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The author program's draft save gets past the app's own CORS layer.

    Only the origin list is replaced: it is deployment config and empty by
    default. Methods, headers, credentials and the middleware order are the
    app's own, so a hard-coded method list in ``app.py`` fails here as well.
    """
    assert _TEST_DRAFT in _route_methods(app.routes).get("PUT", set())
    cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    monkeypatch.setitem(cors.kwargs, "allow_origins", [_PANEL_ORIGIN])
    # The stack is built on the first request and cached; drop it so the
    # request below builds one with the origin above.
    monkeypatch.setattr(app, "middleware_stack", None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.options(
            _TEST_DRAFT.format(document_id=uuid.uuid4()),
            headers={
                "Origin": _PANEL_ORIGIN,
                "Access-Control-Request-Method": "PUT",
                # What the author program sends with a draft save
                # (course-supporter-ui, src/api/client.ts).
                "Access-Control-Request-Headers": "content-type, x-api-key",
            },
        )

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == _PANEL_ORIGIN
    assert "PUT" in response.headers["access-control-allow-methods"].split(", ")
