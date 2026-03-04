# S3-009: Unified Service Registry — config/external_services.yaml

**Phase:** 2 (ExternalServiceCall + Config)
**Складність:** S
**Статус:** PENDING

## Контекст

`config/models.yaml` покриває тільки LLM models. Потрібен unified registry для всіх зовнішніх сервісів — LLM providers, transcription, майбутні інтеграції.

## Файли для зміни

| Файл | Зміни |
|------|-------|
| `config/external_services.yaml` | НОВИЙ — unified registry |
| `config/models.yaml` | ВИДАЛИТИ (замінений) |
| `src/course_supporter/config.py` | Pydantic model для external_services config |
| `src/course_supporter/llm/registry.py` | Оновити для нового config формату |
| `src/course_supporter/llm/router.py` | Оновити provider selection |
| `tests/` | Тести для config loading |

## Деталі реалізації

### 1. Config file (external_services.yaml)

```yaml
providers:
  gemini:
    type: llm
    models:
      - id: gemini-2.0-flash
        unit_type: tokens
        cost_per_1k_in: 0.0001
        cost_per_1k_out: 0.0004
      - id: gemini-2.0-flash-lite
        unit_type: tokens
        # ...
  anthropic:
    type: llm
    models:
      - id: claude-sonnet-4-20250514
        unit_type: tokens
        # ...
  whisper:
    type: transcription
    models:
      - id: base
        unit_type: minutes
        local: true

strategies:
  default:
    providers: [gemini, anthropic, openai]
    fallback: true
  transcription:
    providers: [gemini, whisper]
    fallback: true

actions:
  generate_structure:
    strategy: default
    prompt_ref: v1
  transcribe:
    strategy: transcription
  describe_slides:
    strategy: default
```

### 2. Pydantic model (config.py)

```python
class ProviderModel(BaseModel):
    id: str
    unit_type: str  # tokens, minutes, chars
    cost_per_1k_in: float | None = None
    cost_per_1k_out: float | None = None
    local: bool = False

class ProviderConfig(BaseModel):
    type: str  # llm, transcription, scraping
    models: list[ProviderModel]

class StrategyConfig(BaseModel):
    providers: list[str]
    fallback: bool = True

class ActionConfig(BaseModel):
    strategy: str
    prompt_ref: str | None = None

class ExternalServicesConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    strategies: dict[str, StrategyConfig]
    actions: dict[str, ActionConfig]
```

### 3. Migration від models.yaml

`ModelRouter` та `LLMRegistry` мають бути оновлені для роботи з новим форматом. Backward compatibility не потрібна — це internal config.

## Acceptance Criteria

- [ ] `config/external_services.yaml` з unified registry
- [ ] `config/models.yaml` видалений
- [ ] Config завантажується та валідується Pydantic
- [ ] `ModelRouter` працює з новим config
- [ ] Worker стартує з новим config
- [ ] Тести покривають config validation
