# VD-016: STT-Guided Visual Description

**Статус:** TODO
**Фаза:** 6 — Quality Improvement
**Пріоритет:** High
**Залежності:** VD-011 (done)
**Блокує:** —

## Scope

| Крок | Що | API calls | Статус |
|------|----|-----------|--------|
| Spike | A/B test: 1-2 frames з/без STT context | 2-4 | TODO |
| 1 | STT context block у Eyes prompt | 0 | TODO |
| 2 | VDPipeline + VisualAnalyzer wiring | 0 | TODO |
| 3 | VideoProcessor: parallel → sequential | 0 | TODO |
| 4 | E2E verification (CP-7b) | ~60 | TODO |

## Key Hypothesis

STT текст як context у Eyes prompt → VLM краще описує що на екрані.
Spike перевіряє це за 2-4 API calls перед повною інтеграцією.
