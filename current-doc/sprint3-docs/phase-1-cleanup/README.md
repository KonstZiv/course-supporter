# Phase 1: Незалежний cleanup

**Складність:** 5 x S (паралельно)
**Залежності:** Немає (паралельно з Phase 0)
**Задачі:** S3-003, S3-004, S3-005, S3-006, S3-007
**PRs:** До 5 незалежних PRs

## Мета

Прибрати технічний борг та дублювання перед великим рефакторингом:
- Видалити зайві поля з ORM (content_fingerprint, Job result FKs)
- Створити config registry для auth scopes та platform allowlist
- Завершити незавершений DI extraction (ParsePDFFunc)

## Паралелізм

Всі 5 задач незалежні одна від одної і можуть виконуватись паралельно. A6 та A7 потребують Alembic migrations — деплоїти разом в одне migration window.

## Критерії завершення

- [ ] `content_fingerprint` видалений з MaterialEntry
- [ ] `result_material_id` / `result_snapshot_id` видалені з Job
- [ ] Auth scopes завантажуються з `config/auth.yaml`
- [ ] Platform allowlist для web sources працює
- [ ] `ParsePDFFunc` extracted through DI (як інші heavy steps)
- [ ] CI green для кожного PR
