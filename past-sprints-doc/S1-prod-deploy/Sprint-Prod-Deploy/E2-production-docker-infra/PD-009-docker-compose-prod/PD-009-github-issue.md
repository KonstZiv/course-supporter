# PD-009: docker-compose.prod.yaml

## Що

Production compose: app + PostgreSQL, підключення до `shared-net`, env_file, restart policies, healthchecks.

## Навіщо

Production оточення відрізняється від dev: немає MinIO (Backblaze B2), потрібні restart policies, secrets через env_file, інтеграція з існуючим nginx через shared-net.

## Ключові рішення

- App container не expose ports — трафік тільки через nginx (shared-net)
- PostgreSQL з healthcheck та persistent volume
- `env_file: .env.prod` для secrets
- `restart: unless-stopped` для автовідновлення
- Окрема мережа `default` + зовнішня `shared-net`

## Acceptance Criteria

- [ ] `docker compose -f docker-compose.prod.yaml up -d` запускає app + DB
- [ ] App доступний з `shared-net` для nginx
- [ ] PostgreSQL persistent volume
- [ ] Restart policies working
- [ ] `.env.prod.example` з усіма необхідними змінними
