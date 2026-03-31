# VD-007: Pipeline Orchestrator

**Статус:** TODO
**Фаза:** 4 — Implementation
**Пріоритет:** Critical
**Залежності:** VD-002, VD-003, VD-004, VD-005 (optional), VD-006
**Блокує:** VD-009

`VDPipeline` клас: Stage A (frames) → B (visual analysis) → C (OCR, optional) → D (aggregation). Temp dir management (mkdtemp + try/finally rmtree). Concurrency: VISION_LLM_CONCURRENCY = 5.
