# S1-009: ModelRouter

## Мета
Центральна точка LLM-викликів з two-level fallback: within chain + cross-strategy.

## Що робимо
1. **ModelRouter** — `complete(action, prompt, strategy=)`
2. **Two-level fallback**: chain внутрішній + cross-strategy (quality → default)
3. **Disabled providers** — skip if `provider.enabled is False`
4. **AllModelsFailedError** — всі strategies вичерпані
5. **Strategy tracking** — `response.strategy = "quality→default"`
6. **Cost enrichment** та **LogCallback**

## Контрольні точки
- [ ] Default strategy з fallback всередині chain
- [ ] Explicit strategy працює
- [ ] Disabled provider → skip
- [ ] Cross-strategy fallback (quality → default)
- [ ] Default не fallback-ить на себе
- [ ] AllModelsFailedError з деталями
- [ ] `make check` проходить

## Залежності
- **Блокується:** S1-007, S1-008
- **Блокує:** S1-010, всі наступні компоненти
