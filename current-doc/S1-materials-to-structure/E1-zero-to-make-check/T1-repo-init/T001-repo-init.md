# 📋 S1-001: Ініціалізація репозиторію

## Мета

Створити репозиторій проєкту з правильною структурою, залежностями та базовою конфігурацією. Після виконання — будь-хто може клонувати репо, виконати `uv sync` і отримати робоче Python-середовище.

## Контекст

Це перша задача проєкту. Від її якості залежить зручність роботи на весь час розробки. Структура директорій повинна відображати архітектуру системи з документів (Ingestion → Agents → API), а pyproject.toml — містити всі необхідні залежності для Sprint 1.

---

## Acceptance Criteria

- [x] GitHub-репозиторій `course-supporter` створено
- [x] `uv sync` встановлює всі залежності без помилок
- [x] `uv run python -c "import course_supporter"` працює
- [x] `uv run pytest` запускається (навіть з 0 тестів)
- [x] `uv run ruff check .` проходить без помилок
- [x] Структура директорій відповідає архітектурі системи
- [x] README містить інструкції для швидкого старту

---

## Структура директорій

```
course-supporter/
│
├── src/
│   └── course_supporter/
│       ├── __init__.py              # version, package metadata
│       ├── config.py                # (заглушка, деталі — S1-004)
│       │
│       ├── models/                  # Pydantic schemas
│       │   ├── __init__.py
│       │   ├── course.py            # Course, Module, Lesson, Concept, Task
│       │   ├── source.py            # SourceMaterial, SourceDocument, ContentChunk
│       │   └── llm.py               # LLMCall, LLMResponse
│       │
│       ├── ingestion/               # Source processors
│       │   ├── __init__.py
│       │   ├── base.py              # SourceProcessor ABC
│       │   ├── video.py             # VideoProcessor
│       │   ├── presentation.py      # PresentationProcessor
│       │   ├── text.py              # TextProcessor
│       │   ├── web.py               # WebProcessor
│       │   └── merge.py             # MergeStep
│       │
│       ├── agents/                  # AI agents
│       │   ├── __init__.py
│       │   └── architect.py         # ArchitectAgent
│       │
│       ├── llm/                     # Multi-model infrastructure
│       │   ├── __init__.py
│       │   ├── router.py            # ModelRouter
│       │   ├── providers.py         # LLMProvider implementations
│       │   └── schemas.py           # Provider-level schemas
│       │
│       ├── storage/                 # DB layer
│       │   ├── __init__.py
│       │   └── repositories.py      # CRUD repositories
│       │
│       └── api/                     # FastAPI endpoints
│           ├── __init__.py
│           └── routes/
│               ├── __init__.py
│               └── courses.py       # Course endpoints
│
├── config/
│   └── models.yaml                  # Model registry (заглушка)
│
├── prompts/
│   └── architect/
│       └── v1.yaml                  # (заглушка)
│
├── migrations/
│   └── ...                          # Alembic (створюється в S1-005)
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                  # Shared fixtures
│   ├── unit/
│   │   └── __init__.py
│   └── evals/
│       └── __init__.py
│
├── scripts/
│   └── eval_architect.py            # (заглушка для S1-031)
│
├── .github/
│   └── workflows/
│       └── ci.yaml                  # (заглушка для S1-006)
│
├── .env.example
├── .gitignore
├── .python-version                  # 3.13
├── pyproject.toml
├── README.md
└── docker-compose.yaml              # (заглушка для S1-003)
```

> Усі файли, позначені як "заглушка", містять мінімальний валідний контент (порожні класи, TODO-коментарі). Вони будуть наповнені у відповідних задачах.

---

## pyproject.toml

```toml
[project]
name = "course-supporter"
version = "0.1.0"
description = "AI-powered course structuring and mentoring system"
requires-python = ">=3.13"
dependencies = [
    # API
    "fastapi[standard]>=0.128",
    "pydantic>=2.12",
    "pydantic-settings>=2.12",

    # LLM Providers
    # DeepSeek використовує OpenAI-сумісний API — окремий SDK не потрібен,
    # працюємо через openai.OpenAI(base_url="https://api.deepseek.com")
    "google-genai>=1.12",
    "anthropic>=0.49",
    "openai>=1.68",

    # Database
    "sqlalchemy[asyncio]>=2.0.37",
    "psycopg[binary]>=3.2",
    "alembic>=1.14",
    "pgvector>=0.4",
    "uuid-utils>=0.9",

    # Ingestion: presentations
    "python-pptx>=1.0",
    "pymupdf>=1.25",
    "python-docx>=1.1",

    # Ingestion: web
    "trafilatura>=2.0",
    "beautifulsoup4>=4.13",

    # Observability
    "structlog>=25.1",

    # Config
    "pyyaml>=6.0",
]

[project.optional-dependencies]
# Whisper тягне PyTorch (~2 GB), виносимо окремо
media = [
    "openai-whisper>=20240930",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.25",
    "pytest-cov>=6.0",
    "ruff>=0.9",
    "mypy>=1.14",
    "pre-commit>=4.1",
    "httpx>=0.28",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/course_supporter"]

[tool.ruff]
target-version = "py313"
line-length = 88
src = ["src"]

[tool.ruff.lint]
select = [
    "E",      # pycodestyle errors
    "W",      # pycodestyle warnings
    "F",      # pyflakes
    "I",      # isort
    "N",      # pep8-naming
    "UP",     # pyupgrade
    "B",      # flake8-bugbear
    "SIM",    # flake8-simplify
    "RUF",    # ruff-specific
]

[tool.mypy]
python_version = "3.13"
strict = true
plugins = ["pydantic.mypy"]

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.coverage.run]
source = ["src/course_supporter"]
```

