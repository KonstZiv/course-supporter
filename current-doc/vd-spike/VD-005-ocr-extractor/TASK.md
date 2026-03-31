# VD-005: OCR Extractor (умовний)

**Фаза:** 4 — Implementation
**Пріоритет:** Medium
**Залежності:** VD-001, VD-SPIKE-C

## Що робимо

**УМОВНИЙ ТАСК** — реалізується тільки якщо Spike B покаже, що Vision LLM дає точність розпізнавання коду < 90%. Якщо Vision LLM достатньо точний — Stage C пропускається повністю.

Створюємо модуль для OCR витягування тексту/коду з кадрів за допомогою спеціалізованого OCR engine + LLM post-processing для корекції помилок.

## Яким чином

Створити `src/course_supporter/vd/ocr_extractor.py`:

1. **OCR engine:**
   - Конкретний engine визначається результатами Spike C: Surya, PaddleOCR або Tesseract
   - Інтерфейс абстрагований для можливості заміни engine

2. **Pre-processing:**
   - Інвертувати кольори для темних тем IDE (dark theme → light для кращого OCR)
   - Збільшити contrast при потребі

3. **LLM post-processing для корекції:**
   - Через `ModelRouter` з action `ocr_correction`
   - Модель: DeepSeek Chat → fallback GPT-4o Mini
   - Prompt: виправити OCR помилки в коді (пробіли, відступи, спеціальні символи)
   - Особливо важливо для: `_` vs `-`, `l` vs `1` vs `I`, відступів Python коду

4. **Результат:** `list[OCRExtraction]` для кожного кадру з `raw_text` та `corrected_text`

## Результат

- Файл `src/course_supporter/vd/ocr_extractor.py` (або skip якщо Spike B > 90%)
- OCR engine (визначений Spike C) + LLM correction
- Pre-processing для dark IDE themes
- Використовує `ModelRouter` з action `ocr_correction`

## Як перевіряємо

```bash
uv run mypy src/course_supporter/vd/ocr_extractor.py   # strict, no errors
uv run ruff check src/course_supporter/vd/              # no lint errors
# Manual: витягти код з кадру IDE, порівняти з оригіналом — точність > 95%
```
