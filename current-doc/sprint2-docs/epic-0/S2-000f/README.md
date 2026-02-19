# S2-000f: Структура документації + landing

**Epic:** EPIC-0 — Project Documentation Infrastructure
**Оцінка:** 2h

---

## Мета

Повна навігаційна структура docs site

## Що робимо

Створити всі розділи: Overview, Architecture, Sprints, API, Development

## Як робимо

1. Створити docs/index.md (landing — overview проєкту)
2. Створити placeholder-и для всіх розділів
3. Налаштувати nav в mkdocs.yml
4. Додати architecture/decisions.md (ADR format)

## Очікуваний результат

Docs site має повну навігаційну структуру, placeholder-и для майбутнього контенту

## Як тестуємо

**Автоматизовано:** mkdocs build --strict (перевірка broken links)

**Human control:** Пройти по всій навігації — структура логічна, placeholder-и зрозумілі

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
