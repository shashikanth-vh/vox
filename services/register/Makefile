.PHONY: help install dev migrate seed run test lint fmt docker-up docker-down postman clean

REGISTER_DB_PORT ?= 5432
export REGISTER_DB_HOST ?= 127.0.0.1

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "\033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install the shared platform package, then the Register (runtime + dev deps)
	pip install -e ../../packages/evam-backend-core
	pip install -e ".[dev]"

migrate:  ## Apply database migrations
	alembic upgrade head

seed:  ## Load reference data + ATLAS mock dataset
	python -m app.seed

run:  ## Run the API locally with autoreload
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test:  ## Run the test suite (needs a Postgres at REGISTER_DB_* / register_test db)
	pytest

lint:  ## Ruff + mypy
	ruff check app tests
	mypy app

fmt:  ## Auto-fix lint issues
	ruff check --fix app tests
	ruff format app tests

docker-up:  ## Build and start the full stack (Postgres + Register)
	docker compose -f ../deploy/compose/docker-compose.yml up --build -d

docker-down:  ## Stop the stack
	docker compose -f ../deploy/compose/docker-compose.yml down

postman:  ## Regenerate ALL Postman collections from the frozen OpenAPI contracts (repo-level)
	cd ../.. && python scripts/gen_postman.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache
