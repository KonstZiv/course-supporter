# PD-015: Monitoring (Netdata)

## Що

Netdata контейнер для системного моніторингу VPS. Dashboard через nginx з basic auth. Alerts в Telegram.

## Навіщо

Потрібна видимість: RAM, CPU, disk, container health. Netdata — zero-config, один контейнер, ~100-150 MB RAM. Alerts попереджають про проблеми до того як вони стануть критичними.

## Ключові рішення

- Netdata контейнер в production compose
- Dashboard на `api.pythoncourse.me/netdata/` з basic auth
- Alerts: disk < 20%, RAM > 85%, container restart → Telegram
- Netdata Cloud не використовуємо (self-hosted only)

## Acceptance Criteria

- [ ] Netdata dashboard доступний з basic auth
- [ ] CPU, RAM, disk, Docker metrics видно
- [ ] Telegram alerts налаштовані
- [ ] RAM overhead < 200MB
