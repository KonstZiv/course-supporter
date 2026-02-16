# PD-015: Monitoring (Netdata) — Detail

## Реалізація

### Docker Compose — `docker-compose.prod.yaml`

Додано netdata сервіс:

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
      - DO_NOT_TRACK=1
      - NETDATA_CLAIM_TOKEN=
    deploy:
      resources:
        limits:
          memory: 200M
    networks:
      - default
      - shared-net
```

Додано named volumes: `netdata-config`, `netdata-lib`.

### Nginx — `deploy/nginx/course-supporter.conf`

Додано `/netdata/` location з resolver pattern та basic auth:

```nginx
    set $netdata_backend http://netdata:19999;

    location /netdata/ {
        auth_basic "Monitoring";
        auth_basic_user_file /etc/nginx/.htpasswd_netdata;
        proxy_pass $netdata_backend/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
```

### Config Templates — `deploy/netdata/`

- `health_alarm_notify.conf` — Telegram alert config (placeholders для bot token та chat_id)
- `custom-alerts.conf` — disk (>80% warn, >90% crit) та RAM (>85% warn, >95% crit) thresholds

Копіювання в контейнер:

```bash
docker cp deploy/netdata/health_alarm_notify.conf netdata:/etc/netdata/health_alarm_notify.conf
docker cp deploy/netdata/custom-alerts.conf netdata:/etc/netdata/health.d/custom.conf
docker restart netdata
```

## Telegram Alerts

1. Створити бота: `@BotFather → /newbot → course-supporter-alerts`
2. Отримати chat_id: `GET https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Заповнити `deploy/netdata/health_alarm_notify.conf`
4. Скопіювати в контейнер (див. вище)
5. Тест: `docker exec netdata /usr/libexec/netdata/plugins.d/alarm-notify.sh test`

## Що моніторимо

| Метрика | Default Alert | Custom |
|---|---|---|
| Disk space | Yes (< 20%) | > 80% warn, > 90% crit |
| RAM usage | Yes | > 85% warn, > 95% crit |
| CPU | Yes (sustained) | Default |
| Docker containers | Restart detection | Default |
| Network I/O | Yes | Default |

## Документація

Створено `current-doc/S2-prod-deploy/infrastructure/`:

- `README.md` — загальна архітектура інфраструктури
- `deployment-guide.md` — покрокова інструкція деплою
- `netdata-setup.md` — покрокова інструкція налаштування Netdata та Telegram alerts

## Definition of Done

- [x] Netdata контейнер в docker-compose.prod.yaml
- [x] Dashboard доступний через nginx з basic auth (`/netdata/`)
- [x] Telegram alert config templates (`deploy/netdata/`)
- [x] Custom alert thresholds (disk, RAM)
- [x] Infrastructure documentation створено
- [x] Документ оновлений відповідно до фінальної реалізації
