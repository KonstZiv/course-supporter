# VD-018: Temp Files Cleanup

**Фаза:** 6 — Polish
**Пріоритет:** Medium
**Залежності:** VD-007

## Що робимо

Перевіряємо та гарантуємо коректне видалення тимчасових файлів у всіх сценаріях: успішне завершення, помилка, timeout. VD pipeline створює тимчасові директорії з extracted frames та audio segments.

## Яким чином

1. **Verify frame cleanup (VDPipeline):**
   - `tempfile.mkdtemp()` створює директорію для JPEG кадрів
   - `try/finally` з `shutil.rmtree()` має спрацювати при:
     - Успішному завершенні pipeline
     - Exception в будь-якому stage (A, B, C, D)
     - Timeout (asyncio.TimeoutError)
     - KeyboardInterrupt / SystemExit

2. **Verify audio cleanup (VideoProcessor):**
   - Extracted MP3 audio file
   - Audio segments (при chunking)
   - Тимчасові файли від FFmpeg

3. **Context manager pattern:**
   - Розглянути `contextlib.asynccontextmanager` або `__aenter__`/`__aexit__` для VDPipeline
   - Або залишити `try/finally` якщо context manager надмірний

4. **Log warnings при cleanup failure:**
   - Якщо `shutil.rmtree()` fails (permission error, file in use) — залогувати warning через `structlog`
   - НЕ re-raise exception — cleanup failure не має зупинити основний flow
   - Логувати шлях до директорії що не видалилась

5. **Stress test:**
   - Запустити pipeline і перервати на кожному stage — перевірити що temp files видалені
   - Перевірити що `/tmp` не засмічується після багатьох запусків

## Результат

- Гарантований cleanup temp files у всіх сценаріях
- Warning logs при cleanup failure
- Немає file leaks при повторних запусках

## Як перевіряємо

```bash
uv run ruff check src/course_supporter/vd/             # no lint errors
# Manual test:
# 1. Запустити pipeline, перервати посередині — перевірити /tmp
# 2. Запустити pipeline 10 разів — перевірити що /tmp не росте
ls /tmp/vd_* 2>/dev/null || echo "No temp files (OK)"
```
