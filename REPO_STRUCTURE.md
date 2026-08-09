# Playbook Repository Structure

**Monorepo layout for the Playbook decision-support system.**

This document is the canonical reference for where code lives, who owns it, and how it is organized. Read this before creating a file.

---

## Top-Level Layout

```
playbook/
  app/                  # Python backend (FastAPI + workers + ML)
  frontend/             # Next.js web application
  infra/                # Terraform + Kubernetes manifests
  airflow/              # Airflow DAGs and plugins
  scripts/              # One-off and operational scripts
  tests/                # All backend tests (mirrors app/ structure)
  docs/                 # Architecture decisions, runbooks, guides
  .github/              # GitHub Actions workflows and templates
  docker/               # Dockerfiles and compose
```

---

## Directory Reference

### `app/` -- Python Backend

The backend is a modular monolith. Dependency direction is enforced by `import-linter` in CI:

```
api -> schemas, services
services -> domain, repositories, sim, ml, llm
repositories -> models, domain
domain -> (nothing, no I/O)
sim -> (numpy only, no I/O)
```

Violating these rules fails the build.

```
app/
  __init__.py
  main.py                        # FastAPI application factory, lifespan, middleware

  api/
    __init__.py
    deps.py                      # Dependency injection: get_db, get_redis, get_current_user
    v1/
      __init__.py
      router.py                  # Aggregates all v1 routers
      health.py                  # GET /health, GET /ready
      meta.py                    # GET /v1/meta/nfl-state, GET /v1/meta/models
      leagues.py                 # League CRUD + import trigger
      teams.py                   # Team + roster endpoints
      players.py                 # Player search + detail + projections + stats
      lineup.py                  # Optimal lineup + save draft
      trades.py                  # Trade analysis trigger + retrieval
      simulations.py             # Simulation trigger + retrieval
      explain.py                 # Explanation trigger + SSE stream
      jobs.py                    # Job status polling + SSE stream + cancel

  schemas/
    __init__.py
    common.py                    # Pagination, error envelope, job handle
    league.py                    # League request/response models
    team.py                      # Team + roster models
    player.py                    # Player + projection models
    lineup.py                    # Lineup recommendation models
    trade.py                     # Trade analysis models
    simulation.py                # Simulation result models
    explanation.py               # Explanation response models
    job.py                       # Job status models

  services/
    __init__.py
    league.py                    # League import/sync orchestration
    lineup.py                    # Lineup optimization service
    projection.py                # Projection retrieval + league scoring
    trade.py                     # Trade evaluation orchestration
    simulation.py                # Simulation dispatch
    explanation.py               # Explanation dispatch
    job.py                       # Job lifecycle management
    sleeper/
      __init__.py
      client.py                  # Async HTTP client with rate limiting
      adapter.py                 # LeagueProvider implementation
      normalizer.py              # Sleeper DTOs -> domain objects
      player_mapper.py           # Sleeper player ID -> canonical player_id

  domain/
    __init__.py
    scoring.py                   # apply_scoring() -- pure function, no I/O
    stat_line.py                 # StatLine model
    optimizer.py                 # ILP lineup optimizer -- pure, no I/O
    vorp.py                      # Value over replacement calculation
    schedule_strength.py         # Schedule strength scoring
    provider.py                  # LeagueProvider protocol definition
    types.py                     # Shared domain enums and types

  repositories/
    __init__.py
    base.py                      # Base repository with session management
    user.py
    league.py
    team.py
    player.py
    prediction.py
    lineup.py
    trade.py
    simulation.py
    job.py
    explanation.py

  models/
    __init__.py                  # Re-exports all ORM models for Alembic
    user.py                      # users, league_memberships
    league.py                    # leagues, teams, roster_entries, lineups
    player.py                    # players, player_external_ids, nfl_teams
    game.py                      # games, game_odds, game_weather
    stats.py                     # weekly_stats (partitioned)
    prediction.py                # predictions, prediction_history (partitioned)
    feature.py                   # feature_store (partitioned)
    ml.py                        # model_versions
    simulation.py                # simulation_results
    trade.py                     # trade_analyses
    recommendation.py            # recommendations
    job.py                       # jobs
    matchup.py                   # matchups
    transaction.py               # transactions
    injury.py                    # injury_reports
    sync.py                      # sync_errors
    explanation.py               # explanations

  workers/
    __init__.py
    config.py                    # ARQ worker settings, queue names
    league_import.py             # Full league import job
    league_sync.py               # Incremental sync job
    simulation.py                # Monte Carlo simulation job
    trade.py                     # Trade analysis job
    explanation.py               # LLM explanation job
    cache_warm.py                # Cache warming job
    reaper.py                    # Stale job reaper

  ml/
    __init__.py
    features/
      __init__.py
      builder.py                 # build_features(player_ids, season, week, as_of)
      manifest.py                # Feature set manifest + versioning
      usage.py                   # Usage/opportunity features
      efficiency.py              # Efficiency features
      team_context.py            # Team context features
      opponent.py                # Opponent features
      game_context.py            # Game context features
      weather.py                 # Weather features
      injury.py                  # Injury/availability features
      player_attrs.py            # Player attribute features
    training/
      __init__.py
      trainer.py                 # Per-position model training
      validation.py              # Walk-forward cross-validation
      hyperopt.py                # Optuna hyperparameter search
      evaluation.py              # Metrics: MAE, Spearman, pinball, PIT
      calibration.py             # Quantile calibration (isotonic)
    inference.py                 # Batch prediction pipeline
    play_probability.py          # Injury -> play probability model
    shap_explainer.py            # TreeSHAP computation
    registry.py                  # Model artifact S3 management + model_versions

  sim/
    __init__.py
    engine.py                    # Core Monte Carlo (pure NumPy, no I/O)
    correlation.py               # Gaussian copula correlation matrix
    marginals.py                 # Marginal distribution fitting
    availability.py              # Injury availability process
    horizon.py                   # Variance inflation over horizon

  llm/
    __init__.py
    pipeline.py                  # Orchestrator: classify -> retrieve -> call -> validate
    retriever.py                 # Per-intent structured retrieval
    tools.py                     # 6 read-only tool definitions
    validator.py                 # Numeric grounding validator
    templates.py                 # Deterministic template fallback
    provider.py                  # LLM provider abstraction
    prompts/
      __init__.py
      system.py                  # System prompt construction
      few_shot.py                # Few-shot examples per intent

  core/
    __init__.py
    config.py                    # Pydantic Settings (env-based config)
    auth.py                      # Clerk JWT verification + JWKS cache
    errors.py                    # RFC 7807 error responses
    telemetry.py                 # OpenTelemetry setup
    cache.py                     # Redis client wrapper
    rate_limit.py                # Sliding-window rate limiter
    metrics.py                   # Custom Prometheus collectors
    logging.py                   # structlog config + PII redaction
    sse.py                       # SSE response helpers
    circuit_breaker.py           # Circuit breaker implementation
```

