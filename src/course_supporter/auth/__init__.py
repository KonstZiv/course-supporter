"""Authentication and API key management.

Note: ``require_scope`` lives in ``auth.scopes`` and is NOT re-exported here
to avoid a circular import (auth → scopes → api.deps → auth).
Import directly: ``from course_supporter.auth.scopes import require_scope``.
"""

from course_supporter.auth.context import TenantContext
from course_supporter.auth.keys import generate_api_key, hash_api_key

__all__ = ["TenantContext", "generate_api_key", "hash_api_key"]
