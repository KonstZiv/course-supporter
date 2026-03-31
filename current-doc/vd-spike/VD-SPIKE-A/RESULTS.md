# VD-SPIKE-A: Results — Frame Sampling + PiP Tracking

**Дата:** 2026-03-31
**Відео:** 1280x720, 30.0 fps, 635.3s (19058 frames)

## 1. FFmpeg Extraction Strategies

### Scene Detection

| Threshold | Frames |
|---|---|
| 0.01 | 37 |
| 0.02 | 21 |
| 0.03 | 17 |
| 0.05 | 11 |
| 0.1 | 6 |
| 0.3 | 6 |

### Fixed FPS

| FPS | Frames |
|---|---|
| 0.5 | 318 |
| 1.0 | 635 |
| 2.0 | 1271 |

### Combined (scene=0.02 + interval=30s): **39 frames**

## 2. PiP Detection

**PiP знайдено:** mask (0, 480) → (320, 720)

| Zone | Avg Motion |
|---|---|
| bottom_left | 12.154 |
| left_center | 3.286 |
| top_left | 2.583 |
| top_center | 2.069 |
| top_right | 1.714 |
| bottom_center | 1.623 |
| bottom_right | 1.267 |
| right_center | 1.054 |

## 3. dHash Tuning

Base: fps=0.5 extraction (318 frames)

### hash_size=8

**no_mask:**

| Threshold | Unique Frames |
|---|---|
| 5% (dist>3) | 10 |
| 10% (dist>6) | 9 |
| 15% (dist>9) | 8 |
| 20% (dist>12) | 7 |

**pip_mask:**

| Threshold | Unique Frames |
|---|---|
| 5% (dist>3) | 10 |
| 10% (dist>6) | 9 |
| 15% (dist>9) | 7 |
| 20% (dist>12) | 7 |

### hash_size=12

**no_mask:**

| Threshold | Unique Frames |
|---|---|
| 5% (dist>7) | 24 |
| 10% (dist>14) | 11 |
| 15% (dist>21) | 9 |
| 20% (dist>28) | 8 |

**pip_mask:**

| Threshold | Unique Frames |
|---|---|
| 5% (dist>7) | 13 |
| 10% (dist>14) | 9 |
| 15% (dist>21) | 9 |
| 20% (dist>28) | 8 |

### hash_size=16

**no_mask:**

| Threshold | Unique Frames |
|---|---|
| 5% (dist>12) | 16 |
| 10% (dist>25) | 10 |
| 15% (dist>38) | 9 |
| 20% (dist>51) | 7 |

**pip_mask:**

| Threshold | Unique Frames |
|---|---|
| 5% (dist>12) | 11 |
| 10% (dist>25) | 9 |
| 15% (dist>38) | 9 |
| 20% (dist>51) | 7 |

## 4. Cooldown Logic

hash_size=16, threshold=10% (dist>25)

- **Without cooldown:** 9 frames
- **With cooldown** (4.0s, 3 consecutive): 9 frames

## 5. Golden Frames

- **Кількість:** 17
- **Extraction:** fps=1.0
- **hash_size:** 16
- **Threshold:** 5% (dist>12)
- **PiP mask:** Yes
- **Шлях:** `current-doc/vd-spike/golden-frames/`

## 6. Аналіз та рекомендації

### Ключові спостереження

**Scene detection:**
- Для лекційного відео (слайди + talking head) FFmpeg scene detection дає **дуже мало кадрів** (6-37). Відео має мало hard cuts — переходи між слайдами плавні.
- Threshold 0.01-0.02 дає більше кадрів, але все одно недостатньо для повного покриття.
- Scene detection корисний як **доповнення** до fixed fps, але не як єдина стратегія.

**Fixed FPS:**
- fps=0.5 (318 кадрів) → після dHash залишається 7-16 кадрів. Це **основна стратегія**.
- fps=1.0 (635 кадрів) → дає трохи більше деталей, але dHash все одно зменшує до ~17 кадрів.
- fps=2.0 (1271 кадрів) — overkill, dHash все одно прибере дублікати.

**PiP tracking:**
- Temporal diff **чудово працює** — bottom_left зона має motion 12.15 (4x більше за наступну).
- Confidence 0.73 — високий, однозначне визначення.
- PiP маска зменшує false positives: hash_size=12, 5%: 24 → 13 (майже вдвічі).
- Vision LLM для initial detection **не знадобився** — temporal diff достатній.

**dHash:**
- hash_size=8 — занадто грубий, не розрізняє дрібні зміни (7-10 кадрів незалежно від threshold).
- hash_size=12 — хороший баланс, 5% дає 13-24 кадри.
- hash_size=16 — найточніший, 5% дає 11-16 кадрів.
- **PiP mask суттєво впливає** на hash_size=12 (24→13 при 5%), менше на hash_size=16 (16→11).

**Cooldown:**
- Однаковий результат з/без cooldown (9 vs 9). Причина: у тестовому відео **немає live coding** — лектор показує слайди і вже написаний код, не набирає в реальному часі.
- Cooldown потрібно тестувати на відео з реальним live coding.

### Рекомендовані параметри для Production

| Параметр | Значення | Обґрунтування |
|---|---|---|
| **Extraction** | fps=0.5 | Баланс покриття/кількість. 2-год відео → 3600 кадрів → dHash → 50-100 |
| **hash_size** | 16 | Найточніший, різниця в швидкості мінімальна |
| **Threshold** | 5% (dist>12) | Для Spike B потрібно більше кадрів. Production може бути 10% |
| **PiP detection** | Temporal diff | Vision LLM не потрібен — temporal diff дає confidence 0.73 |
| **PiP mask** | Так | Зменшує false positives в 1.5-2x |
| **Cooldown** | 4 сек, 3 consecutive | Не впливає на цей тип відео, але потрібний для live coding |
| **Min resolution** | 1280 (720p) | Тестове відео 1280x720 — мінімально достатньо |

### Для Spike B

Golden frames: **17 кадрів** (fps=1.0, hash_size=16, threshold=5%, PiP mask). Timestamps покривають все відео від 0 до 634 сек. Достатньо для тестування Vision LLM на різних типах контенту (слайди, код, переходи).
