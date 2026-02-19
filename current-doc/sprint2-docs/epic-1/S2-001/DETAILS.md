# S2-001: Redis в docker-compose (dev + prod) — Деталі для виконавця

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 2h

---

## Мета

Redis доступний як сервіс в dev і prod оточеннях

## Контекст

Ця задача є частиною Epic "Infrastructure — ARQ + Redis" (4-5 днів).
Загальна ціль epic: Task queue з persistence, concurrency control, work window, job tracking, estimates.

## Залежності

**Наступна задача:** [S2-002: ARQ worker setup + Settings](../S2-002/README.md)



---

## Детальний план реалізації

1. Додати redis service: image redis:7-alpine, appendonly yes, healthcheck (redis-cli ping)
2. Volume redis-data для persistence
3. Network — default (та ж мережа що API і postgres)
4. Додати REDIS_URL в .env.example і settings

---

## Очікуваний результат

`docker-compose up redis` → Redis healthy, `redis-cli ping` → PONG

---

## Тестування

### Автоматизовані тести

Health check в docker-compose (redis-cli ping)

### Ручний контроль (Human testing)

docker-compose up, перевірити logs — Redis started, redis-cli з хост-машини підключається

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
