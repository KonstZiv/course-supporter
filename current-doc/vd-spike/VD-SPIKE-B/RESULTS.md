# VD-SPIKE-B: Results — Vision LLM Testing

**Дата:** 2026-03-31
**Відео:** sample2.mp4 — 1280x720, 27.3 min, Python int&float з кодом у консолі та IDE
**Статус:** Частковий — Gemini Flash заблокований (daily quota), GPT-4o повний тест

---

## 1. Test Setup

- **10 ground truth frames** з кодом (5 console + 5 code editor + terminal)
- **20 test frames** (slides, console, code editor, transitions)
- **Prompt:** Combined (describe + extract text/code in one request)
- **Ground truth:** Вручну переписаний код з кожного кадру

## 2. GPT-4o — Combined Prompt Results

### Code Editor Frames (найважливіше для рішення по Stage C)

| Frame | Code Accuracy | Примітка |
|---|---|---|
| golden_060 (simple print) | **99.2%** | Ідеально |
| golden_065 (type check) | **99.5%** | Ідеально |
| golden_075 (traceback) | 69.6% | Autocomplete popup перекриває частину коду |
| golden_080 (two inputs) | **97.2%** | Мінорне: зайвий filename header |
| golden_085 (float error) | **99.5%** | Ідеально, включаючи traceback |
| **Average** | **93.0%** | |

### Slide + Console Frames

| Frame | Overall Accuracy | Примітка |
|---|---|---|
| golden_006 (type()) | 23.4% | Код правильний, але accuracy низький бо slide text "розбавляє" |
| golden_012 (int()) | 14.6% | Те саме — код вірний, slide text домінує |
| golden_017 (float) | 27.0% | Код і числові приклади правильні |
| golden_027 (arithmetic) | 8.8% | Код правильний, slide text домінує у response |
| golden_042 (bool) | 26.5% | Код правильний |

**Примітка:** Низький accuracy на slide frames — артефакт метрики (ground truth містить тільки код, а модель правильно витягує і текст слайда). Фактично модель коректно розпізнає код — підтверджено ручним переглядом raw responses.

### Performance

| Метрика | Значення |
|---|---|
| Total frames | 10 |
| Total input tokens | 12,970 |
| Total output tokens | 3,321 |
| Avg latency per frame | 5.8s |
| Estimated cost per frame | ~$0.005 |
| Estimated cost per 100 frames | ~$0.50 |

### Якість описів (ручна оцінка)

GPT-4o дає **відмінні описи** українською:
- Правильно визначає scene type (console, code_editing, slide)
- Описує що демонструє лектор
- Витягує точний код з правильною indentation
- Зберігає `__init__`, `->`, f-strings
- Включає traceback повністю
- Розрізняє editor code vs terminal output

## 3. Gemini Flash — ЗАБЛОКОВАНИЙ

Всі 4 API ключі вичерпали daily quota (free tier). Тест відкладено.

**План:** Повторити тест на Gemini Flash після reset quota. Очікуємо порівнянну або кращу якість (Gemini Flash — primary candidate через нижчу ціну).

## 4. КЛЮЧОВЕ РІШЕННЯ: Stage C

### Vision LLM Code Accuracy: **93.0%** (GPT-4o, code editor frames)

```
≥90% → Stage C НЕ ПОТРІБЕН  ← МИ ТУТ (93%)
70-90% → Stage C як fallback
<70% → Stage C обов'язковий
```

### Рішення: **Stage C (окремий OCR engine) НЕ ПОТРІБЕН**

GPT-4o дає >90% accuracy на Python коді з IDE. Єдиний low-accuracy кадр (69.6%) — це кадр з autocomplete popup, який частково перекриває код (edge case, не типовий).

**Unified VD+OCR через Vision LLM** — підтверджений підхід.

## 5. Попередня рекомендація (до тесту Gemini)

| Роль | Model | Обґрунтування |
|---|---|---|
| **Primary** | Gemini 2.5 Flash | Найдешевший, 1M+ context (потрібен тест) |
| **Fallback** | GPT-4o | Підтверджена якість 93% |

### Залишається протестувати (Тести 4-7)

- [ ] Gemini Flash (після quota reset)
- [ ] STT context в промпті — чи покращує якість
- [ ] Two-pass (classify → filter → detailed) — cost savings
- [ ] Crop strategy — чи покращує code reading
- [ ] Batch processing — optimal batch size

## 6. Cost Estimate (production)

Для GPT-4o (fallback pricing):

| Відео | Frames (est.) | Cost |
|---|---|---|
| 10 хв | 15-50 | $0.08-$0.25 |
| 1 год | 100-200 | $0.50-$1.00 |
| 2 год | 200-350 | $1.00-$1.75 |
| Курс 20 год | 2000-3500 | $10-$17.50 |

Gemini Flash буде ~5-8x дешевше.
