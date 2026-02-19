# S2-052: Free vs Guided mode

**Epic:** EPIC-6 — Structure Generation Pipeline
**Оцінка:** 3h

---

## Мета

Два режими генерації з різними промптами

## Що робимо

Параметр mode в generate endpoint, різні prompt templates

## Як робимо

1. mode='free': prompt дозволяє вільну структуру
2. mode='guided': prompt містить input tree як constraint
3. Зберігається в snapshot.mode
4. Idempotency per (fingerprint + mode)

## Очікуваний результат

Free і guided генерують різні структури з одних матеріалів

## Як тестуємо

**Автоматизовано:** Unit test: різні prompts для різних modes, idempotency per mode

**Human control:** Згенерувати free і guided для одного курсу → порівняти результати — guided зберігає input structure

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
