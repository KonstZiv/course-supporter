# PD-019: Deploy Documentation — Detail

## Контекст

Після завершення технічних задач спрінту — зафіксовано процедури в `docs/deployment.md`. Документ self-sufficient для першого deploy та повторних деплоїв.

## Реалізація

### Файл: `docs/deployment.md`

Єдиний deployment guide, 9 секцій:

1. **Prerequisites** — VPS, Docker, домен, B2, LLM keys, GitHub deploy key
2. **First Deploy** — 11 покрокових команд від clone до verify
3. **Subsequent Deploys** — автоматичний (GitHub Actions) та ручний
4. **Environment Variables Reference** — таблиця всіх env vars з defaults
5. **Tenant Management** — CLI команди (create-tenant, create-key, list, revoke, deactivate)
6. **Monitoring** — health check, Netdata dashboard, logs, UptimeRobot
7. **Backup & Restore** — pg_dump/psql
8. **Rollback** — app (git checkout + rebuild) та migration (alembic downgrade)
9. **Troubleshooting** — 7 типових проблем з діагностикою та рішеннями

### Джерела інформації

Документ консолідує та структурує інформацію з:
- `current-doc/S2-prod-deploy/infrastructure/README.md`
- `current-doc/S2-prod-deploy/infrastructure/deployment-guide.md`
- `current-doc/S2-prod-deploy/infrastructure/netdata-setup.md`
- `.env.prod.example`
- `docker-compose.prod.yaml`
- `Dockerfile`
- `.github/workflows/deploy.yml`
- `scripts/manage_tenant.py`

## Definition of Done

- [x] `docs/deployment.md` створено
- [x] First deploy instructions — від нуля до працюючого API (11 кроків)
- [x] Env vars reference — повна таблиця з defaults
- [x] Troubleshooting — 7 типових проблем
- [x] Rollback procedure — app + migration
- [x] Tenant management reference
- [x] Monitoring та backup/restore
- [x] Документ оновлений відповідно до фінальної реалізації
