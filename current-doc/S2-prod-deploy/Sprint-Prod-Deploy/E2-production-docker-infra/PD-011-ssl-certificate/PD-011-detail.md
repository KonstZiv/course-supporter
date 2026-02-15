# PD-011: SSL Certificate — Detail

## Контекст

Certbot вже працює для `pythoncourse.me` з webroot challenge. Потрібно додати сертифікат для `api.pythoncourse.me`.

## Порядок дій

### 1. Переконатись що DNS propagated

```bash
dig api.pythoncourse.me +short
# Повинен повернути IP VPS
```

### 2. Тимчасовий HTTP server block

Перед видачею сертифікату потрібен HTTP server block з ACME challenge location (див. PD-010 — HTTP server block).

### 3. Запросити сертифікат

```bash
docker compose exec certbot certbot certonly \
    --webroot \
    -w /var/www/html \
    -d api.pythoncourse.me \
    --non-interactive \
    --agree-tos \
    --email admin@pythoncourse.me
```

### 4. Увімкнути HTTPS server block

Після отримання сертифікату — увімкнути повний HTTPS config з PD-010.

```bash
docker compose exec nginx nginx -s reload
```

### 5. Перевірити автопоновлення

Certbot renewal вже налаштований (cron або systemd timer). Перевірити:

```bash
certbot renew --dry-run
```

Новий домен автоматично включений в renewal.

## Тестування

```bash
# Перевірка сертифікату:
curl -vI https://api.pythoncourse.me/health 2>&1 | grep "SSL certificate"

# Перевірка redirect:
curl -I http://api.pythoncourse.me/health
# Повинен бути 301 → https://
```

## Definition of Done

- [ ] Сертифікат видано та встановлено
- [ ] HTTPS працює
- [ ] HTTP → HTTPS redirect
- [ ] Автопоновлення включає новий домен
- [ ] Документ оновлений відповідно до фінальної реалізації