---

## .gitignore

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
dist/
build/
.venv/

# Environment
.env
.env.local

# IDE
.idea/
.vscode/
*.swp

# OS
.DS_Store
Thumbs.db

# Project
*.mp4
*.mp3
*.wav
data/
uploads/
```

---

## .env.example

```env
# === LLM API Keys ===
GEMINI_API_KEY=your-gemini-api-key
ANTHROPIC_API_KEY=your-anthropic-api-key
OPENAI_API_KEY=your-openai-api-key
DEEPSEEK_API_KEY=your-deepseek-api-key
# DeepSeek використовує OpenAI-сумісний API:
# openai.OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

# === PostgreSQL (docker image: pgvector/pgvector:pg17) ===
POSTGRES_USER=course_supporter
POSTGRES_PASSWORD=secret
POSTGRES_DB=course_supporter
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
# Composed URL for SQLAlchemy (assembled in config.py from individual vars)
# DATABASE_URL=postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}

# === Storage (MinIO — S3-compatible) ===
S3_ENDPOINT=http://localhost:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=course-materials

# === App ===
LOG_LEVEL=DEBUG
ENVIRONMENT=development
```

---

## .python-version

```
3.13
```

---

## README.md

```markdown
# Course Supporter

AI-powered system for transforming course materials into structured learning plans with automated mentoring.

## Quick Start

### Prerequisites
- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- Docker & Docker Compose

### Setup

1. Clone and install dependencies:
   ```bash
   git clone https://github.com/<org>/course-supporter.git
   cd course-supporter
   uv sync --all-extras
   ```

2. Copy environment config:
   ```bash
   cp .env.example .env
   # Fill in your API keys
   ```

3. Start infrastructure:
   ```bash
   docker compose up -d
   ```

4. Run migrations:
   ```bash
   uv run alembic upgrade head
   ```

5. Start the API:
   ```bash
   uv run uvicorn course_supporter.api:app --reload
   ```

### Development

```bash
# Run tests
uv run pytest

# Lint & format
uv run ruff check .
uv run ruff format .

# Type check
uv run mypy src/
```

## Architecture

See `docs/` for detailed architecture documentation.
```

---

## Файли-заглушки

### src/course_supporter/__init__.py

```python
"""AI-powered course structuring and mentoring system."""

__version__ = "0.1.0"
```

### src/course_supporter/config.py

```python
"""Application configuration. TODO: implement in S1-004."""
```

### Усі інші __init__.py

Порожні файли. Модулі-заглушки (video.py, architect.py тощо) містять:

```python
"""<Module description>. TODO: implement in S1-0XX."""
```

---

## Кроки виконання

1. Створити GitHub-репозиторій
2. `uv init course-supporter && cd course-supporter`
3. Налаштувати pyproject.toml (копіювати з цього документа)
4. Створити структуру директорій та файли-заглушки
5. `uv sync --all-extras` — переконатись, що залежності встановлюються
6. Створити .gitignore, .env.example, .python-version, README.md
7. `uv run ruff check .` — переконатись, що лінтер проходить
8. `uv run pytest` — переконатись, що тести запускаються
9. Initial commit + push

---

## Примітки

- Версії залежностей у pyproject.toml — мінімальні compatible-release (`>=`), станом на лютий 2026. Lockfile (`uv.lock`) фіксує точні версії.
- **DeepSeek** не потребує окремого SDK — його API повністю сумісний з OpenAI SDK. Використовуємо `openai.OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")`. Це спрощує інтеграцію: `DeepSeekProvider` у нашому `ModelRouter` — це фактично `OpenAIProvider` з іншим `base_url`.
- **openai-whisper** винесений у окрему dependency group `[project.optional-dependencies] media`, бо тягне PyTorch (~2 GB). Встановлюється через `uv sync --extra media`. Для CI — мокані відповіді, без реального Whisper.
- **fastapi[standard]** включає uvicorn, httptools та інші runtime-залежності — окремо вказувати uvicorn не потрібно.
- **PostgreSQL** — використовуємо образ `pgvector/pgvector:pg17` (PostgreSQL 17 з pgvector). Змінні `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` передаються напряму в docker-compose і в Pydantic Settings.
- Структура `src/course_supporter/` — src layout для коректного пакування та імпортів.
