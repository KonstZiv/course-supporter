# VD-SPIKE-B: Vision LLM — описи, OCR, combined prompt

**Фаза:** 2 — Spike
**Пріоритет:** Критичний (визначає архітектуру Stage B і потребу Stage C)
**Залежності:** VD-SPIKE-A (golden frames)

## Що робимо

Порівнюємо Vision LLM моделі для: (1) описів сцен, (2) витягування тексту/коду (Vision LLM як OCR), (3) combined prompt. Визначаємо чи потрібен окремий OCR engine (Stage C).

## Яким чином

### Підготовка

- Обрати 20-30 representative frames з golden frames (Spike A):
  - 5 слайдів (текст, діаграма, код на слайді)
  - 5 кадрів live coding (dark IDE)
  - 5 terminal output
  - 5 переходів (між слайдами / IDE)
  - 5 talking head (контрольна група)
- Вручну підготувати **ground truth** для 10 кадрів (5 IDE + 5 slides): точний текст/код з екрану

### 7 тестів

| # | Тест | Що порівнюємо |
|---|---|---|
| 1 | **Описи (batch)** | Якість описів: Gemini Flash, Pro, GPT-4o, Claude Sonnet |
| 2 | **Vision LLM як OCR** | Code/text extraction accuracy vs ground truth |
| 3 | **Combined prompt** | "describe AND extract" vs окремі запити — якість, cost |
| 4 | **STT context** | З контекстом транскрипту vs без — якість описів |
| 5 | **Two-pass** | Pass 1 classify → filter → Pass 2 detailed — cost savings |
| 6 | **Crop strategy** | Full frame vs cropped region — code accuracy |
| 7 | **Batch size** | 10 vs 20 vs 30 frames per batch — якість vs cost |

### Моделі

- Gemini 2.5 Flash (primary candidate — дешевий, 1M+ context)
- Gemini 2.5 Pro (quality fallback)
- GPT-4o (alternative)
- Claude Sonnet (alternative)

## Результат

- Скрипт `scripts/spike_vision_llm.py`
- Ground truth файли у `current-doc/vd-spike/VD-SPIKE-B/ground-truth/`
- Звіт `current-doc/vd-spike/VD-SPIKE-B/RESULTS.md`:
  - Таблиця: provider × scene_type → якість (1-5), code accuracy (%), latency, cost
  - Таблиця: combined vs separate → якість, cost
  - Таблиця: with STT context vs without → якість
  - Таблиця: two-pass cost vs one-pass cost
- **КЛЮЧОВЕ РІШЕННЯ:** Vision LLM code accuracy:
  - ≥90% → Stage C НЕ потрібен
  - 70-90% → Stage C як fallback
  - <70% → Stage C обов'язковий
- Рекомендація: primary → fallback chain

## Як перевіряємо

- Code accuracy обчислюється автоматично (character-level diff з ground truth)
- Описи оцінюються вручну (1-5 бал за інформативність)
- Cost рахується по token usage з API responses
- Talking head правильно ігнорується (importance 1-2)
- Combined prompt не гірший за окремі (якість) і дешевший (cost)
