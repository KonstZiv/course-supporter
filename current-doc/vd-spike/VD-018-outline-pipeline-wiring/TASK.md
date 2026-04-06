# VD-018: Wire OutlineAgent into Ingestion Pipeline

**Фаза:** 7 — Pre-Agent Processing (продовження VD-017)
**Пріоритет:** Критичний (блокує production processing)
**Залежності:** VD-017 (done)

---

## Проблема

VD-017 створив OutlineAgent і wiring для ArchitectAgent (читає `outline_content` з fallback на raw). Але **OutlineAgent ніде не викликається автоматично** — `outline_content` завжди NULL. Без outline ArchitectAgent працює на raw SourceDocument зі шумом STT/VD.

## Рішення

Вбудувати виклик OutlineAgent в ingestion pipeline: після успішного `complete_processing()` → генерувати outline → зберегти в `outline_content`.

```
Processor.process() → SourceDocument
    ↓
IngestionCallback.on_success()
    ↓ complete_processing(processed_content=...)
    ↓ [NEW] OutlineAgent.run() → MaterialOutline
    ↓ [NEW] save_outline(outline_json=...)
    ↓ job_repo.update_status("complete")
    ↓ session.commit()
```

## Точка інтеграції

**Файл:** `src/course_supporter/ingestion_callback.py` → `on_success()`

Після `entry_repo.complete_processing()` (line ~72), перед `job_repo.update_status()` (line ~74):
1. Deserialize `content_json` → `SourceDocument`
2. `OutlineAgent(router).run_with_metadata(source_doc)` → `OutlineResult`
3. `entry_repo.save_outline(material_id, outline_json=outline.model_dump_json())`
4. Persist `ExternalServiceCall` для LLM call tracking

**Проблема:** `IngestionCallback` не має `ModelRouter`. Потрібно прокинути через конструктор.

## Кроки реалізації

### Крок 1: Прокинути ModelRouter в IngestionCallback

**Файли:**
- `src/course_supporter/ingestion_callback.py` — додати `router: ModelRouter | None = None` в `__init__`
- `src/course_supporter/api/tasks.py` — передати `router` при створенні `IngestionCallback`

### Крок 2: Генерація outline в on_success

**Файл:** `src/course_supporter/ingestion_callback.py`

Додати метод `_generate_outline()`:
- Deserialize `content_json` → `SourceDocument`
- `OutlineAgent(router).run_with_metadata(source_doc)` → `OutlineResult`
- `entry_repo.save_outline(material_id, outline_json=...)`
- Persist `ExternalServiceCall` record
- **Важливо:** outline failure НЕ повинен зламати ingestion. Wrap у try/except, log warning, продовжити.

Викликати `_generate_outline()` в `on_success()` після `complete_processing()`.

### Крок 3: Тести

- Unit тест: `IngestionCallback.on_success()` з mock router → outline generated + saved
- Unit тест: outline failure → ingestion still succeeds (graceful degradation)
- Unit тест: router=None → outline skipped
- Integration: verify outline_content populated after full ingestion

### Крок 4: Вибір default моделі для outline

Поточний config:
```yaml
material_outline:
  chain:
    default: [gemini-2.5-flash, deepseek-chat]
```

Spike показав: DeepSeek достатня якість за мінімальну ціну. Але default chain починає з Gemini (free tier) — це OK для production. Якщо Gemini rate limit → fallback на DeepSeek.

**Розглянути:** чи варто змінити default на `[deepseek-chat, gemini-2.5-flash]` — deepseek стабільніший (немає rate limit issues), Gemini як fallback.

## Error Handling

Outline generation — **nice-to-have**, не блокер для ingestion:
- Якщо LLM зафейлив → log warning, `outline_content` залишається NULL
- ArchitectAgent має fallback на raw `processed_content`
- Outline можна regenerate пізніше (re-trigger через API або manual)
- НЕ змінювати Job status — ingestion вважається успішною навіть без outline

## Acceptance Criteria

- [ ] `IngestionCallback` приймає `ModelRouter` (optional)
- [ ] `on_success()` генерує outline після `complete_processing()`
- [ ] Outline failure не зламує ingestion (graceful degradation)
- [ ] `ExternalServiceCall` record зберігається для LLM call tracking
- [ ] Всі існуючі тести зелені
- [ ] Нові unit тести для outline generation в callback
