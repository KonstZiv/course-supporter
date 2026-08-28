"""Guard: the CORS methods default must allow PATCH.

The SPA performs every in-place edit via PATCH — node rename, node-summary
Final, document, homework. When the API is cross-origin to the SPA, the
browser sends a CORS preflight; if ``PATCH`` is not advertised in
``Access-Control-Allow-Methods`` the request is rejected before it is sent,
and the failure is silent. Production overrides this via the
``CORS_ALLOWED_METHODS`` env var, so this asserts the code *default* (not an
env-influenced instance) to keep the default from silently regressing.
"""

from course_supporter.config import Settings


def test_cors_methods_default_allows_patch() -> None:
    default = Settings.model_fields["cors_allowed_methods"].default
    assert default is not None
    assert "PATCH" in default, (
        "CORS methods default must include PATCH — the SPA edits via PATCH "
        "and a cross-origin preflight silently drops it otherwise."
    )
