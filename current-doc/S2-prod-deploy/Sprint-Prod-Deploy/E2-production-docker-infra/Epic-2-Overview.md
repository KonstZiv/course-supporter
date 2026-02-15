# Epic 2: Production Docker & Infrastructure

## Мета

Розгорнути Course Supporter API на VPS: Dockerfile, production compose, nginx routing через `shared-net`, Backblaze B2 storage, streaming upload для великих файлів (до 1GB), health checks, Netdata monitoring.

## Інфраструктурний контекст

- VPS: Aviti (Україна), 3.82 GB RAM, Ubuntu
- Існуючий стек: Django + nginx + certbot на `pythoncourse.me`, Docker
- Nginx маршрутизує через `shared-net` (вже є паттерн з webinar-bot)
- Subdomain: `api.pythoncourse.me`

## Задачі

| ID | Назва | Залежності |
| :---- | :---- | :---- |
| PD-008 | Dockerfile (multi-stage) | — |
| PD-009 | docker-compose.prod.yaml | PD-008 |
| PD-010 | Nginx config для subdomain | PD-009 |
| PD-011 | SSL certificate | PD-010 |
| PD-012 | Backblaze B2 integration | — |
| PD-013 | Streaming upload (1GB) | PD-012 |
| PD-014 | Deep health check | PD-009 |
| PD-015 | Monitoring (Netdata) | PD-009 |

## Результат

- `api.pythoncourse.me` відповідає через HTTPS
- Upload файлів до 1GB без OOM (streaming → S3 multipart)
- `/health` перевіряє DB + S3
- Netdata dashboard з alerts в Telegram
