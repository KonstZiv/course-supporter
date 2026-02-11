# S1-002: Dev-інструменти та лінтинг

## Мета

Забезпечити єдиний стиль і якість коду автоматично — через pre-commit hooks при кожному коміті та зручні команди в Makefile. Один інструмент (ruff) замість п'яти.

## Що робимо

1. **Ruff (розширення):** додати правила ASYNC (async-помилки), S (security/bandit), PTH (pathlib), T20 (заборона print → structlog). Per-file-ignores для тестів
2. **Mypy (strict):** додати overrides для бібліотек без type stubs (trafilatura, python-pptx, PyMuPDF, whisper). Довести заглушки до strict-compatible
3. **Pre-commit:** `.pre-commit-config.yaml` — ruff lint+format, mypy, trailing whitespace, check-yaml/toml, захист від великих файлів (500KB)
4. **Makefile:** команди `install`, `lint`, `format`, `typecheck`, `test`, `check` (all-in-one)

## Очікуваний результат

- Розробник робить `make install` → все готово (deps + hooks)
- При коміті автоматично: format → lint → type check
- `make check` — повна перевірка одною командою
- Забуті `print()`, blocking calls в async, відсутні type hints — ловляться автоматично

## Контрольні точки

- [ ] `uv run ruff check src/ tests/` → 0 помилок
- [ ] `uv run ruff format --check .` → 0 змін
- [ ] `uv run mypy src/` → strict mode, 0 помилок
- [ ] `uv run pre-commit run --all-files` → все зелене
- [ ] `make check` → lint + typecheck + test проходять
- [ ] `check-added-large-files` блокує файли > 500KB

## Залежності

- **Блокується:** S1-001 (репозиторій, pyproject.toml з базовими dev deps)
- **Блокує:** нічого напряму, але має бути готова до початку Epic 2–3

## Деталі

Повний spec (конфігурації ruff/mypy/pre-commit, Makefile): **T002-dev-tools.md**
