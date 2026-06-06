.PHONY: help install lint format typecheck test test-cov check all run-api up down reset logs ps doctor migrate db-upgrade db-downgrade db-reset

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

# --- API ---

run-api:  ## Запустити API сервер (uvicorn --reload)
	uv run uvicorn course_supporter.api:app --reload

# --- Database ---

migrate:  ## Створити нову міграцію (autogenerate): make migrate msg="add_feedback_table"
	uv run alembic revision --autogenerate -m "$(msg)"

db-upgrade:  ## Застосувати міграції
	uv run alembic upgrade head

db-downgrade:  ## Відкатити останню міграцію
	uv run alembic downgrade -1

db-reset:  ## Повний ресет: downgrade до base + upgrade до head
	uv run alembic downgrade base
	uv run alembic upgrade head

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

doctor:  ## Pre-flight infra check (postgres/redis/minio up + alembic at head + pings)
	@fail=0; \
	running=$$(docker compose ps --status=running --services 2>/dev/null); \
	for svc in postgres redis minio; do \
		if echo "$$running" | grep -qw $$svc; then echo "$$svc: up"; \
		else echo "$$svc: DOWN"; fail=1; fi; \
	done; \
	head=$$(uv run alembic heads 2>/dev/null | head -1 | awk '{print $$1}'); \
	cur=$$(uv run alembic current 2>/dev/null | head -1 | awk '{print $$1}'); \
	if [ -n "$$head" ] && [ "$$cur" = "$$head" ]; then echo "alembic: $$cur (head)"; \
	else echo "alembic: MISMATCH cur='$$cur' head='$$head'"; fail=1; fi; \
	if docker compose exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; then echo "redis ping: PONG"; \
	else echo "redis ping: FAIL"; fail=1; fi; \
	if curl -fs http://localhost:9000/minio/health/live >/dev/null 2>&1; then echo "minio health: ok"; \
	else echo "minio health: FAIL"; fail=1; fi; \
	if [ $$fail -eq 0 ]; then echo "doctor: PASS"; else echo "doctor: FAIL"; fi; \
	exit $$fail

psql:  ## Відкрити psql shell у postgres контейнері
	docker compose exec postgres psql -U $${POSTGRES_USER:-course_supporter} -d $${POSTGRES_DB:-course_supporter}
