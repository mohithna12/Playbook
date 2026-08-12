.PHONY: help install dev lint typecheck imports test test-unit test-integration test-contract test-ml format check migrate migrate-down migrate-new run run-worker run-sim-worker run-reaper docker-up docker-down openapi frontend-install frontend-dev frontend-build clean

PYTHON := uv run
PYTEST := $(PYTHON) pytest

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# =============================================================================
# Setup
# =============================================================================

install: ## Install production dependencies
	uv sync

dev: ## Install all dependencies (including dev, worker, ml)
	uv sync --all-extras

# =============================================================================
# Code Quality
# =============================================================================

lint: ## Run ruff linter
	$(PYTHON) ruff check app/ tests/ airflow/ scripts/

typecheck: ## Run mypy strict type checking
	$(PYTHON) mypy app/

imports: ## Check import-linter dependency rules
	$(PYTHON) lint-imports

format: ## Format code with ruff
	$(PYTHON) ruff format app/ tests/ airflow/ scripts/
	$(PYTHON) ruff check --fix app/ tests/ airflow/ scripts/

check: lint typecheck imports ## Run all code quality checks

# =============================================================================
# Testing
# =============================================================================

test: ## Run all tests
	$(PYTEST) tests/ -v

test-unit: ## Run unit tests only
	$(PYTEST) tests/unit/ -v

test-integration: ## Run integration tests (requires Docker services)
	$(PYTEST) tests/integration/ -v

test-contract: ## Run contract tests
	# The schemathesis pytest plugin is disabled globally in pyproject.toml
	# (it hooks every collection); contract tests are where it earns its keep.
	$(PYTEST) tests/contract/ -v -p schemathesis

test-ml: ## Run ML pipeline tests
	$(PYTEST) tests/ml/ -v

test-cov: ## Run tests with coverage report
	$(PYTEST) tests/unit/ tests/integration/ --cov=app --cov-report=html --cov-report=term

# =============================================================================
# Database
# =============================================================================

migrate: ## Run all pending migrations
	$(PYTHON) alembic upgrade head

migrate-down: ## Rollback one migration
	$(PYTHON) alembic downgrade -1

migrate-new: ## Create a new migration (usage: make migrate-new MSG="add users table")
	$(PYTHON) alembic revision --autogenerate -m "$(MSG)"

# =============================================================================
# Run
# =============================================================================

run: ## Start the FastAPI development server
	$(PYTHON) uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

run-worker: ## Start the ARQ worker
	$(PYTHON) arq app.workers.config.WorkerSettings

run-reaper: ## Start the job reaper (reconciles stale jobs)
	$(PYTHON) arq app.workers.config.ReaperSettings

run-sim-worker: ## Start the simulation worker
	$(PYTHON) arq app.workers.config.SimWorkerSettings

# =============================================================================
# Docker
# =============================================================================

docker-up: ## Start local dev services (Postgres, Redis, LocalStack)
	docker compose -f docker/docker-compose.yml up -d

docker-down: ## Stop local dev services
	docker compose -f docker/docker-compose.yml down

docker-build: ## Build all Docker images
	docker build -f docker/Dockerfile.api -t playbook-api .
	docker build -f docker/Dockerfile.worker -t playbook-worker .
	docker build -f docker/Dockerfile.ml -t playbook-ml .

# =============================================================================
# OpenAPI
# =============================================================================

openapi: ## Generate OpenAPI spec and frontend client
	$(PYTHON) scripts/generate_openapi.py
	$(PYTHON) scripts/generate_frontend_client.py

# =============================================================================
# Frontend
# =============================================================================

frontend-install: ## Install frontend dependencies
	cd frontend && npm install

frontend-dev: ## Start frontend dev server
	cd frontend && npm run dev

frontend-build: ## Build frontend for production
	cd frontend && npm run build

frontend-lint: ## Lint frontend code
	cd frontend && npm run lint

frontend-typecheck: ## Type-check frontend code
	cd frontend && npx tsc --noEmit

# =============================================================================
# Cleanup
# =============================================================================

clean: ## Remove build artifacts and caches
	rm -rf __pycache__ .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
