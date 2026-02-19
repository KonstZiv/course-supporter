# EPIC-1: Infrastructure — ARQ + Redis

**Оцінка:** 4-5 днів
**Ціль:** Task queue з persistence, concurrency control, work window, job tracking, estimates.

---

## Задачі

- [S2-001: Redis в docker-compose (dev + prod)](./S2-001/README.md) (2h)
- [S2-002: ARQ worker setup + Settings](./S2-002/README.md) (4h)
- [S2-003: Worker config через env](./S2-003/README.md) (2h)
- [S2-004: Work Window service](./S2-004/README.md) (4h)
- [S2-005: Job priorities (IMMEDIATE/NORMAL)](./S2-005/README.md) (2h)
- [S2-006: Job ORM model + repository](./S2-006/README.md) (3h)
- [S2-007: Queue estimate service](./S2-007/README.md) (4h)
- [S2-008: Замінити BackgroundTasks → ARQ enqueue](./S2-008/README.md) (3h)
- [S2-009: Ingestion completion callback](./S2-009/README.md) (3h)
- [S2-010: Job status API endpoint](./S2-010/README.md) (2h)
- [S2-011: Health check — додати Redis](./S2-011/README.md) (1h)
- [S2-012: Worker integration tests](./S2-012/README.md) (4h)

---

## Автоматизований контроль результатів Epic

Integration tests: job lifecycle (queued→active→complete/failed), retry з backoff, depends_on (job B чекає job A), work window scheduling, estimated_at calculation. Unit tests: WorkWindow service, QueueEstimate service, JobRepository CRUD.

---

## Ручний контроль результатів Epic (Human testing)

1. Запустити docker-compose, перевірити що Redis + worker піднімаються і healthy
2. Завантажити матеріал через API → побачити job в черзі → дочекатись обробки → перевірити результат
3. Завантажити 5+ матеріалів одночасно → перевірити що max_jobs=2 працює (тільки 2 обробляються паралельно)
4. Перевірити estimated_at — чи адекватний час
5. Вимкнути worker → завантажити матеріал → запустити worker → перевірити що job не втрачений
6. Якщо work window enabled — перевірити що heavy job чекає вікно

---

## Обов'язкові дії після завершення Epic

1. **Оновити ERD** на docs site якщо змінились моделі
2. **Оновити Sprint 2 progress** на docs site
3. **Ревізія наступних epic-ів** — чи не вплинули зміни на їх scope
4. **Оновити task documents** для наступних epic-ів якщо з'явились нові залежності або зміни в підході
5. **PR review checklist:**
   - [ ] Код відповідає архітектурним рішенням (AR-*)
   - [ ] Unit tests додані/оновлені
   - [ ] Error messages зрозумілі і містять hints
   - [ ] Документація оновлена
