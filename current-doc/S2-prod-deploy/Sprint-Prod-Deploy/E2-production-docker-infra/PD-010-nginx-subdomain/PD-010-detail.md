# PD-010: Nginx Config для subdomain — Detail

## Контекст

Існуючий nginx обслуговує `pythoncourse.me` (Django) та проксює `webinar-bot` через `shared-net`. Додаємо `api.pythoncourse.me` для Course Supporter.

## Зміни в nginx.conf

Додати до існуючого конфігу (не замінювати):

```nginx
upstream course_supporter {
    server course-supporter-app:8000;
}

upstream netdata_backend {
    server netdata:19999;
}

# --- Course Supporter API ---

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

server {
    listen 443 ssl;
    server_name api.pythoncourse.me;

    ssl_certificate /etc/letsencrypt/live/api.pythoncourse.me/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.pythoncourse.me/privkey.pem;

    # Upload limits for video files (up to 1GB)
    client_max_body_size 1G;

    # Timeouts for large uploads (1GB @ 10Mbps ≈ 14 min)
    client_body_timeout 900s;
    proxy_read_timeout 900s;
    proxy_send_timeout 900s;

    # Stream uploads directly to upstream (no disk buffering)
    proxy_request_buffering off;

    location / {
        proxy_pass http://course_supporter;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Security headers
        proxy_hide_header X-Powered-By;
        add_header X-Content-Type-Options nosniff always;
        add_header X-Frame-Options DENY always;
        add_header X-XSS-Protection "1; mode=block" always;
    }

    # Netdata dashboard (basic auth)
    location /netdata/ {
        auth_basic "Monitoring";
        auth_basic_user_file /etc/nginx/.htpasswd_netdata;
        proxy_pass http://netdata_backend/;
        proxy_set_header Host $host;
    }
}
```

## DNS

Додати A-record до deploy:
```
api.pythoncourse.me. A <VPS_IP>
```

## Basic Auth для Netdata

```bash
# На VPS (всередині nginx container або volume):
apt-get install -y apache2-utils
htpasswd -c /etc/nginx/.htpasswd_netdata admin
```

Або volume mount з підготовленим файлом.

## Порядок deploy

1. Додати DNS A-record для `api.pythoncourse.me`
2. Зачекати propagation (5-15 хв)
3. Тимчасово: server block тільки з HTTP (port 80) для certbot challenge
4. Запустити certbot (PD-011)
5. Увімкнути повний config з HTTPS

## Тестування

```bash
# Перевірка що nginx config валідний:
docker compose exec nginx nginx -t

# Після deploy:
curl -I https://api.pythoncourse.me/health
# Перевірити headers: X-Content-Type-Options, X-Frame-Options
```

## Definition of Done

- [ ] Nginx config доданий, `nginx -t` проходить
- [ ] `api.pythoncourse.me` проксює до app
- [ ] Security headers present
- [ ] Timeouts та body size для 1GB
- [ ] Netdata проксюється з basic auth
- [ ] `pythoncourse.me` продовжує працювати
- [ ] Документ оновлений відповідно до фінальної реалізації
