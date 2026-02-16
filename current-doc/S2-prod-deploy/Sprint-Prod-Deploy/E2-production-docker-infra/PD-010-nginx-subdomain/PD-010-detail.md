# PD-010: Nginx Config для subdomain — Detail

## Контекст

Існуючий nginx обслуговує `pythoncourse.me` (Django) та проксює `webinar-bot` через `shared-net`. Додаємо `api.pythoncourse.me` для Course Supporter.

Reference config: `deploy/nginx/course-supporter.conf` — snippet для додавання в існуючий `nginx.conf` на VPS.

## Nginx config snippet

Додати до існуючого `nginx.conf` всередині `http { }` блоку (не замінювати існуючі server blocks):

```nginx
# --- Course Supporter API (HTTP → HTTPS redirect + ACME challenge) ---

server {
    listen 80;
    server_name api.pythoncourse.me;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# --- Course Supporter API (HTTPS) ---

server {
    listen 443 ssl;
    server_name api.pythoncourse.me;

    ssl_certificate /etc/letsencrypt/live/api.pythoncourse.me/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.pythoncourse.me/privkey.pem;

    # TLS hardening
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384';
    ssl_prefer_server_ciphers on;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 1h;
    ssl_session_tickets off;
    ssl_stapling on;
    ssl_stapling_verify on;

    client_max_body_size 1G;
    client_body_timeout 900s;
    proxy_read_timeout 900s;
    proxy_send_timeout 900s;
    proxy_request_buffering off;

    # Docker DNS — resolve at request time, not at startup
    # Allows nginx to start even if course-supporter-app is not running yet
    resolver 127.0.0.11 valid=30s;
    set $course_supporter http://course-supporter-app:8000;

    location / {
        proxy_pass $course_supporter;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Security headers
        proxy_hide_header X-Powered-By;
        add_header X-Content-Type-Options nosniff always;
        add_header X-Frame-Options DENY always;
        add_header X-XSS-Protection "1; mode=block" always;
        add_header Strict-Transport-Security "max-age=31536000; includeSubDomains; preload" always;
    }
}
```

### Ключове рішення: resolver замість upstream

Стандартний `upstream` блок резолвить hostname при старті nginx. Якщо `course-supporter-app` ще не запущений — nginx відмовиться стартувати (`host not found in upstream`).

Рішення: `resolver 127.0.0.11` (Docker DNS) + `set $variable` — nginx резолвить hostname при кожному запиті. Nginx стартує нормально, повертає 502 поки app не з'явиться в `shared-net`.

## Покрокова інструкція deploy на VPS

### Крок 1: DNS CNAME record

Додати CNAME record у DNS-панелі реєстратора:

```
api.pythoncourse.me. CNAME pythoncourse.me.
```

CNAME замість A-record: якщо IP VPS зміниться — оновлюється тільки A-record на `pythoncourse.me`, subdomain підтягнеться автоматично.

Перевірити propagation:

```bash
dig api.pythoncourse.me +short
# Повинен повернути VPS IP (через CNAME → A chain)
```

### Крок 2: HTTP server block + certbot

Додати в `nginx.conf` тільки HTTP server block (port 80) для `api.pythoncourse.me` з ACME challenge location. Потрібен щоб certbot міг отримати сертифікат.

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yaml exec nginx nginx -t
docker compose --env-file .env.prod -f docker-compose.prod.yaml exec nginx nginx -s reload
```

Запустити certbot (використовує вже існуючі shared volumes `certbot-etc` + `./certbot-www`):

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yaml run --rm certbot certonly \
    --webroot -w /var/www/html \
    -d api.pythoncourse.me \
    --agree-tos --non-interactive
```

### Крок 3: Повний конфіг з HTTPS

Додати HTTPS server block з `resolver` + security headers + upload timeouts (повний snippet вище).

```bash
docker compose --env-file .env.prod -f docker-compose.prod.yaml exec nginx nginx -t
docker compose --env-file .env.prod -f docker-compose.prod.yaml exec nginx nginx -s reload
```

## Тестування

```bash
# Перевірити що Django продовжує працювати:
curl -I https://pythoncourse.me

# api.pythoncourse.me — поверне 502 поки course-supporter-app не запущений:
curl -I https://api.pythoncourse.me/health

# Після запуску course-supporter-app — перевірити headers:
#   X-Content-Type-Options: nosniff
#   X-Frame-Options: DENY
#   X-XSS-Protection: 1; mode=block
```

## Конфігураційні деталі

| Параметр | Значення | Чому |
|----------|----------|------|
| `client_max_body_size` | 1G | Upload відео до 1GB |
| `client_body_timeout` | 900s | 1GB @ 10Mbps ≈ 14 хв |
| `proxy_read_timeout` | 900s | LLM processing може бути тривалим |
| `proxy_send_timeout` | 900s | Великі відповіді |
| `proxy_request_buffering` | off | Стрім напряму в upstream, без буферизації на диск |
| `resolver 127.0.0.11` | Docker DNS | Резолв при запиті, не при старті nginx |
| container name | `course-supporter-app:8000` | Відповідає `container_name` в `docker-compose.prod.yaml` |

## Definition of Done

- [x] Nginx config створений (`deploy/nginx/course-supporter.conf`)
- [x] DNS CNAME: `api.pythoncourse.me` → `pythoncourse.me`
- [x] SSL certificate отримано (certbot, expires 2026-05-17)
- [x] `nginx -t` проходить
- [x] `pythoncourse.me` продовжує працювати
- [x] Timeouts та body size для 1GB
- [x] Security headers (X-Content-Type-Options, X-Frame-Options, X-XSS-Protection)
- [ ] `api.pythoncourse.me` проксює до app (верифікація після запуску course-supporter-app)
- [x] Документ оновлений відповідно до фінальної реалізації
