## Summary

<!-- What does this PR do? Keep it to 1-3 bullet points. -->

## Milestone

<!-- Which roadmap milestone does this belong to? e.g., M2: Database Schema -->

## Changes

<!-- List the key files changed and why. -->

## Testing

- [ ] Unit tests pass (`make test-unit`)
- [ ] Integration tests pass (`make test-integration`) -- if applicable
- [ ] Type checking passes (`make typecheck`)
- [ ] Import rules pass (`make imports`)
- [ ] No new `ruff` warnings

## Checklist

- [ ] No business logic in routers
- [ ] No SQLAlchemy in services
- [ ] No I/O in domain/ or sim/
- [ ] Migrations have tested downgrades
- [ ] Migrations use `lock_timeout` and `CREATE INDEX CONCURRENTLY`
- [ ] No hardcoded secrets
- [ ] OpenAPI spec updated if endpoints changed
