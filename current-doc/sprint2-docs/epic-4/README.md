# EPIC-4: Heavy Steps Extraction

**Оцінка:** 2-3 дні
**Ціль:** Injectable heavy operations, serverless-ready boundary.

---

## Задачі

- [S2-031: Heavy step protocols + param/result models](./S2-031/README.md) (2h)
- [S2-032: Extract whisper transcription](./S2-032/README.md) (3h)
- [S2-033: Extract vision/slide description](./S2-033/README.md) (3h)
- [S2-034: Extract web scraping](./S2-034/README.md) (2h)
- [S2-035: Refactor processors as orchestrators](./S2-035/README.md) (4h)
- [S2-036: Factory for heavy steps](./S2-036/README.md) (2h)
- [S2-037: Heavy steps unit tests](./S2-037/README.md) (3h)

---

## Автоматизований контроль результатів Epic

Unit tests з mock heavy steps: processor отримує injectable function, викликає її, пакує результат в SourceDocument. Tests перевіряють що processor не знає implementation details.

---

## Ручний контроль результатів Epic (Human testing)

1. Запустити ingestion відео → перевірити що whisper працює як раніше (функціональність не зламана)
2. Запустити ingestion презентації → vision працює
3. Code review: перевірити що heavy steps мають чистий contract (bytes/url in → structured data out)

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
