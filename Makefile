# PRISM monorepo — top-level developer tasks. Run from the repo root.
# Layout: services/*  (deployable services)   packages/*  (shared libraries)
.PHONY: help install lint type test fmt ci new-service

PKGS := packages/evam-backend-core packages/evam-register-client
SVCS := services/register

help:  ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install:  ## Install shared packages, then services (editable, with dev deps)
	python -m pip install --upgrade pip
	pip install -e packages/evam-backend-core
	pip install -e "packages/evam-register-client[dev]"
	pip install -e "services/register[dev]"
	pip install -e "services/workflows[dev]"
	@echo "Optional: pre-commit install"

lint:  ## Ruff lint every package + service
	python -m ruff check services/register/app services/register/scripts services/register/tests
	python -m ruff check services/workflows/app services/workflows/tests
	python -m ruff check packages/evam-backend-core/evam_backend_core packages/evam-backend-core/examples
	python -m ruff check packages/evam-register-client/evam_register_client packages/evam-register-client/tests

fmt:  ## Auto-format + auto-fix
	python -m ruff format services packages
	python -m ruff check --fix services packages

type:  ## Type-check everything (mypy)
	cd services/register && python -m mypy app
	cd services/workflows && python -m mypy app
	cd packages/evam-backend-core && python -m mypy evam_backend_core
	cd packages/evam-register-client && python -m mypy evam_register_client

test:  ## Run all test suites (needs a Postgres for the Register — see QUICKSTART/CONTRIBUTING)
	cd services/register && python -m pytest -q
	cd services/workflows && python -m pytest -q
	cd packages/evam-register-client && python -m pytest -q

ci: lint type test  ## Everything CI runs

new-service:  ## Scaffold a new service on evam-backend-core:  make new-service NAME=cipher
	python scripts/new_service.py $(NAME)
