# S2-050: Generate structure ARQ task

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 4h

---

## Мета

ARQ task що виконує merge → ArchitectAgent → save snapshot

## Що робимо

generate_structure(node_id, mode) як ARQ function

## Як робимо

1. Collect READY materials from subtree
2. MergeStep → CourseContext
3. ArchitectAgent.generate(context, mode) → CourseStructure
4. Save CourseStructureSnapshot з fingerprint
5. Handle errors gracefully

## Очікуваний результат

Generation task створює snapshot і зберігає в DB

## Як тестуємо

**Автоматизовано:** Integration test з mock ArchitectAgent: task → snapshot created з правильним fingerprint

**Human control:** Запустити generation → перевірити snapshot в DB: structure JSON, fingerprint, LLM metadata

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
