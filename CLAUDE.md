# CLAUDE.md -- Project Instructions for Claude Code

## Project
FantasyAI: an ML-backed fantasy football decision copilot. Modular Python monolith (FastAPI + ARQ workers + Airflow) with a Next.js frontend.

## Repository Layout
- `app/` -- Python backend (FastAPI API, services, domain, repositories, ORM models, workers, ML, simulation, LLM)
- `frontend/` -- Next.js 15 + React 19 + TypeScript + Tailwind + TanStack Query
- `infra/` -- Terraform (AWS) + Kubernetes manifests (Kustomize)
- `airflow/` -- Airflow DAGs and Great Expectations suites
- `tests/` -- All backend tests (unit, integration, contract, ml, load)
- `docker/` -- Dockerfiles and docker-compose for local dev
- `scripts/` -- One-off operational scripts
- `docs/` -- ADRs, runbooks, development guides

## Architecture Rules (enforced by import-linter in CI)
- `api -> schemas, services` (routers contain NO business logic)
- `services -> domain, repositories, sim, ml, llm`
- `repositories -> models, domain`
- `domain -> (nothing, no I/O)` -- pure functions only
- `sim -> (numpy only, no I/O)` -- pure computation only
- No SQLAlchemy imports in services. No business logic in routers. No I/O in domain or sim.

## Key Technical Decisions
- **Scoring in raw stat space**, not fantasy points. Points derived per-league at read time.
- **Batch inference only** -- no online model serving. Predictions precomputed.
- **`as_of` discipline** -- all features use point-in-time correctness via timestamp filtering.
- **Gaussian copula** for correlated simulation, not independent sampling.
- **Numeric grounding validation** on all LLM output -- every number checked against retrieved context.

## Coding Standards
- Python 3.12+, `mypy --strict`, `ruff` for lint+format
- Async everywhere for I/O: `asyncpg`, `httpx`, `redis.asyncio`
- Pydantic v2 for all validation boundaries
- SQLAlchemy 2.0 style (`select()`, not `Query`)
- `NUMERIC`/`Decimal` for scoring arithmetic, never `float`
- `orjson` for JSON serialization
- Tests: `pytest` + `hypothesis` (property-based for optimizer/sim) + `testcontainers`

## Commands
- `make dev` -- install all dependencies
- `make check` -- run lint + typecheck + import-linter
- `make test-unit` -- run unit tests
- `make test-integration` -- run integration tests (needs docker services)
- `make run` -- start FastAPI dev server
- `make docker-up` -- start Postgres, Redis, LocalStack
- `make migrate` -- run Alembic migrations
- `make openapi` -- regenerate OpenAPI spec + frontend client

## File Naming
- Python: snake_case
- TypeScript components: PascalCase.tsx
- TypeScript hooks/utils: camelCase.ts
- Kubernetes: kebab-case.yaml
- Terraform: snake_case.tf
