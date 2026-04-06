# VD-017: Material Outline

**Статус:** TODO
**Фаза:** 7 — Pre-Agent Processing
**Пріоритет:** High
**Залежності:** VD-011 (done)

## Scope

| Крок | Що | API calls | Статус |
|------|----|-----------|--------|
| 1 | Pydantic schemas (MaterialOutline, OutlineSection, CodeSnippet) | 0 | TODO |
| 2 | Outline prompt v1 (lossless restructuring, NOT summarization) | 0 | TODO |
| 3 | OutlineAgent (ModelRouter, action=material_outline) | 0 | TODO |
| 4 | DB migration + storage (outline_content field) | 0 | TODO |
| 5 | Spike на реальних даних + A/B (Flash vs quality) | 1-2 | TODO |
| 6 | Agent wiring (outline_content з fallback) | 0 | TODO |

## Key Idea

SourceDocument (raw STT + VD) → один LLM call → MaterialOutline ("ідеальний конспект": lossless restructuring по тематичних блоках 2-5 хв з часовими мітками, key_concepts, code as-is). Зберігається в `material_entries.outline_content`. Всі downstream агенти працюють з outline. Drill-down у raw за time range коли потрібна деталізація.
