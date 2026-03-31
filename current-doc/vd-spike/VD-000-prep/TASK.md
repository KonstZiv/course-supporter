# VD-000: Підготовка середовища

**Фаза:** 0 — Підготовка
**Пріоритет:** Критичний (блокує всі наступні таски)
**Залежності:** Немає

## Що робимо

Готуємо середовище для роботи з VD pipeline: встановлюємо залежності для обробки зображень, завантажуємо тестове відео, перевіряємо що інструменти працюють.

## Яким чином

1. **Додати залежності** у `pyproject.toml` як optional group `[vd]`:
   - `opencv-python-headless>=4.9`
   - `Pillow>=10.0`
   - `ImageHash>=4.3`

2. **Додати mypy overrides** для `cv2.*` та `imagehash.*` (ignore_missing_imports).

3. **Встановити:** `uv sync --extra vd`

4. **Завантажити тестове відео** через `yt-dlp`:
   - URL: `https://www.youtube.com/watch?v=bRYsA9Yyvy4`
   - Зберегти як `current-doc/vd-spike/sample.mp4`
   - Те саме відео що використовувалось у STT spike

5. **Smoke test:** Python-скрипт що відкриває відео через OpenCV, витягує один кадр, рахує dHash через imagehash, зберігає як JPEG.

## Результат

- `pyproject.toml` оновлений з `[vd]` optional group
- `mypy` overrides додані
- `current-doc/vd-spike/sample.mp4` — тестове відео
- Smoke test пройдений: OpenCV + Pillow + imagehash працюють разом

## Як перевіряємо

```bash
uv sync --extra vd
python -c "import cv2; import imagehash; from PIL import Image; print('OK')"
ls current-doc/vd-spike/sample.mp4  # файл існує
```
