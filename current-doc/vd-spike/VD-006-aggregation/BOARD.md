# VD-006: Aggregation Layer

**Статус:** TODO
**Фаза:** 4 — Implementation
**Пріоритет:** High
**Залежності:** VD-001
**Блокує:** VD-007

Merge STT transcript + VD analysis + optional OCR в єдину timeline. Cross-reference (±5 sec window), deduplication (fuzzy match, вищий confidence), priority: VISUAL_DESCRIPTION > CODE_BLOCK > TRANSCRIPT. Повертає `list[ContentChunk]`.
