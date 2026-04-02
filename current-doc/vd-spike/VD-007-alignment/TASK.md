# VD-007: Cross-modal Alignment (alignment.py)

**Фаза:** 5 — Integration
**Пріоритет:** Високий
**Залежності:** VD-001 (schemas), VD-006
**Складність:** Висока (нова логіка, не port)

## Що робимо

Створити модуль cross-modal alignment: з'єднати результати STT (що лектор каже) з VD (що на екрані) в єдиний потік з verification.

**Належить до:** `src/course_supporter/ingestion/alignment.py` (НЕ до `vd/`).

## Яким чином

### CrossModalAligner class

4 фази alignment:

**Phase 1: Temporal matching**
- VD scene [start_sec, end_sec] ↔ STT segments з overlap ±10s
- Greedy: кожна VD scene отримує STT segments з максимальним temporal overlap

**Phase 2: Semantic cross-reference**
- Витягти identifiers з VD code blocks (function names, variables, classes)
- Fuzzy match identifiers в STT text
- Score: semantic_overlap 0.0-1.0

**Phase 3: Conflict detection**
- Code/identifiers conflict: VD wins (екран = ground truth)
- Numbers conflict: VD wins
- Natural language: both kept
- Timing/sequence: STT wins

**Phase 4: Verification**
- Coverage gaps (>30s без контенту)
- VD orphans (scenes без STT match)
- STT orphans (segments без VD)
- Overall semantic_coverage %

### AlignmentVerifier class

Post-alignment quality report → AlignmentReport.

## Acceptance criteria

- [ ] Temporal alignment коректний (manual verification на тестовому відео)
- [ ] Known conflict detected (як fa12/faa12 scenario)
- [ ] Coverage gaps >30s flagged
- [ ] AlignmentReport has actionable information
- [ ] Це алгоритмічна робота (НЕ LLM calls)

## ⚠️ Після завершення — виконати CP-5: Перевірка Alignment

Див. VD-IMPLEMENTATION-PLAN.md, секція CP-5. Людина перевіряє: AlignmentReport, 10 пар, conflicts, known conflict test. ~1 година.