**Purpose:** All backend application logic.
**Ownership:** Backend / ML engineer.
**Naming:** snake_case modules and functions. Classes are PascalCase. Constants are UPPER_SNAKE_CASE.

---

### `frontend/` -- Next.js Web Application

```
frontend/
  package.json
  tsconfig.json
  next.config.ts
  tailwind.config.ts
  .eslintrc.cjs
  .prettierrc

  public/
    favicon.ico
    robots.txt

  src/
    app/
      layout.tsx                 # Root layout (fonts, providers, nav shell)
      page.tsx                   # Landing / marketing page
      (auth)/
        sign-in/[[...sign-in]]/page.tsx
        sign-up/[[...sign-up]]/page.tsx
      (dashboard)/
        layout.tsx               # Authenticated layout with sidebar nav
        leagues/
          page.tsx               # League list
          import/page.tsx        # League import flow
          [leagueId]/
            page.tsx             # League dashboard
            standings/page.tsx
            matchups/page.tsx
            transactions/page.tsx
            playoffs/page.tsx    # Playoff odds
            simulations/page.tsx
        teams/
          [teamId]/
            roster/page.tsx
            lineup/page.tsx      # Lineup optimizer
        players/
          page.tsx               # Player search
          [playerId]/page.tsx    # Player detail
        trades/
          page.tsx               # Trade analyzer
          [analysisId]/page.tsx  # Trade result
        ask/page.tsx             # AI explanation chat
        jobs/[jobId]/page.tsx    # Job progress (fallback)

    components/
      ui/                        # Primitive UI components (buttons, cards, inputs)
      layout/
        Sidebar.tsx
        Header.tsx
        ErrorBoundary.tsx
      charts/
        ProjectionDistribution.tsx
        PlayoffTrajectory.tsx
        SeedDistribution.tsx
        TradeValueComparison.tsx
      lineup/
        DragDropLineup.tsx
        SlotCard.tsx
        LineupDiff.tsx
      trade/
        TradeBuilder.tsx
        TradeResult.tsx
      explain/
        ChatStream.tsx
        Citation.tsx
      league/
        StandingsTable.tsx
        MatchupCard.tsx
      player/
        PlayerCard.tsx
        StatTable.tsx
      job/
        JobProgress.tsx

    hooks/
      useAsyncJob.ts             # SSE + polling fallback for async jobs
      useLeague.ts               # League data queries
      useRoster.ts               # Roster data queries
      useProjection.ts           # Projection queries
      useAuth.ts                 # Clerk auth helpers

    lib/
      api/
        client.ts                # Generated from OpenAPI (openapi-fetch)
        types.ts                 # Generated from OpenAPI (openapi-typescript)
      auth/
        provider.tsx             # Clerk provider wrapper
      utils.ts                   # Formatting, date helpers

    stores/
      lineup-draft.ts            # Zustand store for lineup drag-and-drop state
      trade-builder.ts           # Zustand store for trade builder selections
```

