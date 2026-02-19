# S2-054: MergeStep refactor — tree-aware

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 3h

---

## Мета

Merge враховує ієрархію MaterialNode при формуванні context

## Що робимо

MergeStep отримує tree structure, передає як context для ArchitectAgent

## Як робимо

1. MergeStep.merge(tree: MaterialNode) замість flat list
2. CourseContext включає tree structure (node titles, nesting)
3. В guided mode: tree structure = constraint для agent

## Очікуваний результат

ArchitectAgent отримує повний context про структуру дерева

## Як тестуємо

**Автоматизовано:** Unit test: MergeStep з nested tree → CourseContext містить hierarchy

**Human control:** Перевірити що generated structure відображає input tree hierarchy (guided mode)

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
