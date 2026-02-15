# PD-011: SSL Certificate

## Що

Let's Encrypt сертифікат для `api.pythoncourse.me` через існуючий certbot.

## Навіщо

HTTPS обов'язковий для production API з API key auth. Certbot вже налаштований для основного домену.

## Ключові рішення

- certbot з webroot challenge (вже працює для pythoncourse.me)
- Автопоновлення через існуючий cron/systemd timer

## Acceptance Criteria

- [ ] `https://api.pythoncourse.me` відповідає з валідним сертифікатом
- [ ] Автопоновлення налаштовано
- [ ] HTTP → HTTPS redirect працює