**Purpose:** React-based web UI. Client-rendered behind auth; server components for the shell and public pages.
**Ownership:** Frontend engineer.
**Naming:** PascalCase for components and component files. camelCase for hooks, utils, and non-component files. kebab-case for CSS classes (Tailwind handles this).

---

### `infra/` -- Infrastructure as Code

```
infra/
  terraform/
    modules/
      vpc/                       # VPC, subnets, NAT, security groups
      eks/                       # EKS cluster, node groups, add-ons
      rds/                       # PostgreSQL RDS instance
      elasticache/               # Redis ElastiCache
      s3/                        # S3 buckets (raw, models, features, logs)
      alb/                       # ALB + ACM certificate
      iam/                       # IAM roles, policies, IRSA
      secrets/                   # Secrets Manager entries
      ecr/                       # ECR repositories
    environments/
      staging/
        main.tf
        variables.tf
        outputs.tf
        terraform.tfvars
        backend.tf               # S3 + DynamoDB state backend
      prod/
        main.tf
        variables.tf
        outputs.tf
        terraform.tfvars
        backend.tf

  k8s/
    base/                        # Kustomize base manifests
      kustomization.yaml
      namespace.yaml
      api/
        deployment.yaml
        service.yaml
        hpa.yaml
        pdb.yaml
      worker/
        deployment.yaml
        keda-scaledobject.yaml
      sim-worker/
        deployment.yaml
        keda-scaledobject.yaml
      pgbouncer/
        deployment.yaml
        service.yaml
        configmap.yaml           # pgbouncer.ini
      network-policies/
        default-deny.yaml
        allow-api-to-pgbouncer.yaml
        allow-api-to-redis.yaml
        allow-worker-to-pgbouncer.yaml
        allow-worker-to-redis.yaml
      monitoring/
        prometheus/
          values.yaml            # kube-prometheus-stack Helm values
        grafana/
          values.yaml
          dashboards/            # Dashboard JSON files
        adot-collector.yaml
        fluent-bit.yaml
      airflow/
        values.yaml              # Official Airflow Helm chart values
      external-secrets/
        cluster-secret-store.yaml
        external-secrets.yaml
      keda/
        values.yaml
    overlays/
      staging/
        kustomization.yaml       # Staging-specific patches (replica counts, resources)
      prod/
        kustomization.yaml       # Production patches
```

**Purpose:** All infrastructure definitions. Nothing runs without this.
**Ownership:** Platform / infrastructure engineer.
**Naming:** kebab-case for Kubernetes resource files. snake_case for Terraform files (HCL convention).

---

### `airflow/` -- Data Pipeline DAGs

