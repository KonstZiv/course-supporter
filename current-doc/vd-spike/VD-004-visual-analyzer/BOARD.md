# VD-004: Visual Analyzer

**Статус:** TODO
**Фаза:** 4 — Implementation
**Пріоритет:** Critical
**Залежності:** VD-001, VD-003
**Блокує:** VD-007

Two-pass Vision LLM: Pass 1 batch classification (20-30 frames, importance >= 3 filter) → Pass 2 selective detailed analysis + text extraction (parallel, asyncio.Semaphore). Smart crop + optional STT context.
