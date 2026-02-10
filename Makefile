.PHONY: help install lint format typecheck test test-cov check all up down reset logs ps

help:  ## Показати цю довідку
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

install:  ## Встановити залежності та pre-commit hooks
	uv sync
	uv run pre-commit install

lint:  ## Перевірити код (ruff)
	uv run ruff check src/ tests/

format:  ## Форматувати код (ruff)
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

typecheck:  ## Перевірити типи (mypy)
	uv run mypy src/

test:  ## Запустити тести
	uv run pytest || test $$? -eq 5

test-cov:  ## Запустити тести з coverage
	uv run pytest --cov --cov-report=term-missing || test $$? -eq 5

check: lint typecheck test  ## Повна перевірка (lint + types + tests)

all: format check  ## Форматувати + повна перевірка

# --- Infrastructure ---

up:  ## Запустити інфраструктуру (PostgreSQL + MinIO)
	docker compose up -d
	@echo "Waiting for services..."
	@docker compose exec postgres pg_isready -U $${POSTGRES_USER:-course_supporter} > /dev/null 2>&1 && \
		echo "PostgreSQL: ready" || echo "PostgreSQL: waiting..."
	@echo "MinIO Console: http://localhost:9001"

down:  ## Зупинити інфраструктуру
	docker compose down

reset:  ## Зупинити та видалити всі дані (чистий рестарт)
	docker compose down -v

logs:  ## Показати логи сервісів
	docker compose logs -f

ps:  ## Статус сервісів
	docker compose ps
