# VD-011: E2E Test

**Фаза:** 5 — Integration
**Пріоритет:** Високий
**Залежності:** VD-006, VD-007, VD-008, VD-009, VD-010

## Що робимо

End-to-end integration test: відео → STT + VD (parallel) → alignment → SourceDocument.

## Яким чином

- Один повний прохід на тестовому відео
- Verify: chunks покривають все відео
- Verify: ChunkType balance (STT + VD + aligned)
- Verify: AlignmentReport без critical gaps
- Performance baseline: час обробки 10-хв відео

## Acceptance criteria

- [ ] Pipeline працює end-to-end без crash
- [ ] SourceDocument має chunks всіх нових типів
- [ ] Coverage >90% відео
- [ ] Performance: 10-хв відео < 30 хвилин

## ⚠️ Після завершення — виконати CP-6: Фінальна перевірка SourceDocument

Див. VD-IMPLEMENTATION-PLAN.md, секція CP-6. Людина перевіряє: SourceDocument як конспект, ChunkType balance, порівняння з baseline. ~1-2 години.
