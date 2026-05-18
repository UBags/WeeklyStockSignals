# Phase 12

Phase 12 addressed the Tier-3 review items from Gemini's deep-dive,
plus the carried-over pytest-qt teardown debt. The work landed as
six items:

## 1. pytest-qt teardown race fix

`tests/gui/conftest.py` rewritten with a `managed_widget` factory
fixture and an autouse `_drain_after_every_gui_test` that pumps
the event loop with deferred-deletion handling. Implements the
fix shape from Gemini's "Deep Dive: Technical Debt and Hardening"
analysis adapted to the existing test infrastructure.

**Honest scope:** improved standalone `tests/gui` reliability from
~3/5 to ~4-5/5 clean exits. The CI two-invocation pattern +
output-parsing wrapper remain necessary because some teardown
happens inside pytest's atexit handlers, after fixtures can
intervene.

## 2. PyInstaller GUI installer

New `gui_installer.spec` at the repo root, plus a
`build-gui-installer` job in `.github/workflows/release.yml` that
builds a Linux x86_64 binary on every tag push and uploads it as
a release artifact. Windows + macOS deferred until requested.

## 3. DB role separation

New Alembic migration `0002_db_roles.py` creates three Postgres
roles (`trip_etl_user`, `trip_engine_user`, `trip_gui_user`) with
per-table GRANTs. The config schema gained an optional
`database.roles:` block; absence preserves the existing single-user
behavior. See [`operator/db-roles.md`](../operator/db-roles.md).

## 5. DLQ for notifier worker

New Redis-backed dead-letter queue. After `notifier.max_attempts`
failures (default 5), the worker moves the job to the DLQ instead
of cycling it back into the main queue. Attempt counter persists
in Redis (HASH keyed by job_id) rather than on the payload, so it
survives the distributor recreating the payload each cycle.

Three new admin endpoints (`GET /v1/admin/notifications/dlq`,
`POST .../replay`, `DELETE .../{job_id}`) plus a Prometheus
counter label (`outcome="dlq"`). See
[`operator/dlq.md`](../operator/dlq.md).

## 6. MkDocs Material site

This site. Built from the existing markdown under `docs/` and the
root-level README + PHASE11_CHANGELOG. CI publishes to GitHub
Pages via `.github/workflows/docs.yml` on every push to main.

## 8. Alembic exclusivity (squash)

Legacy SQL files moved to `migrations/legacy/` with a README
explaining the archive. New squashed Alembic revision
`0001_initial_schema` is the canonical fresh-DB path; the bridge
auto-stamps it on pre-Phase-12 databases without re-running DDL.
`app.py` is now Alembic-exclusive — `apply_pending` removed from
the public API.

A parity test (`test_alembic_and_legacy_produce_same_tables`)
runs both paths against a fresh DB and asserts they produce the
same schema. Catches drift.

## Items NOT done

- **#7 — Real TradingView parity fixtures.** Requires TV Premium
  access on the operator's side. Harness has been ready since
  Phase 10.
- **Multi-user authn / RBAC.** Out of scope; the current
  bearer-token model is adequate for single-operator deployments.

## Test counts

- DB-free: **465** (up from 437 — 28 new tests across DLQ,
  worker DLQ, DB roles, and Alembic bridge)
- Postgres-backed: +1 schema-parity test
- Qt: 123 (unchanged)
