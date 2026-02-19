# PD-008: Dockerfile (multi-stage)

## Що

Multi-stage Dockerfile: builder stage (uv + dependencies) + runtime stage (slim image, non-root user).

## Навіщо

Production image повинен бути мінімальним (менше attack surface), з non-root user, без dev dependencies.

## Ключові рішення

- `python:3.13-slim` base
- uv для dependency resolution в builder stage
- Non-root user `app` в runtime
- `HEALTHCHECK` directive
- Копіюємо src/, config/, prompts/, migrations/, alembic.ini

## Acceptance Criteria

- [ ] `docker build` успішний
- [ ] Image працює: `docker run ... /health` → 200 (з DB)
- [ ] Non-root user: `whoami` → app
- [ ] Dev dependencies відсутні в image
- [ ] Image size < 500MB
