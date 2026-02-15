# PD-015: Monitoring (Netdata) — Detail

## Docker Compose

Додати до `docker-compose.prod.yaml`:

```yaml
  netdata:
    image: netdata/netdata:stable
    container_name: netdata
    restart: unless-stopped
    hostname: course-supporter-vps
    cap_add:
      - SYS_PTRACE
    security_opt:
      - apparmor:unconfined
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - netdata-config:/etc/netdata
      - netdata-lib:/var/lib/netdata
    environment:
      - DO_NOT_TRACK=1           # no Netdata Cloud telemetry
      - NETDATA_CLAIM_TOKEN=     # no cloud claim
    deploy:
      resources:
        limits:
          memory: 200M
    networks:
      - default
      - shared-net
```

## Telegram Alerts

### 1. Створити Telegram bot

```
@BotFather → /newbot → course-supporter-alerts
```

Зберегти bot token та chat_id.

### 2. Netdata alert config

Створити `health_alarm_notify.conf`:

```ini
# /etc/netdata/health_alarm_notify.conf
SEND_TELEGRAM="YES"
TELEGRAM_BOT_TOKEN="<bot-token>"
DEFAULT_RECIPIENT_TELEGRAM="<chat-id>"
```

Mount через volume або exec в контейнер.

### 3. Custom Alert Thresholds

```yaml
# Custom alarms (optional — Netdata has good defaults)
# /etc/netdata/health.d/custom.conf

alarm: disk_space_warning
on: disk.space
lookup: average -1m percentage of used
units: %
every: 1m
warn: $this > 80
crit: $this > 90
info: Disk space usage

alarm: ram_usage_warning
on: system.ram
lookup: average -1m percentage of used
units: %
every: 1m
warn: $this > 85
crit: $this > 95
info: RAM usage
```

## Nginx Config

Вже включено в PD-010:

```nginx
location /netdata/ {
    auth_basic "Monitoring";
    auth_basic_user_file /etc/nginx/.htpasswd_netdata;
    proxy_pass http://netdata_backend/;
    proxy_set_header Host $host;
}
```

## Що моніторимо

| Метрика | Default Alert | Custom |
|---|---|---|
| Disk space | Yes (< 20%) | < 10% critical |
| RAM usage | Yes | > 85% warning |
| CPU | Yes (sustained) | — |
| Docker containers | Restart detection | — |
| Network I/O | Yes | — |
| PostgreSQL (якщо є plugin) | Connections | — |

## Тестування

1. Відкрити `https://api.pythoncourse.me/netdata/` — dashboard доступний
2. Basic auth працює
3. Docker container metrics видно
4. Trigger test alert: `docker exec netdata /usr/libexec/netdata/plugins.d/alarm-notify.sh test`

## Definition of Done

- [ ] Netdata контейнер запущено
- [ ] Dashboard доступний через nginx з basic auth
- [ ] Telegram alerts налаштовані
- [ ] RAM < 200MB (перевірити через htop)
- [ ] Документ оновлений відповідно до фінальної реалізації