```
airflow/
  dags/
    __init__.py
    ingest_nflverse.py           # Tue 04:00 ET -- PBP, snaps, participation -> S3 -> Postgres
    ingest_market_data.py        # Every 6h / hourly on game days -- odds, weather
    ingest_sleeper_reference.py  # Daily 05:00 ET -- player dimension, NFL state
    sync_all_leagues.py          # Hourly / 15-min game days -- fan out incremental syncs
    build_features.py            # Dataset-triggered by ingest_nflverse
    train_models.py              # Weekly Tue 08:00 ET
    generate_projections.py      # After build_features; also Wed/Fri/Sat/Sun
    warm_caches.py               # After generate_projections
    data_quality_checks.py       # After each ingest
    partition_maintenance.py     # Monthly -- create new season partitions, archive old

  plugins/
    __init__.py
    operators/
      __init__.py
      s3_to_postgres.py          # Custom operator: S3 parquet -> staging -> upsert
    hooks/
      __init__.py

  great_expectations/
    expectations/
      nflverse_weekly_stats.json
      nflverse_games.json
      market_data_odds.json
      market_data_weather.json
      feature_store_coverage.json
    checkpoints/
    uncommitted/                 # GE output artifacts (gitignored)

  config/
    airflow.cfg.override         # Non-secret Airflow config overrides
```

**Purpose:** All batch/scheduled data work. DAGs are the unit of deployment.
**Ownership:** Data / ML engineer.
**Naming:** snake_case for DAG files. DAG IDs match filenames exactly (e.g., `ingest_nflverse`).

---

### `scripts/` -- Operational and One-Off Scripts

```
scripts/
  seed_nfl_teams.py              # Seed 32 NFL teams into the database
  seed_sleeper_players.py        # Bulk load ~10K players from Sleeper player export
  backfill_historical.py         # Run ingest for historical seasons 2018-2025
  generate_openapi.py            # Export OpenAPI spec to openapi/spec.json
  generate_frontend_client.py    # Run openapi-typescript + openapi-fetch codegen
  create_season_partitions.py    # Create partitions for a new season
  run_backtest.py                # Run the walk-forward backtest harness
  estimate_correlation.py        # Fit correlation coefficients from historical residuals
  reconcile_players.py           # Run player identity resolution
  warm_all_caches.py             # Manually trigger cache warming for all leagues
```

**Purpose:** Runnable utilities that are not part of the application or DAGs. Each script is self-contained with `if __name__ == "__main__"` and argparse.
**Ownership:** Whoever needs them.
**Naming:** snake_case, verb-first (e.g., `seed_`, `backfill_`, `generate_`).

---

### `tests/` -- All Backend Tests

```
tests/
  __init__.py
  conftest.py                    # Shared fixtures: db session, redis, test client, auth override

  unit/
    __init__.py
    test_scoring_engine.py       # Golden-file tests -- 100% branch coverage required
    test_optimizer.py            # Property-based (Hypothesis)
    test_optimizer_golden.py     # Known edge cases
    test_simulation_determinism.py
    test_simulation_statistical.py
    test_simulation_invariants.py
    test_feature_builder.py
    test_as_of_leakage.py        # Inject leaky column, assert MAE doesn't improve
    test_feature_parity.py       # Production vs training snapshot
    test_vorp.py
    test_trade_analyzer.py
    test_numeric_validator.py
    test_template_fallback.py
    test_rate_limiter.py
    test_circuit_breaker.py
    test_scoring_rules_validation.py

  integration/
    __init__.py
    conftest.py                  # testcontainers: Postgres + Redis
    test_auth.py
    test_error_handling.py
    test_league_import.py
    test_job_lifecycle.py
    test_sse.py
    test_projection_serving.py
    test_lineup_api.py
    test_simulation_api.py
    test_trade_api.py
    test_explanation_api.py
    test_migrations.py           # Alembic upgrade/downgrade on clean DB
    test_query_counts.py         # Assert N+1 queries don't exist on hot paths

  contract/
    __init__.py
    test_sleeper_live.py         # Nightly contract test against live Sleeper API
    test_openapi_schema.py       # Schemathesis contract fuzzing

  ml/
    __init__.py
    test_backtest.py             # Walk-forward evaluation on holdout
    test_training_smoke.py       # Fixed-slice training, assert MAE regression < 3%
    test_explanation_golden.py   # 60 (question, context) pairs
    test_explanation_adversarial.py

  load/
    k6/
      sunday_spike.js            # Simulates Sunday morning traffic shape
      baseline.js                # Steady-state load
      simulation_burst.js        # Burst of simulation requests

  fixtures/
    sleeper/                     # Recorded Sleeper API responses (VCR-style)
    scoring/                     # Golden files: league configs + expected points
    explanations/                # Golden (question, context, expected_facts) sets
    features/                    # Test feature snapshots
```

