# VD-002: PiP Tracker

**Фаза:** 4 — Implementation
**Пріоритет:** High
**Залежності:** VD-001

## Що робимо

Створюємо модуль для 3-рівневого відстеження Picture-in-Picture (PiP) зони у відео. PiP — це маленьке вікно з обличчям лектора, яке може з'являтися/зникати/переміщуватись. Потрібно знати його позицію, щоб маскувати при dHash обчисленні та frame sampling.

## Яким чином

Створити `src/course_supporter/vd/pip_tracker.py` з функцією `track_pip()`:

1. **Рівень 1 — Vision LLM initial detection:**
   - Відправити перший кадр відео до Gemini Flash через існуючий `ModelRouter`
   - Запитати: "Is there a PiP (picture-in-picture) webcam overlay? If yes, return bounding box coordinates"
   - Отримати початкову позицію PiP зони (або підтвердження що PiP немає)

2. **Рівень 2 — Temporal diff tracking:**
   - Використовувати `cv2.absdiff()` + zone motion detection між послідовними кадрами
   - Відстежувати рух PiP зони (переміщення, зникнення, повторна поява)
   - Логувати `PiPEvent` для кожної зміни стану

3. **Рівень 3 — Periodic validation:**
   - Кожні N кадрів (або при значній зміні scene) повторно перевіряти через Vision LLM
   - Коригувати bounding box якщо PiP змістився

4. **No-PiP handling:**
   - Якщо Рівень 1 не знайшов PiP — повернути порожній `PiPMask` і пропустити Рівні 2-3
   - Pure screen recordings (без вебкамери) — типовий випадок для багатьох курсів

5. **PiPEvent logging:**
   - Записувати кожну зміну стану: appeared, disappeared, moved
   - Зберігати timestamp, zone, method (vision_llm / temporal_diff), confidence

## Результат

- Файл `src/course_supporter/vd/pip_tracker.py`
- Функція `track_pip()` що повертає `list[PiPEvent]` та фінальний `PiPMask`
- Використовує `ModelRouter` для Vision LLM запитів (Gemini Flash)
- Використовує моделі `PiPMask`, `PiPEvent`, `Rect` з `vd/schemas.py`
- Обробляє edge case: відео без PiP

## Як перевіряємо

```bash
uv run mypy src/course_supporter/vd/pip_tracker.py   # strict, no errors
uv run ruff check src/course_supporter/vd/            # no lint errors
uv run pytest tests/unit/test_vd_pip_tracker.py -v    # unit tests (після VD-015)
```
