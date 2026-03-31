# VD-014: E2E Test

**Статус:** TODO
**Фаза:** 5 — Integration
**Пріоритет:** Critical
**Залежності:** VD-009, VD-011, VD-012, VD-013
**Блокує:** —

E2E тест на sample відео: download → audio extract → STT + frame sampling → Visual analysis → Aggregation → SourceDocument. Verify chunks (TRANSCRIPT + VD types), timestamps (sorted, within duration), metadata (strategy, provider, frames). Performance baseline.
