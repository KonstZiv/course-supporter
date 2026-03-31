# VD-SPIKE-A: Frame Sampling + PiP Tracking

**Фаза:** 1 — Spike
**Пріоритет:** Критичний (блокує Spike B і C)
**Залежності:** VD-000

## Що робимо

Визначаємо оптимальні параметри витягування "унікальних" кадрів з навчального відео. Тестуємо scene detection, dHash dedup, PiP tracking, cooldown logic.

## Яким чином

### 1. FFmpeg frame extraction — порівняння стратегій

На тестовому відео (`sample.mp4`, 10:35):
- **Scene detection:** `ffmpeg -vf "select=gt(scene,0.3)"` — рахуємо скільки кадрів
- **Fixed fps:** `ffmpeg -vf fps=0.5` (кожні 2 сек) — рахуємо
- **Комбінована:** scene detect + мінімальний інтервал 30 сек (заповнюємо gaps)
- Порівнюємо кількість кадрів і якість покриття (чи всі слайд-переходи зафіксовані)

### 2. dHash tuning

Для кожної стратегії extraction:
- `hash_size`: 8, 12, 16
- Thresholds: 5%, 10%, 15%, 20% від max Hamming distance
- Рахуємо кількість "унікальних" кадрів після фільтрації
- Таблиця: strategy × hash_size × threshold → кількість кадрів

### 3. PiP tracking

- **Initial detection:** Vision LLM (Gemini Flash) на перших 10 кадрах — "де PiP камера?"
- **Temporal diff:** `cv2.absdiff` між сусідніми кадрами, motion per zone (4 кути + центри)
- **Тест no-PiP:** Чи правильно визначає "none" якщо PiP немає
- **Порівняння:** dHash з маскою PiP vs без маски → різниця у false positives

### 4. Cooldown logic

- Визначити чи є live coding у тестовому відео
- Якщо так: порівняти з cooldown (3-5 сек) vs без → кількість near-identical кадрів

### 5. Перевірка роздільності

- Записати resolution тестового відео
- Візуально оцінити: чи читається код на витягнутих кадрах

### Інструменти

`ffmpeg`, `opencv-python-headless`, `Pillow`, `ImageHash`, `numpy`, Gemini Flash API (для PiP detection)

## Результат

- Скрипт `scripts/spike_frame_sampling.py` — повний pipeline з параметрами
- Таблиця результатів у `current-doc/vd-spike/VD-SPIKE-A/RESULTS.md`
- Набір "golden" кадрів (50-200 шт.) у `current-doc/vd-spike/golden-frames/`
- PiP tracking log
- Рекомендовані параметри для production

## Як перевіряємо

- Візуальна перевірка golden frames: всі слайд-переходи зафіксовані (recall 100%)
- Немає 100+ near-identical кадрів з друку коду
- PiP tracking log показує стабільну роботу
- Загальна кількість golden frames: 50-200 для 10-хв відео
- Скрипт запускається і завершується без помилок
