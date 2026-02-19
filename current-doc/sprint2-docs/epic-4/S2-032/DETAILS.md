# S2-032: Extract whisper transcription — Деталі для виконавця

**Epic:** EPIC-4 — Heavy Steps Extraction
**Оцінка:** 3h

---

## Мета

Whisper transcription — окрема injectable function

## Контекст

Ця задача є частиною Epic "Heavy Steps Extraction" (2-3 дні).
Загальна ціль epic: Injectable heavy operations, serverless-ready boundary.

## Залежності

**Попередня задача:** [S2-031: Heavy step protocols + param/result models](../S2-031/README.md)

**Наступна задача:** [S2-033: Extract vision/slide description](../S2-033/README.md)



---

## Детальний план реалізації

1. Створити local_transcribe(audio_url, params) → Transcript
2. Функція: download audio → whisper → structured result
3. Нічого про DB, S3 storage, ORM

---

## Очікуваний результат

local_transcribe працює автономно, можна замінити на lambda_transcribe

---

## Тестування

### Автоматизовані тести

Unit test з mock whisper: перевірити input/output contract

### Ручний контроль (Human testing)

Запустити ingestion відео → результат ідентичний до рефакторингу

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
