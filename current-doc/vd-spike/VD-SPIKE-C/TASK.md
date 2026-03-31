# VD-SPIKE-C: OCR Accuracy (УМОВНИЙ)

**Фаза:** 2.5 — Spike
**Пріоритет:** Середній
**Залежності:** VD-SPIKE-B (тільки якщо Vision LLM code accuracy < 90%)
**УВАГА:** Ця таска виконується ТІЛЬКИ за результатами Spike B

## Що робимо

Порівнюємо OCR engines для витягування коду з скріншотів IDE (dark theme) та тексту зі слайдів. Виконується лише якщо Vision LLM не дав достатньої точності для Python коду.

## Яким чином

1. Використовуємо ті ж 10 кадрів з ground truth що підготовані в Spike B
2. Тестуємо OCR engines:
   - **Surya OCR** — SOTA для code recognition
   - **PaddleOCR** — зрілий, multilingual
   - **Tesseract** (через pytesseract) — безкоштовний baseline
   - **Google Vision API** (через httpx) — cloud quality
3. Для кожного engine: raw output → character accuracy vs ground truth
4. LLM post-processing: raw OCR → DeepSeek/GPT-4o Mini correction → accuracy
5. Pre-processing тест: invert colors для dark IDE themes → accuracy
6. Порівняння: OCR + LLM correction vs Vision LLM (Spike B results)

## Результат

- Скрипт `scripts/spike_ocr.py`
- Звіт `current-doc/vd-spike/VD-SPIKE-C/RESULTS.md`:
  - Таблиця: engine × content_type → character accuracy (%), code accuracy (%)
  - Таблиця: raw OCR vs LLM-corrected vs Vision LLM
  - Pre-processing (invert) вплив на accuracy
- Рекомендація: який OCR engine для production

## Як перевіряємо

- Code accuracy > 90% після LLM correction
- Slide text accuracy > 95%
- Python indentation збережений
- `__init__`, `->`, `_` коректно розпізнаються
- Порівняння з Vision LLM — OCR має бути суттєво кращим щоб виправдати додатковий stage
