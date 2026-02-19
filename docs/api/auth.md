# Authentication & Authorization

!!! info "Coming in Sprint 2, Epic 7"
    Detailed auth guide with examples will be published here.

**Current auth model** (since Sprint 1):

- API key via `X-API-Key` header
- Scopes: `prep` (course preparation), `check` (homework checking)
- Rate limiting per tenant + scope
- Keys managed via Admin CLI (`manage_tenant.py`)

For architecture details, see [ADR-008: API key auth](../architecture/decisions.md#adr-008-api-key-auth-not-oauthjwt).
