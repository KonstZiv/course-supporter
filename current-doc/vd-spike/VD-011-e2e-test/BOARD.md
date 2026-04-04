# VD-011: E2E Integration Test

**Статус:** TODO
**Фаза:** 5 — Integration
**Пріоритет:** High
**Залежності:** VD-001..010 (all done)
**Блокує:** Фаза 6
**Human check:** CP-6 (1-2 год) — фінальна перевірка SourceDocument

## Scope

| Крок | Що | Статус |
|------|----|--------|
| 0 | VideoProcessor: Whisper → STT Router | TODO |
| 1 | E2E скрипт `scripts/cp4_e2e_test.py` | TODO |
| 2 | HTML звіт: CP-4 + VD-011 + CP-5 | TODO |

## Deliverables

- `scripts/cp4_e2e_test.py` — один прогін, один набір API calls
- HTML звіт з 3 секціями (CP-4 VD quality, VD-011 integration, CP-5 alignment)
- Оновлені тести для VideoProcessor (STT Router замість Whisper)

## Key Metrics

- Coverage > 90% відео
- Semantic coverage > 0.5
- 5-хв відео < 15 хв обробки
- Обидва ChunkType в SourceDocument
