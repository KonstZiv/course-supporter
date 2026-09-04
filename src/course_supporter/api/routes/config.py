"""Read-only lookup endpoints for client-side config (Task 2.4.13).

Currently exposes:

* ``GET /api/v1/config/languages`` — the allowed course-language
  whitelist (single source of truth, mirrors ``config/languages.yaml``).
  The UI consumes this to populate its language selector — no hardcoded
  list on the client.

Future home for similar lookup endpoints (e.g. DD-2.4-I —
``allowed_extensions`` for the upload modal). Auth-protected so the
contract matches the rest of the API surface even though the data is
non-tenant-scoped.

The student portal has its own twin of the languages route
(``routes/portal_config.py``) serving the same list under a bearer
session. The two are not duplication to be collapsed: this door is an
API-key scope lock, and a portal session carries no scope to pass it.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends

from course_supporter.api.schemas import AllowedLanguagesResponse
from course_supporter.auth.context import TenantContext
from course_supporter.auth.registry import AuthScope
from course_supporter.auth.scopes import require_scope
from course_supporter.language import list_allowed

logger = structlog.get_logger()

router = APIRouter(prefix="/config", tags=["config"])

SharedDep = Annotated[
    TenantContext, Depends(require_scope(AuthScope.PREP, AuthScope.CHECK))
]


@router.get("/languages")
async def get_allowed_languages(_tenant: SharedDep) -> AllowedLanguagesResponse:
    """Return the project-wide course-language whitelist.

    Codes are canonical ISO 639-3; ``name_en`` is always populated
    (via ``iso639``), ``name_native`` is best-effort (None when the
    library does not carry a native-script name for that language).
    """
    items = list_allowed()
    return AllowedLanguagesResponse(items=items, total=len(items))
