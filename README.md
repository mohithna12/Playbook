# Playbook

An ML-backed fantasy football decision copilot. Playbook imports your league,
projects player performance in raw stat space, and turns those projections into
lineup, trade, and playoff decisions you can actually audit.

> **Status: in development.** The data layer and API foundation are built and
> tested; the features that sit on top of them are not. See
> [Milestones](#milestones) for exactly what runs today.

## Why it is built this way

Four decisions shape most of the codebase:

**Projections live in raw stat space, not fantasy points.** The model predicts
receptions, yards, and touchdowns; points are derived per league at read time.
Half-PPR and TE-premium leagues change every downstream number, so a model
trained on points is a model trained on one league's rules.

**Correlated simulation, not independent sampling.** A quarterback and his
receiver do not have independent outcomes. Playoff odds computed by sampling
players independently are confidently wrong, so the simulator uses a Gaussian
copula over the correlation structure.

**Point-in-time correctness everywhere.** Every feature is built with an
`as_of` filter, so a model never trains on information that did not exist when
the decision was made. This is the difference between a backtest and a fantasy.

**Every number the LLM says is checked.** Explanations are generated against
retrieved context, and a numeric validator verifies each figure in the output
against that context before it is returned. Failures regenerate or fall back to
a deterministic template; the outcome is recorded so the claim is auditable
afterward rather than only at review time.

## Architecture

A modular Python monolith with three execution planes:

| Plane | Runs | Responsibility |
|---|---|---|
| API | FastAPI | Request handling, auth, reads. No business logic in routers. |
| Workers | ARQ + Redis | League import, simulation, explanation — anything slow or async. |
| Pipeline | Airflow | Ingest, feature engineering, training, batch inference. |

Postgres holds the shared state. Inference is batch-only — projections are
precomputed, never served from a live model.

The layering is enforced in CI by `import-linter`, not by convention:

```
api  ->  services  ->  repositories  ->  models
              \-> domain, sim, ml, llm
```

`domain` and `sim` are pure — no I/O, no SQLAlchemy. Routers hold no ORM
entities. Authorization lives in the repository layer, where every
league-scoped query joins through `league_memberships` on the caller's id, so
a handler that forgets a check returns nothing rather than someone else's
league.

## Stack

Python 3.11+ · FastAPI · SQLAlchemy 2.0 (async, `asyncpg`) · Alembic · Redis ·
ARQ · Airflow · XGBoost · NumPy/SciPy · Pydantic v2 · Next.js 15 + React 19 +
TypeScript · Terraform + EKS

## Getting started

Local development costs nothing and needs no cloud account — Postgres, Redis,
and an S3 stand-in all run in Docker.

```bash
cp .env.example .env      # defaults work as-is for local dev
make dev                  # install all dependencies
make docker-up            # start Postgres, Redis, LocalStack
make migrate              # apply migrations
uv run python scripts/seed_nfl_teams.py
make run                  # http://localhost:8000/docs
```

### Common commands

| Command | What it does |
|---|---|
| `make check` | Lint, `mypy --strict`, and the import-layering contracts |
| `make test-unit` | Unit tests (no I/O) |
| `make test-integration` | Integration tests (Postgres + Redis via testcontainers) |
| `make test-contract` | Schemathesis against the committed OpenAPI spec |
| `make openapi` | Regenerate `openapi/spec.json` and the frontend client |
| `make migrate` | Apply migrations |

Integration and contract tests use testcontainers, so they need a Docker
daemon. On a machine without one, point them at an already-running server:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user@host:5432/db \
TEST_REDIS_URL=redis://host:6379/0 \
  uv run pytest tests/integration
```

Those databases are dropped and rebuilt from scratch — never aim them at
anything you care about.

## Milestones

| | Milestone | State |
|---|---|---|
| M0 | Project skeleton, app factory, health probes | Done |
| M1 | Terraform (VPC, EKS, RDS, ElastiCache, S3) + Kubernetes | Done |
| M2 | Database schema, migrations, ORM models | Done |
| M3 | API foundation, Clerk auth, RFC 7807 errors, OpenAPI contract | Done |
| M4 | Async job system (ARQ, job lifecycle, SSE, reaper) | Next |
| M5–M7 | Sleeper import, scoring rule engine, ingest DAGs | Planned |
| M8–M10 | Feature store, training pipeline, batch inference | Planned |
| M11–M14 | Lineup optimizer, simulation, trade analyzer, explanations | Planned |
| M15–M18 | Frontend, observability, hardening | Planned |

## Repository layout

```
app/          Backend — api, services, domain, repositories, models,
              workers, ml, sim, llm
frontend/     Next.js application
infra/        Terraform modules and Kubernetes manifests
airflow/      DAGs and Great Expectations suites
alembic/      Migrations
tests/        unit, integration, contract, ml, load
docker/       Dockerfiles and the local compose stack
scripts/      Operational scripts
docs/         ADRs, runbooks, development guides
```

## Design notes

A few details that are easy to get wrong and are deliberate here:

- **`NUMERIC`/`Decimal` for all scoring arithmetic**, never `float`. Serialized
  as JSON numbers so generated clients match the schema.
- **`public.uuidv7()` primary keys**, schema-qualified. Postgres 18 ships a
  builtin `uuidv7()` in `pg_catalog`, which is searched ahead of `public`
  regardless of `search_path` — unqualified, key generation silently differs by
  server version. Sub-millisecond ordering via RFC 9562's increased-clock-
  precision method, so batch inserts stay ordered.
- **Season-partitioned time series** (`weekly_stats`, `feature_store`,
  `predictions`), with partition pruning verified by test rather than assumed.
- **Rate limiting fails open, caching fails open.** A Redis outage should make
  the API slow, not down.
- **Errors are `application/problem+json`** with a `trace_id` that also appears
  in the response header and every log line for the request, so a user-reported
  problem resolves to a single log query.

## Contributing

`make check` and `make test-unit` must pass before a commit; CI additionally
runs integration and contract tests and fails on an uncommitted OpenAPI diff.

## License

MIT — see [LICENSE](LICENSE).
