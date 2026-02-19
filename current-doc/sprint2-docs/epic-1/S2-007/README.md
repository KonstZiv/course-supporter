# S2-007: Queue estimate service

**Epic:** EPIC-1 — Infrastructure — ARQ + Redis
**Оцінка:** 4h

---

## Мета

При submit job — розрахунок estimated start/complete з урахуванням черги і вікна

## Що робимо

Створити QueueEstimateService з методом estimate_job()

## Як робимо

1. QueueEstimate dataclass: position_in_queue, estimated_start, estimated_complete, next_window_start, queue_summary
2. estimate_job(): count_pending × avg_completion_time
3. Window-aware: якщо поза вікном → next_start + queue time
4. avg_completion_time з jobs history (або default для нових систем)
5. Обробка випадку коли черга не вміщується в одне вікно (overflow на наступний день)

## Очікуваний результат

estimate_job() повертає адекватні прогнози з урахуванням черги і вікна

## Як тестуємо

**Автоматизовано:** Unit tests: порожня черга (start=now), 5 jobs в черзі, поза вікном (start=next_window+queue), overflow на наступний день, 24/7 mode, default avg коли немає history

**Human control:** Додати 3 job-и в чергу, перевірити що estimated_at для 4-го адекватний (queue_position × avg_time)

## Точки контролю

- [ ] Код написаний і проходить `make check`
- [ ] Tests написані і зелені
- [ ] Human control пройдений
- [ ] Documentation checkpoint: чи потрібно оновити docs/ERD/наступні задачі
