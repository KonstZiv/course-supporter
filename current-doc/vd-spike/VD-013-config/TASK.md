# VD-013: Config Env Vars

**Фаза:** 6 — Polish
**Пріоритет:** Низький
**Залежності:** VD-008

## Що робимо

Всі VD параметри конфігуруються через environment variables.

## Яким чином

Додати в config.py:
- VD_GEMINI_KEYS — key pool (comma-separated)
- VD_MODEL — model name (default: gemini-3.1-flash-lite-preview)
- VD_RPM_LIMIT — rate limit (default: 15)
- VD_FPS — frame extraction fps (default: 0.5)
- VD_HASH_SIZE — dHash hash_size (default: 16)
- VD_DHASH_THRESHOLD — dedup threshold (default: 0.05)
- VD_GAP_FILL_SEC — max gap for fill (default: 15.0)

Оновити .env.example.

## Acceptance criteria

- [ ] Всі параметри мають defaults
- [ ] .env.example оновлений
- [ ] Config validation (mypy strict)
