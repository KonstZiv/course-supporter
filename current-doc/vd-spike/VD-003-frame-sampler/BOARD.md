# VD-003: Frame Sampler

**Статус:** TODO
**Фаза:** 4 — Implementation
**Пріоритет:** Critical
**Залежності:** VD-001, VD-002
**Блокує:** VD-004, VD-007

FFmpeg scene detection + dHash dedup (hash_size=16, threshold=25) + PiP masking перед hash + cooldown для live coding. Повертає `FrameSamplingResult`.
