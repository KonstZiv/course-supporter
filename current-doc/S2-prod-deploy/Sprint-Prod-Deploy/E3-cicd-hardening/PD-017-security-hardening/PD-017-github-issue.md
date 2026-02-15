# PD-017: Security Hardening

## Що

CORS restricted, security headers, disable debug mode, hide version info.

## Навіщо

Production API не повинен мати `CORS: ["*"]`, відкритий debug mode або leaking server info.

## Ключові рішення

- CORS: тільки конкретні origins (з env var)
- FastAPI: `docs_url=None` в production (або залишити для API consumers)
- No stack traces в error responses
- Environment-based config switching

## Acceptance Criteria

- [ ] CORS restricted в production
- [ ] Error responses не містять stack traces
- [ ] Security headers від nginx (PD-010)
- [ ] Swagger UI: рішення про доступність в production
