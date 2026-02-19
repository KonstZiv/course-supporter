# PD-016: GitHub Actions Deploy Workflow

## Що

CI/CD pipeline: push to main → test → build on VPS → deploy → run migrations.

## Навіщо

Ручний deploy через SSH — error-prone та повільний. Автоматизація гарантує що кожен merge проходить тести перед deploy.

## Ключові рішення

- GitHub Actions, не GitLab CI (репо на GitHub)
- Deploy через SSH (appleboy/ssh-action) — build on VPS, без Docker registry
- Послідовність: test job → deploy job (тільки якщо тести зелені)
- Alembic migration після deploy

## Acceptance Criteria

- [ ] Push to main triggers deploy
- [ ] Deploy тільки після зелених тестів
- [ ] Build image on VPS
- [ ] Alembic migration автоматично
- [ ] GitHub Secrets для SSH credentials