**Purpose:** All backend test code. Mirrors `app/` structure. Tests never import from each other across directories.
**Ownership:** Same as the code they test.
**Naming:** `test_` prefix on all test files and functions. Fixture files use descriptive names without `test_` prefix.

---

### `docs/` -- Documentation

```
docs/
  architecture/
    decisions/                   # Architecture Decision Records (ADR)
      001-modular-monolith.md
      002-eks-over-ecs.md
      003-xgboost-over-deep-learning.md
      004-batch-inference-over-online.md
      005-postgres-feature-store.md
      006-arq-over-celery.md
      007-gaussian-copula-correlation.md
    system-context.md            # High-level architecture diagram
    data-flow.md                 # Data lineage and flow diagrams

  runbooks/
    api-5xx-spike.md
    database-connection-exhaustion.md
    projection-dag-failure.md
    sleeper-circuit-breaker-open.md
    llm-fallback-rate-spike.md
    simulation-calibration-drift.md
    rds-restore-procedure.md
    model-rollback.md
    redis-failure-recovery.md

  development/
    setup.md                     # Local development setup guide
    testing.md                   # How to run each test suite
    adding-a-feature.md          # Feature template checklist
    database-migrations.md       # Migration safety rules

  api/
    openapi-changelog.md         # Log of breaking API changes
```

**Purpose:** Living documentation. Runbooks are linked from alerts. ADRs are append-only.
**Ownership:** Whoever made the decision writes the ADR. Runbooks owned by the on-call.
**Naming:** kebab-case for filenames. ADRs are numbered sequentially.

---

### `docker/` -- Container Definitions

```
docker/
  Dockerfile.api                 # FastAPI app -- no XGBoost (~280 MB)
  Dockerfile.worker              # API deps + NumPy/SciPy + LLM SDK (~420 MB)
  Dockerfile.ml                  # Full ML stack: XGBoost, sklearn, pandas, SHAP (~900 MB)
  docker-compose.yml             # Local dev: Postgres, Redis, LocalStack, API, worker
  docker-compose.test.yml        # CI: adds testcontainers config
  .dockerignore
```

**Purpose:** Container build definitions and local dev orchestration.
**Ownership:** Platform engineer.
**Naming:** `Dockerfile.<target>` suffix matches the image name.

---

### `.github/` -- CI/CD and GitHub Configuration

```
.github/
  workflows/
    ci.yml                       # PR gate: lint, typecheck, unit, integration, contract, coverage
    ci-ml.yml                    # ML-specific: triggered on app/ml/ changes
    deploy.yml                   # CD: build, push ECR, deploy staging, smoke, approval, prod
    infra.yml                    # Terraform plan/apply with manual approval
    nightly.yml                  # Nightly: contract tests against live APIs, dependency audit

  PULL_REQUEST_TEMPLATE.md
  CODEOWNERS
  dependabot.yml
```

**Purpose:** Automation and repo governance.
**Ownership:** Platform engineer.

---

### Root Files

```
playbook/
  pyproject.toml                 # Python project config: dependencies, ruff, mypy, pytest, import-linter
  uv.lock                       # Pinned dependency lockfile
  alembic.ini                   # Alembic migration config
  Makefile                       # Developer convenience targets
  .editorconfig                  # Cross-editor formatting rules
  .gitignore
  .env.example                   # Documented env vars (never real secrets)
  .pre-commit-config.yaml        # Pre-commit hooks
  CLAUDE.md                      # Claude Code project instructions
  REPO_STRUCTURE.md              # This file
  openapi/
    spec.json                    # Committed OpenAPI 3.1 spec (CI-diffed)
  config/
    correlation_coefficients.yaml # Versioned simulation correlation config
```

---

## Naming Conventions

### Python (backend)

