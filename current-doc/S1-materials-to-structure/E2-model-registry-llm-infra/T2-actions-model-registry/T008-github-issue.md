# S1-008: Actions & Model Registry

## Мета
YAML-конфіг: **models** (capabilities + cost), **actions** (вимоги), **routing** (action → strategies → chains). Cross-validation при старті.

## Що робимо
1. **models.yaml** — 5 моделей × 4 actions × 3 strategies (default/quality/budget)
2. **Pydantic-схеми** — `ModelCapability`, `ActionConfig`, `ModelRegistryConfig` з `model_validator`
3. **`get_chain(action, strategy)`** → ordered list
4. **Валідація**: capabilities mismatch → error; missing default → error

## Приклад
```yaml
routing:
  video_analysis:
    default: [gemini-2.5-flash, gemini-2.5-pro]
    quality: [gemini-2.5-pro, claude-sonnet]
```

## Контрольні точки
- [ ] `load_registry()` → валідний config
- [ ] `get_chain("video_analysis", "quality")` → correct chain
- [ ] Model without vision в video_analysis routing → ValueError
- [ ] Missing default strategy → error
- [ ] `make check` проходить

## Залежності
- **Блокується:** S1-007
- **Блокує:** S1-009
