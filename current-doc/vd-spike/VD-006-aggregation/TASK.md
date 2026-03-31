# VD-006: Aggregation Layer

**Фаза:** 4 — Implementation
**Пріоритет:** High
**Залежності:** VD-001

## Що робимо

Створюємо модуль агрегації, який об'єднує результати з різних stages (STT transcript, VD analysis, optional OCR) в єдину timeline з cross-references та дедуплікацією.

## Яким чином

Створити `src/course_supporter/vd/aggregation.py`:

1. **Merge by timestamps:**
   - Прийняти STT transcript (list[STTSegment]), VD analysis (list[VisualAnalysis]), optional OCR (list[OCRExtraction])
   - Об'єднати всі chunks в єдину timeline, відсортовану за timestamp

2. **Cross-reference (±5 sec window):**
   - Для кожного VD chunk знайти відповідний STT chunk з найближчим timestamp (вікно ±5 секунд)
   - Зв'язати їх для downstream processing (агент буде знати що лектор казав в момент показу коду)
   - Додати reference ID між пов'язаними chunks

3. **Deduplication:**
   - Якщо Stage B (Visual Analysis) та Stage C (OCR) дають однаковий текст для одного кадру — залишити варіант з вищим confidence
   - Fuzzy matching для порівняння (не exact match, бо OCR та Vision LLM можуть мати дрібні відмінності)

4. **Priority ordering для same-timestamp chunks:**
   - `VISUAL_DESCRIPTION` > `CODE_BLOCK` > `TRANSCRIPT`
   - При однаковому timestamp — важливіший тип йде першим

5. **Повернути `list[ContentChunk]`:**
   - Використовує існуючий `ContentChunk` з `models/source.py`
   - Кожен chunk має `chunk_type`, `text`, `start_time`, `end_time`, metadata

## Результат

- Файл `src/course_supporter/vd/aggregation.py`
- Функція агрегації що приймає результати STT + VD + OCR
- Cross-reference з ±5 sec window
- Deduplication з fuzzy matching
- Priority ordering: VISUAL_DESCRIPTION > CODE_BLOCK > TRANSCRIPT
- Повертає `list[ContentChunk]`

## Як перевіряємо

```bash
uv run mypy src/course_supporter/vd/aggregation.py    # strict, no errors
uv run ruff check src/course_supporter/vd/             # no lint errors
uv run pytest tests/unit/test_vd_aggregation.py -v     # unit tests (після VD-015)
```