| Element | Convention | Example |
|---------|-----------|---------|
| Modules / files | `snake_case` | `league_import.py` |
| Functions | `snake_case` | `apply_scoring()` |
| Classes | `PascalCase` | `ScoringRules` |
| Constants | `UPPER_SNAKE_CASE` | `MAX_SIMULATIONS` |
| Type aliases | `PascalCase` | `PlayerId = int` |
| Pydantic models | `PascalCase`, suffixed by role | `LeagueResponse`, `TradeRequest` |
| SQLAlchemy models | `PascalCase`, singular | `League`, `Player`, `WeeklyStat` |
| Test functions | `test_<what>_<condition>_<expected>` | `test_scoring_ppr_bonus_applied` |
| DAG IDs | `snake_case`, match filename | `ingest_nflverse` |
| ARQ job names | `dot.separated` | `league.full_import` |
| Feature names | `snake_case` | `target_share_l3` |
| Redis keys | `prefix:entity:id:qualifier` | `cache:lineup:{tid}:{week}:{ver}` |

### TypeScript (frontend)

| Element | Convention | Example |
|---------|-----------|---------|
| Component files | `PascalCase.tsx` | `TradeBuilder.tsx` |
| Hook files | `camelCase.ts` | `useAsyncJob.ts` |
| Utility files | `camelCase.ts` | `utils.ts` |
| Components | `PascalCase` | `<StandingsTable />` |
| Hooks | `use` prefix, camelCase | `useLeague()` |
| Constants | `UPPER_SNAKE_CASE` | `API_BASE_URL` |
| Types/interfaces | `PascalCase` | `LineupSlot` |
| Route folders | `kebab-case` or `[paramName]` | `leagues/[leagueId]/` |

### Terraform

| Element | Convention | Example |
|---------|-----------|---------|
| Files | `snake_case.tf` | `main.tf`, `variables.tf` |
| Resources | `snake_case` | `aws_eks_cluster.main` |
| Variables | `snake_case` | `rds_instance_class` |
| Modules | `kebab-case` directories | `modules/eks/` |

### Kubernetes

| Element | Convention | Example |
|---------|-----------|---------|
| Resource files | `kebab-case.yaml` | `default-deny.yaml` |
| Resource names | `kebab-case` | `playbook-api` |
| Labels | `app.kubernetes.io/*` | `app.kubernetes.io/name: api` |

### Database

| Element | Convention | Example |
|---------|-----------|---------|
| Tables | `snake_case`, plural | `weekly_stats`, `predictions` |
| Columns | `snake_case` | `player_id`, `created_at` |
| Indexes | `idx_{table}_{columns}` | `idx_pred_lookup` |
| Constraints | `ck_{table}_{rule}` or `uq_{table}_{columns}` | `ck_quantile_order` |
| Enums | `snake_case` | `league_provider`, `job_status` |
| Partitions | `{table}_{value}` | `weekly_stats_2025` |
| Migrations | Alembic auto-generated revisions | `001_initial_schema.py` |

---

## Branching Strategy

### Trunk-Based Development with Short-Lived Feature Branches

This is a single-engineer project. The branching model reflects that.

```
main (protected)
  |
  +-- feat/m0-project-skeleton        # One branch per milestone or logical unit
  +-- feat/m2-database-schema
  +-- feat/sleeper-import
  +-- fix/scoring-ppr-bonus
  +-- infra/eks-keda-setup
  +-- ml/quantile-calibration
  +-- docs/adr-003
```

**Rules:**

1. `main` is always deployable. It is protected: no direct pushes. All changes go through PRs.
2. Branches are short-lived: merge within 1-3 days. Long-lived branches rot.
3. Branch naming: `{type}/{description}`
   - Types: `feat/`, `fix/`, `refactor/`, `infra/`, `ml/`, `docs/`, `test/`, `chore/`
   - Description: kebab-case, concise
4. Rebase onto `main` before merging (linear history, no merge commits).
5. Squash merge is acceptable for single-feature branches. Regular merge for multi-commit branches where history matters.
6. Delete branches after merge.

**Release model:** Continuous deployment from `main`. Every merge to `main` triggers the deploy pipeline (staging -> approval -> prod). No release branches — this is not a library.

