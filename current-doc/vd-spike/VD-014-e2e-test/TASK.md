# VD-014: E2E Test

**Фаза:** 5 — Integration
**Пріоритет:** Critical
**Залежності:** VD-009, VD-011, VD-012, VD-013

## Що робимо

End-to-end тест повного VD pipeline на реальному відео. Перевіряємо що весь flow від завантаження відео до фінального `SourceDocument` працює коректно.

## Яким чином

1. **Full pipeline test:**
   ```
   Download video → Extract audio → STT (parallel)
   → Frame sampling (scene detect + dHash)
   → Visual analysis (Pass 1 + Pass 2)
   → Aggregation (merge STT + VD)
   → SourceDocument
   ```

2. **Verify SourceDocument chunks:**
   - Має містити `TRANSCRIPT` chunks (з STT)
   - Має містити `VISUAL_DESCRIPTION` chunks (з VD Pass 2)
   - Має містити `CODE_BLOCK` chunks (якщо у відео є код на екрані)
   - Можливо: `SLIDE_OCR`, `TERMINAL_OUTPUT`, `DIAGRAM_DESCRIPTION`

3. **Verify timestamps:**
   - Всі chunks відсортовані за `start_time`
   - Timestamps в межах тривалості відео (0 <= timestamp <= video_duration)
   - Немає від'ємних або нереально великих timestamps
   - Cross-references між VD та STT chunks мають сенс (±5 sec window)

4. **Verify metadata:**
   - `strategy: "stt+vd"`
   - `stt_provider` заповнений (e.g., "elevenlabs")
   - `vd_frames_total` > 0
   - `pip_events` >= 0

5. **Performance baseline:**
   - Заміряти загальний час обробки 10-хвилинного sample відео
   - Записати baseline для порівняння в майбутньому

## Результат

- E2E тест у `tests/integration/test_vd_e2e.py`
- Тест на реальному sample відео
- Assertions на chunks, timestamps, metadata
- Performance baseline записаний

## Як перевіряємо

```bash
uv run pytest tests/integration/test_vd_e2e.py -v -s   # E2E test
# Markers: @pytest.mark.requires_ffmpeg, @pytest.mark.slow
# Manual inspection: подивитись на output SourceDocument — чи має сенс?
```
