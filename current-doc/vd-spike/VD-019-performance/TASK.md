# VD-019: Performance Profiling

**Фаза:** 6 — Polish
**Пріоритет:** Medium
**Залежності:** VD-014

## Що робимо

Профілюємо VD pipeline для виявлення bottlenecks та оптимізації. Вимірюємо час обробки для типових use cases: 10-хвилинне та 2-годинне відео.

## Яким чином

1. **Frame extraction profiling (CPU-bound):**
   - FFmpeg scene detection — наскільки швидко?
   - dHash computation — чи потрібна batch optimization?
   - Якщо CPU-bound операції блокують event loop — обгорнути в `asyncio.to_thread()`
   - Заміряти: frames per second для extraction

2. **dHash computation optimization:**
   - Профілювати dHash для 100, 500, 1000 кадрів
   - Batch computation: чи швидше рахувати hash для всіх кадрів одразу?
   - Розглянути: зменшення hash_size якщо 16 занадто повільне

3. **Vision LLM batching profiling:**
   - Оптимальний batch size для Pass 1: 20 vs 25 vs 30 кадрів
   - Оптимальний concurrency для Pass 2: 3 vs 5 vs 8 паралельних запитів
   - Вплив rate limits на throughput
   - Latency per batch vs total wall time

4. **Total processing time:**
   - 10-хвилинне відео: очікується ~2-3 хвилини
   - 2-годинне відео (симуляція): очікується ~10-15 хвилин
   - Breakdown по stages: A%, B%, C%, D%

5. **Identify bottlenecks:**
   - Якщо Stage A (extraction) > 30% часу → optimize FFmpeg params
   - Якщо Stage B (Vision LLM) > 60% часу → increase concurrency або batch size
   - Якщо aggregation повільна → оптимізувати cross-reference lookup

6. **Optimize:**
   - Впровадити знайдені оптимізації
   - Повторно заміряти — порівняти з baseline (VD-014)

## Результат

- Performance baseline для 10-хв та 2-год відео
- Breakdown по stages
- Ідентифіковані та оптимізовані bottlenecks
- asyncio.to_thread() для CPU-bound операцій (якщо потрібно)

## Як перевіряємо

```bash
# Profiling script:
uv run python scripts/profile_vd_pipeline.py --video sample.mp4
# Output: таблиця з часом кожного stage, total time, frames/sec
# Порівняти з baseline з VD-014
```
