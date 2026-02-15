# After Sprint 1 Review — Допрацювання

**Тип:** Tech debt / housekeeping
**Оцінка:** ~2-4 години
**Пріоритет:** Low — не блокує нічого, можна робити між спрінтами або як перший епік нового спрінту
**Контекст:** Знахідки з code review Sprint 1 (оцінка 8.5/10, 326 тестів, ~4890 LOC source)

---

## Задачі

### 1. Видалити порожній `models/llm.py`
**Файл:** `src/course_supporter/models/llm.py`
**Проблема:** Файл містить ~1 рядок, виглядає як залишок від раннього етапу розробки. Всі LLM-моделі живуть в `llm/schemas.py`.
**Дія:** Видалити файл, перевірити що немає імпортів з нього.
**Оцінка:** 5 хв

### 2. Додати `@overload` для router type safety
**Файл:** `src/course_supporter/llm/router.py`
**Проблема:** `_RouterResult` type alias використовує `type: ignore[return-value]` на методах `complete()` / `complete_structured()`. Це працює, але mypy не перевіряє return types повноцінно.
**Дія:** Додати `@overload` декоратори для `_execute_with_fallback`, щоб mypy розрізняв `str` return від `BaseModel` return залежно від `call_fn`.
**Оцінка:** 30-45 хв

### 3. Рефакторинг `PROVIDER_CONFIG` на dataclass
**Файл:** `src/course_supporter/llm/factory.py`
**Проблема:** `PROVIDER_CONFIG` використовує string-based attribute resolution (`"key_attr": "gemini_api_key"`, `"base_url_attr"`) через `getattr(settings, config["key_attr"])`. Це крихке — помилка в назві атрибута виявиться тільки в runtime.
**Дія:** Замінити dict-конфіг на `@dataclass ProviderConfig` з типізованими полями. Замість string-based `getattr` використати callable (lambda або property reference).
**Приклад:**
```python
@dataclass(frozen=True)
class ProviderFactoryConfig:
    provider_class: type[LLMProvider]
    get_api_key: Callable[[Settings], SecretStr | None]
    get_base_url: Callable[[Settings], str] | None = None
    default_model_attr: str = ""

PROVIDER_CONFIGS: dict[str, ProviderFactoryConfig] = {
    "gemini": ProviderFactoryConfig(
        provider_class=GeminiProvider,
        get_api_key=lambda s: s.gemini_api_key,
    ),
    # ...
}
```
**Оцінка:** 1-2 год (включаючи оновлення тестів)

### 4. CORS `["*"]` → production-ready
**Файл:** `src/course_supporter/config.py`
**Проблема:** `cors_allowed_origins: list[str] = ["*"]` з коментарем `# TODO: restrict for production`.
**Дія:** Для production deploy (Sprint PD) це вже враховано в PD-017 (Security Hardening). Тут лише маркер — перевірити що PD-017 покриває це.
**Оцінка:** 0 (вже в sprint-prod)

### 5. `error` → `pending` retry workflow
**Файл:** `src/course_supporter/storage/repositories.py`
**Проблема:** Коментар `# TODO: consider error → pending for retry workflow` в `VALID_TRANSITIONS`. Зараз `error` — terminal state.
**Дія:** Додати transition `"error": {"pending"}` та метод `retry()` в `SourceMaterialRepository`. Корисно для production, коли transient помилки S3/LLM можуть бути виправлені retry.
**Оцінка:** 30-45 хв

---

## Що НЕ входить (вже в backlog/sprint-prod)

- **Background task queue (Celery/TaskIQ)** — заміна `asyncio.create_task` для production. Окремий epic.
- **Integration tests** — очікувано для Sprint 1, планувати окремо.
- **CORS hardening** — покрито PD-017.

---

## Definition of Done

- [ ] `models/llm.py` видалений, імпорти перевірені
- [ ] Router methods мають `@overload` signatures, `type: ignore` прибрані
- [ ] `PROVIDER_CONFIG` → dataclass, string-based getattr замінено
- [ ] Error → pending transition додано з тестами
- [ ] `make check` зелений (ruff + mypy + pytest)
- [ ] Жоден існуючий тест не зламаний
