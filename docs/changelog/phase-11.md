# Phase 11 + Tier 2

The detailed changelog is in [`PHASE11_CHANGELOG.md`](../PHASE11_CHANGELOG.md)
at the repo root. Highlights:

## Phase 11 (initial delivery)

- ETL backend: NSE listings ingest, EoD bar refresh, parking, tier
  scheduling
- PySide6 operator console — 7 views (signals, subscribers, ETL
  status, notification log, settings, system health, chart)
- Expanded admin API — 10 new endpoints for ETL control,
  subscriber management, notification log queries

Delivered in 4 slices.

## Tier 2 (post-Phase-11 hardening)

- Version bump to 0.11.0
- GitHub Actions CI workflows (DB-free + Postgres integration +
  release)
- Alembic migration tooling alongside the legacy SQL migrator
- End-to-end integration test (bars → engine → cache → HTTP)
- Invariant 3.14 codifying Engine/ETL/GUI process boundaries
