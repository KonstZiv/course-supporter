# S1-001: Ініціалізація репозиторію

## Мета

Створити репозиторій `course-supporter` з робочим Python-середовищем, структурою директорій та інструментами розробки. Після виконання — будь-хто клонує репо, робить `uv sync` і одразу може працювати.

## Що робимо

1. **Репозиторій:** `uv init`, src-layout (`src/course_supporter/`), `.gitignore`, `.python-version` (3.13)
2. **Залежності:** `pyproject.toml` з усіма deps для Sprint 1 (fastapi, pydantic, sqlalchemy, LLM SDKs, ingestion libs). Whisper — окрема group `media`
3. **Структура директорій:** модулі-заглушки для `ingestion/`, `agents/`, `llm/`, `storage/`, `api/`, `models/` — відповідають архітектурі системи
4. **Конфігурація:** `.env.example` зі змінними для PostgreSQL (офіційний docker image), LLM API keys (Gemini, Anthropic, OpenAI, DeepSeek), MinIO
5. **Dev-інструменти:** ruff + mypy + pytest сконфігуровані в `pyproject.toml`
6. **README:** інструкція quick start (clone → uv sync → docker compose up → run)

## Очікуваний результат

Репозиторій на GitHub, де після `git clone && uv sync --all-extras`:
- Імпорт `import course_supporter` працює
- `uv run ruff check .` — 0 помилок
- `uv run pytest` — запускається (0 тестів, 0 failures)
- Структура директорій готова для паралельної роботи над Epic 2–6

## Контрольні точки

- [ ] `uv sync --all-extras` завершується без помилок
- [ ] `uv run python -c "from course_supporter import __version__; print(__version__)"` → `0.1.0`
- [ ] `uv run ruff check src/ tests/` → OK
- [ ] `uv run mypy src/` → OK (заглушки проходять strict mode)
- [ ] `uv run pytest` → session starts, 0 errors
- [ ] `.env.example` містить усі необхідні змінні
- [ ] README описує повний шлях від clone до запуску

## Деталі

Повний spec (структура директорій, pyproject.toml, версії залежностей): **S1-001-repo-init.md**

## Блокує

S1-002 (dev-інструменти), S1-003 (Docker Compose), S1-004 (конфігурація) — увесь Epic 1
