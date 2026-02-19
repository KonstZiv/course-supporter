# PD-007: Admin CLI для управління tenants

## Що

CLI скрипт `scripts/manage_tenant.py` для створення tenants, видачі/відкликання API ключів. Працює через DB напряму.

## Навіщо

Admin UI — не в scope цього спрінту. CLI достатній для управління кількома tenants на старті. Дозволяє onboard нового клієнта за хвилину.

## Ключові рішення

- Один скрипт з subcommands (argparse)
- Повний ключ виводиться тільки при створенні (далі — тільки prefix)
- Синхронне виконання (не async) — простіше для CLI

## Acceptance Criteria

- [ ] `create-tenant --name "Company A"` → створює tenant
- [ ] `create-key --tenant "Company A" --scopes prep,check` → видає ключ, виводить його
- [ ] `list-tenants` → показує всіх tenants
- [ ] `revoke-key --prefix cs_live_abc1` → деактивує ключ
- [ ] Тести на CLI commands
