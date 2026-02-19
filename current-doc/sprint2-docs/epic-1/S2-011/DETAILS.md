# S2-011: Health check — додати Redis — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 1h

---

## Мета

/health перевіряє Redis connectivity

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Попередня задача:** [S2-010: Job status API endpoint](../S2-010/README.md)

**Наступна задача:** [S2-012: Worker integration tests](../S2-012/README.md)



---

## Детальний план реалізації

1. Додати Redis ping в health check
2. Response: { db: ok, s3: ok, redis: ok }
3. Якщо Redis недоступний — HTTP 503 з деталями

---

## Очікуваний результат

/health повертає статус DB + S3 + Redis

---

## Тестування

### Автоматизовані тести

Unit test: health з mock Redis (ok і fail scenarios)

### Ручний контроль (Human testing)

Зупинити Redis → GET /health → 503 з redis: error. Запустити → 200 з redis: ok

---

## Checklist перед PR

- [ ] Реалізація відповідає архітектурним рішенням Sprint 2 (AR-*)
- [ ] Код проходить `make check` (ruff + mypy + pytest)
- [ ] Unit tests написані і покривають основні сценарії
- [ ] Edge cases покриті (error handling, boundary values)
- [ ] Error messages зрозумілі і містять hints для користувача
- [ ] Human control points пройдені
- [ ] Документація оновлена якщо потрібно (ERD, API docs, sprint progress)
- [ ] Перевірено чи зміни впливають на наступні задачі — якщо так, оновити їх docs

---

## Нотатки

_Простір для нотаток виконавця під час роботи над задачею._