**Hotfix:** Branch from `main`, fix, PR, merge. Same as any other change. The deploy pipeline handles urgency through the manual approval gate.

---

## Coding Standards

### Python

- **Target:** Python 3.12+
- **Type checking:** `mypy --strict`. All public functions have type annotations. No `Any` without a comment explaining why.
- **Async:** `async def` for all I/O-bound functions. Never mix sync and async database access in the same module.
- **Pydantic v2:** for all data validation boundaries (API schemas, config, JSONB deserialization, LLM structured output).
- **SQLAlchemy 2.0:** async engine with `asyncpg`. Use the 2.0-style `select()` / `Session.execute()` API, never the legacy `Query` API.
- **Error handling:** raise domain exceptions in services, catch and map to HTTP in routers. Never catch bare `Exception` except at the outermost boundary.
- **Imports:** absolute imports only (`from app.services.league import ...`). No relative imports.
- **No business logic in routers.** Routers validate (Pydantic), call services, serialize (Pydantic). That's it.
- **No SQLAlchemy in services.** Services call repositories, never raw queries.
- **No I/O in domain/ or sim/.** These modules are pure functions over data. This is enforced by import-linter.
- **Decimal for money and scoring.** `float` is acceptable in NumPy/ML contexts. Never `float` for fantasy point totals displayed to users.

### TypeScript

- **Target:** ES2022, strict mode
- **React:** functional components only. No class components.
- **State:** TanStack Query for server state. Zustand for the small amount of client state. No Redux.
- **Styling:** Tailwind CSS. No CSS-in-JS. No CSS modules.
- **Fetching:** generated client from OpenAPI spec. Never hand-write fetch calls.
- **No `any`.** Use `unknown` and narrow.

### SQL / Migrations

- **Every migration has a tested downgrade.**
- **`lock_timeout = 3s`** in every migration.
- **`CREATE INDEX CONCURRENTLY`** always.
- **Expand/contract pattern:** add nullable column -> deploy code that writes it -> backfill -> deploy code that reads it -> later, add NOT NULL.
- **No raw SQL in application code** except in `ml/features/` (complex CTEs) and analytics views, and those are always parameterized.

### General

- **No commented-out code** in committed files. Use version control.
- **No TODO without a linked issue.**
- **Fail loudly.** Silent failures (caught exception with `pass`, swallowed error, missing data treated as empty) are bugs.
- **Log at boundaries.** Log on entry to workers, on external API calls, on errors. Don't log inside tight loops.

---

## Formatting, Linting, and Testing Tools

### Python

| Tool | Purpose | Config Location |
|------|---------|----------------|
| **ruff** | Linting + formatting (replaces black, isort, flake8, pyupgrade) | `pyproject.toml [tool.ruff]` |
| **mypy** | Static type checking, strict mode | `pyproject.toml [tool.mypy]` |
| **import-linter** | Enforce module dependency rules | `pyproject.toml [tool.importlinter]` |
| **pytest** | Test runner | `pyproject.toml [tool.pytest.ini_options]` |
| **pytest-cov** | Coverage measurement | `pyproject.toml` |
| **pytest-asyncio** | Async test support | `pyproject.toml` |
| **hypothesis** | Property-based testing (optimizer, simulation) | In test files |
| **testcontainers** | Real Postgres + Redis in integration tests | `tests/integration/conftest.py` |
| **schemathesis** | OpenAPI contract fuzzing | `tests/contract/` |
| **pytest-benchmark** | Performance regression testing | Selected test files |
| **great-expectations** | Data quality in pipelines | `airflow/great_expectations/` |
| **pre-commit** | Git hook runner | `.pre-commit-config.yaml` |
| **uv** | Dependency management and virtual environments | `pyproject.toml`, `uv.lock` |

### TypeScript / Frontend

| Tool | Purpose | Config Location |
|------|---------|----------------|
| **eslint** | Linting (with Next.js + TypeScript rules) | `frontend/.eslintrc.cjs` |
| **prettier** | Formatting | `frontend/.prettierrc` |
| **typescript** | Type checking (`strict: true`) | `frontend/tsconfig.json` |
| **vitest** | Unit testing | `frontend/vitest.config.ts` |
| **playwright** | E2E testing | `frontend/playwright.config.ts` |
| **openapi-typescript** | TypeScript types from OpenAPI spec | `scripts/generate_frontend_client.py` |
| **openapi-fetch** | Type-safe API client from OpenAPI spec | `frontend/src/lib/api/` |

