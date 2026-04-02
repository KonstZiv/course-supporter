# VD-010: Update MergeStep

**Фаза:** 5 — Integration
**Пріоритет:** Середній
**Залежності:** VD-009

## Що робимо

Оновити MergeStep для обробки нових ChunkType значень від VD pipeline.

## Яким чином

- MergeStep.merge() має коректно обробляти VISUAL_SCENE, VISUAL_CODE, VISUAL_SLIDE, VISUAL_TERMINAL, ALIGNED_SEGMENT
- Aligned segments мають priority metadata для downstream agents (ArchitectAgent)
- Cross-references між VISUAL і TRANSCRIPT chunks зберігаються

## Acceptance criteria

- [ ] MergeStep не ламається на нових ChunkTypes
- [ ] Downstream ArchitectAgent отримує VD chunks
- [ ] Існуючий merge flow для non-video sources не змінився
