# S2-015: MaterialState derived property

**Epic:** EPIC-2 — MaterialTree + MaterialEntry
**Оцінка:** 1h

---

## Мета

Стан матеріалу визначається автоматично з полів entry

## Що робимо

Реалізувати MaterialState enum і state property на MaterialEntry

## Як робимо

1. MaterialState(StrEnum): RAW, PENDING, READY, INTEGRITY_BROKEN, ERROR
2. @property state з логікою пріоритетів: ERROR > PENDING > RAW > INTEGRITY_BROKEN > READY

## Очікуваний результат

entry.state правильно відображає поточний стан

## Як тестуємо

**Автоматизовано:** Unit tests: всі 5 станів, перехід між станами, edge cases (error + pending одночасно)

**Human control:** Немає (повністю покривається unit tests)

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