### Infrastructure

| Tool | Purpose |
|------|---------|
| **terraform fmt** | HCL formatting |
| **terraform validate** | Config validation |
| **tflint** | Terraform linting |
| **trivy** | Container image vulnerability scanning |
| **kubeconform** | Kubernetes manifest validation |
| **checkov** | Infrastructure security scanning |

---

## GitHub Actions Workflows

### 1. `ci.yml` -- PR Gate (every PR to main)

**Trigger:** `pull_request` to `main`
**Target:** < 8 minutes wall clock

```
Jobs:
  lint-and-typecheck:
    - ruff check + ruff format --check
    - mypy --strict app/
    - import-linter

  unit-tests:
    - pytest tests/unit/ --cov=app --cov-report=xml
    - Coverage gate: >= 85% on app/services/ and app/ml/
    - Coverage gate: 100% branch on app/domain/scoring.py

  integration-tests:
    - Start testcontainers (Postgres + Redis)
    - alembic upgrade head
    - pytest tests/integration/

  contract-tests:
    - Start API in test mode
    - schemathesis run against OpenAPI spec

  openapi-diff:
    - Generate spec, diff against committed spec
    - Fail if breaking change detected

  frontend-checks:
    - cd frontend && npm ci
    - eslint + prettier --check
    - tsc --noEmit
    - vitest run

  build-images:
    - Build all 3 Docker images (ARM64)
    - trivy scan, fail on HIGH+

  kubernetes-validate:
    - kubeconform on k8s/ manifests
```

### 2. `ci-ml.yml` -- ML Pipeline Checks (on app/ml/ changes)

**Trigger:** `pull_request` to `main` with paths `app/ml/**`

```
Jobs:
  ml-smoke-test:
    - Load fixed historical slice
    - Train on small subset
    - Assert MAE regression < 3% vs baseline
    - Assert feature-set version consistency
```

### 3. `deploy.yml` -- Continuous Deployment (on merge to main)

**Trigger:** `push` to `main`

```
Jobs:
  build-and-push:
    - Build ARM64 images
    - Tag with git SHA
    - Push to ECR

  deploy-staging:
    - Run Alembic migrations (K8s Job)
    - Rolling deploy to staging
    - Smoke tests against staging

  approve-prod:
    - Manual approval gate (GitHub Environment)

  deploy-prod:
    - Run Alembic migrations
    - Rolling deploy (maxSurge 1, maxUnavailable 0)
    - 10-minute error-rate watch
    - Auto-rollback on error-rate breach
```

### 4. `infra.yml` -- Infrastructure Changes

**Trigger:** `pull_request` with paths `infra/terraform/**`, manual dispatch

```
Jobs:
  plan:
    - terraform init
    - terraform validate
    - terraform plan -out=plan.tfplan
    - Post plan output as PR comment

  apply:
    - Manual approval (GitHub Environment)
    - terraform apply plan.tfplan
```

### 5. `nightly.yml` -- Nightly Checks

**Trigger:** `schedule: cron '0 6 * * *'` (06:00 UTC daily)

```
Jobs:
  contract-tests-live:
    - Run tests/contract/test_sleeper_live.py against real Sleeper API
    - Alert on failure (Slack), don't fail the build (upstream change != our bug)

  dependency-audit:
    - uv audit (Python)
    - npm audit (frontend)
    - Report findings
```

---

## Pre-Commit Hooks

Configured in `.pre-commit-config.yaml`:

1. **ruff** -- lint + format (fast, Rust-based)
2. **mypy** -- type check (only changed files for speed)
3. **import-linter** -- dependency rules
4. **detect-secrets** -- prevent accidental secret commits
5. **check-yaml** -- validate YAML files
6. **end-of-file-fixer** -- consistent file endings
7. **trailing-whitespace** -- clean whitespace

---

## Environment Variables

All configuration is via environment variables, loaded by `app/core/config.py` (Pydantic Settings).

Documented in `.env.example` with comments. Never commit real values. Secrets come from AWS Secrets Manager via External Secrets Operator in production, and from `.env` locally.
