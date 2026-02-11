# S1-007: LLM Providers

## Мета
Розширюваний реєстр LLM-провайдерів з runtime enable/disable.

## Що робимо
1. **LLMProvider ABC** — `complete()` + `complete_structured()` + enable/disable
2. **GeminiProvider** — google-genai SDK, нативний structured output
3. **AnthropicProvider** — official SDK, structured output через system prompt
4. **OpenAICompatProvider** — OpenAI та DeepSeek через один клас (`base_url`)
5. **Schemas** — `LLMRequest` (action + strategy), `LLMResponse` (unified)
6. **PROVIDER_REGISTRY** — dict `name → class`, extensible без зміни factory
7. **Factory** — data-driven `create_providers(settings)`

## Контрольні точки
- [ ] LLMProvider ABC — не можна інстанціювати
- [ ] Провайдери повертають `LLMResponse` з tokens/latency
- [ ] `complete_structured()` → `(ParsedModel, LLMResponse)`
- [ ] PROVIDER_REGISTRY містить 4 провайдери
- [ ] `create_providers()` — тільки ті, де є API key
- [ ] `provider.disable()` / `enable()` працюють
- [ ] `make check` проходить

## Залежності
- **Блокується:** S1-004 (config)
- **Блокує:** S1-008, S1-009
