# S2-051: Cascade generation orchestrator

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 4h

---

## Мета

Автоматично запускає ingestion для stale materials перед generation

## Що робимо

generate_for_subtree(): знаходить stale → enqueue ingestion → depends_on → enqueue generation

## Як робимо

1. find_stale_materials(node_id)
2. Якщо є stale: enqueue ingestion jobs для кожного
3. enqueue structure generation з depends_on = ingestion job ids
4. Якщо всі READY: check fingerprint → idempotency або enqueue generation
5. Return 200 (existing) або 202 (new jobs) з планом і estimate

## Очікуваний результат

Один POST trigger каскадно обробляє все піддерево

## Як тестуємо

**Автоматизовано:** Integration test: tree з mix READY/RAW → ingestion jobs + generation job created з depends_on

**Human control:** POST generate для node з RAW матеріалами → перевірити response plan: ingestion_required list, estimate

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
