# Phase 0: Production Blockers

**Складність:** S (Small)
**Залежності:** Немає
**Задачі:** S3-001, S3-002
**PR:** 1 PR, deploy одразу

## Мета

Розблокувати QA testing на production. Два критичних баги виявлені під час мануального тестування Sprint 2 (S2-058):

1. **BUG-004** — S3 download не працює для матеріалів в Backblaze B2
2. **BUG-001** — `GET /nodes/tree` повертає 500 Internal Server Error

## Контекст

Під час QA Sprint 2 (2026-03-03) було завантажено 28 матеріалів (9 videos, 8 presentations, 9 texts, 1 PPTX, 1 long video). З них:
- 8 YouTube videos оброблені успішно (5 Gemini + 3 Whisper fallback)
- 8 presentations (B2) — FAILED через BUG-004
- 9 texts (B2) + 1 PPTX — FAILED через BUG-004
- Tree endpoint — 500 через BUG-001

Обидва баги блокують подальше тестування і мають бути виправлені до початку рефакторингу.

## Критерії завершення

- [ ] B2 матеріали (presentations, texts) успішно обробляються на production
- [ ] `GET /nodes/tree` повертає 200 з коректним JSON деревом
- [ ] Всі існуючі тести проходять
- [ ] Нові тести покривають виправлені баги
