# Phase 9: Cascading Job Failure

**Складність:** M (Medium)
**Залежності:** Phase 1 (S3-004 — Job cleanup)
**Задачі:** S3-019
**PR:** 1 PR

## Мета

При failure Job → fail всіх залежних (depends_on) рекурсивно. Зараз залежні jobs чекають нескінченно.

## Контекст

`depends_on` — JSONB list[str] Job UUIDs. Якщо dependency fails, dependent jobs не отримують нотифікацію. Стає критичним з recursive multi-pass generation (Phase 10).

## Критерії завершення

- [ ] При failure Job → всі залежні fail рекурсивно
- [ ] `error_message: "Dependency {job_id} failed"`
- [ ] Тести покривають multi-level propagation
