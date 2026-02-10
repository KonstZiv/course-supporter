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
   git clone https://github.com/KonstZiv/course-supporter.git
   cd course-supporter
   uv sync
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
