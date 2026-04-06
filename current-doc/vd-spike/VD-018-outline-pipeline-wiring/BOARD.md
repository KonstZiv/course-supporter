# VD-018: Outline Pipeline Wiring

**Статус:** TODO
**Фаза:** 7 — Pre-Agent Processing
**Пріоритет:** Critical (blocks production)
**Залежності:** VD-017 (done)

## Scope

| Крок | Що | API calls | Статус |
|------|----|-----------|--------|
| 1 | Прокинути ModelRouter в IngestionCallback | 0 | TODO |
| 2 | Генерація outline в on_success (graceful) | 0 | TODO |
| 3 | Тести (success, failure, no-router) | 0 | TODO |
| 4 | Default model chain review | 0 | TODO |

## Key Idea

OutlineAgent викликається автоматично після кожного successful ingestion. Outline failure НЕ зламує ingestion — graceful degradation з fallback на raw SourceDocument.
