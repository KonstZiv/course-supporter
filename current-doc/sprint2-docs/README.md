# Sprint 2 — Контроль результатів

**Sprint:** Material Tree, Task Queue, Structure Generation
**Оцінка:** 4-5 тижнів

---

## Епіки

- [EPIC-0: Project Documentation Infrastructure](./epic-0/README.md) (1-2 дні)
- [EPIC-1: Infrastructure — ARQ + Redis](./epic-1/README.md) (4-5 днів)
- [EPIC-2: MaterialTree + MaterialEntry](./epic-2/README.md) (4-5 днів)
- [EPIC-3: Merkle Fingerprints](./epic-3/README.md) (2-3 дні)
- [EPIC-4: Heavy Steps Extraction](./epic-4/README.md) (2-3 дні)
- [EPIC-5: SlideVideoMapping — Redesign](./epic-5/README.md) (3-4 дні)
- [EPIC-6: Structure Generation Pipeline](./epic-6/README.md) (3-4 дні)
- [EPIC-7: Integration Documentation](./epic-7/README.md) (1-2 дні)

---

## Автоматизований контроль

### Unit tests
- `make check` (ruff + mypy + pytest) — зелений на кожному PR
- Coverage > 90% для нових модулів
- Кожен epic має свій набір unit tests (остання задача в epic)

### Integration tests
- Job lifecycle: queued → active → complete/failed (з реальним Redis)
- Ingestion callback: entry update → fingerprint invalidation → mapping revalidation
- Generation pipeline: upload → ingestion → generation → snapshot
- Cascade orchestration: stale materials → auto-ingestion → depends_on → generation

### CI pipeline
- GitHub Actions: lint + typecheck + tests на кожен PR
- mkdocs build --strict (перевірка документації)
- Migration test: upgrade → verify → downgrade

---

## Ручний контроль (Human testing)

### Перед кожним merge
- Code review: чистота коду, naming conventions, відповідність AR-ам
- Перевірка error messages: чи зрозумілі, чи містять hints

### По завершенню кожного Epic
- Функціональний тест згідно human_test кожного epic (див. epic README)
- Перевірка на staging з копією production даних (для міграцій)

### По завершенню Sprint

### E2E сценарій: повний flow створення курсу

**Передумови:** API запущений, Redis + worker healthy, API key отримано.

1. `POST /courses` → створити курс "Python Basics"
2. `POST /courses/{id}/nodes` → створити root node "Модуль 1"
3. `POST /courses/{id}/nodes/{node_id}/children` → створити child "Тема 1.1"
4. `POST /courses/{id}/nodes/{child_id}/materials` → завантажити video.mp4
5. `POST /courses/{id}/nodes/{child_id}/materials` → завантажити slides.pdf
6. `GET /courses/{id}` → перевірити дерево: 2 nodes, 2 materials в стані PENDING
7. Дочекатись ingestion (polling `GET /jobs/{id}`) → materials стають READY
8. `POST /courses/{id}/nodes/{node_id}/slide-mapping` → batch маппінг
9. `GET /courses/{id}` → fingerprints заповнені на всіх рівнях
10. `POST /courses/{id}/structure/generate` (mode: free) → 202 Accepted
11. Polling `GET /jobs/{job_id}` → complete
12. `GET /courses/{id}/structure` → snapshot з CourseStructure JSON
13. Повторити generate → 200 OK (idempotent, той самий fingerprint)
14. Змінити один матеріал → fingerprints інвалідовані → generate → 202 (новий snapshot)

**Очікуваний результат:** повний flow без помилок, всі стани коректні на кожному кроці.

### E2E сценарій: conflict detection

1. Курс з Node A і Node B (siblings), матеріали READY
2. `POST /nodes/{A}/structure/generate` → 202
3. Негайно `POST /nodes/{A}/structure/generate` → 409 (overlap — той самий node)
4. `POST /nodes/{B}/structure/generate` → 202 (незалежні — паралельно)
5. `POST /courses/{id}/structure/generate` → 409 (course-level overlap з Node A)

### E2E сценарій: deferred mapping validation

1. Створити node з відео (READY) і презентацією (PENDING)
2. Batch upload маппінгів → partial: validated (timecode ok) + pending_validation (slide check blocked)
3. Ingestion презентації завершується → auto-revalidation → mapping стає validated
4. Або: ingestion fails → blocking_factor оновлюється до material_error

---

## Обов'язковий пункт після кожного завершеного Epic

> ⚠️ **Після завершення кожного epic (або групи задач що вносять зміни):**
>
> 1. **Оновити ERD** — якщо змінились моделі (docs/architecture/erd.md)
> 2. **Оновити docs site** — sprint progress, нові endpoints, змінені flow-и
> 3. **Ревізія майбутніх задач** — чи не вплинули зміни на scope/підхід наступних epic-ів
> 4. **Оновити Sprint 2 план** — відмітити completed, скоригувати estimates якщо потрібно
>
> Це НЕ опціонально. Кожен PR що закриває epic повинен включати documentation update.

---

## Definition of Done (Sprint level)

- [ ] Всі E2E сценарії проходять на staging
- [ ] Docs site актуальний (ERD, sprint progress, API reference)
- [ ] `make check` зелений
- [ ] Alembic міграції працюють (upgrade + downgrade)
- [ ] Tenant isolation перевірена для всіх нових endpoints
- [ ] Зовнішня команда може пройти flow guide без додаткових питань
