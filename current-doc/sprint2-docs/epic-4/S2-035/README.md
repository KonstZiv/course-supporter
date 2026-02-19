# S2-035: Refactor processors as orchestrators

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 4h

---

## Мета

Processors приймають heavy steps через DI, не знають implementation

## Що робимо

Рефакторинг VideoProcessor, PresentationProcessor, WebProcessor

## Як робимо

1. Processor.__init__(heavy_step_func) — injectable
2. Processor.process(): prepare input → call heavy step → package SourceDocument
3. Processor не імпортує whisper/vision напряму

## Очікуваний результат

Processors — тонкі оркестратори, heavy logic injectable

## Як тестуємо

**Автоматизовано:** Unit tests: processor з mock heavy step → правильний SourceDocument

**Human control:** Code review: processors не мають прямих imports heavy libraries

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
