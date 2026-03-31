# VD-008: External Services Config

**Фаза:** 4 — Implementation
**Пріоритет:** High
**Залежності:** Spikes A+B (щоб знати які моделі/chains)

## Що робимо

Додаємо нові actions до конфігурації `external_services.yaml` для Vision LLM запитів VD pipeline. `ModelRouter` має підхопити нові actions і використовувати правильні fallback chains.

## Яким чином

Оновити `config/external_services.yaml`:

1. **Action `visual_classification` (Pass 1):**
   - Мета: швидка batch класифікація кадрів
   - Default chain: `[gemini-2.5-flash]`
   - Gemini Flash — найкращий price/performance для класифікації зображень

2. **Action `visual_analysis` (Pass 2):**
   - Мета: детальний аналіз кадрів з витягуванням тексту/коду
   - Default chain: `[gemini-2.5-flash, gpt-4o]`
   - GPT-4o як fallback якщо Gemini Flash дає неякісний результат

3. **Action `ocr_correction` (Stage C, якщо потрібен):**
   - Мета: LLM post-processing для корекції OCR помилок
   - Default chain: `[deepseek-chat, gemini-2.5-flash]`
   - DeepSeek — найдешевший для text correction, Gemini Flash як fallback

4. **Verification:**
   - Перевірити що `ModelRouter` коректно зчитує нові actions
   - Перевірити fallback behaviour: якщо primary модель недоступна → fallback працює
   - Перевірити що key rotation працює з новими actions

## Результат

- Оновлений `config/external_services.yaml` з 3 новими actions
- `ModelRouter` підхоплює `visual_classification`, `visual_analysis`, `ocr_correction`
- Fallback chains працюють коректно

## Як перевіряємо

```bash
uv run python -c "
from course_supporter.llm.router import ModelRouter
router = ModelRouter()
# Перевірити що нові actions доступні
for action in ['visual_classification', 'visual_analysis', 'ocr_correction']:
    chain = router.get_chain(action)
    print(f'{action}: {chain}')
"
uv run ruff check src/                                # no lint errors
```
