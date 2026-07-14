# `docs/changelog/` — Per-phase changelog files

One file per delivery phase. The phase is the project's unit of work —
each represents a chunk of design + implementation that ships together
and is reviewed as a unit.

## Contents

| File | Phase |
|---|---|
| `phase-11.md` | Phase 11 — ETL backend (NSE/BSE listings + bars), desktop operator console (PySide6), expanded admin API |
| `phase-12.md` | Phase 12 — pytest-qt teardown fix, Alembic exclusivity, DB role separation, notifier DLQ, PyInstaller GUI installer, MkDocs site |

## Conventions

- File name: `phase-NN.md` (or `phase-NN.M.md` for sub-phases like
  `14.6`)
- Frontmatter (when present) gives the phase date range, ship status,
  reviewer
- Body sections: motivation, what shipped, what's intentionally not
  shipped, follow-up items
- Code references use `path/to/file.py:LNN` so they survive renames
  better than line-only references
- Cross-references to invariants use the canonical number from
  `../architecture/invariants.md`

## Where each phase lives in code

| Phase | Code center of gravity |
|---|---|
| 1-8 | `trip_ema/indicators/`, `trip_ema/engine/`, `trip_ema/cache/`, `trip_ema/scheduler/` |
| 9 | `trip_ema/transports/http/`, `trip_ema/notifications/`, `trip_ema/config/` |
| 10 | `trip_ema/observability/` |
| 11 | `trip_ema/etl/`, `trip_ema/gui/`, `trip_ema/registry/` |
| 12 | `alembic_pkg/versions/0002_db_roles.py`, `tests/gui/conftest.py`, `mkdocs.yml`, `gui_installer.spec` |
| 14.6 | `trip_ema/persistence/exchange_state_repo.py`, `alembic_pkg/versions/0004_exchange_state.py` |
| 15a-d | `trip_ema/etl/signal_generator.py`, `trip_ema/config/exchange_overrides.py`, `trip_ema/gui/views/widgets/`, `docs/gui_endpoint_audit.md` |
| Fundamentals 1-8 | `trip_ema/fundamentals/`, `alembic_pkg/versions/0006_*` through `0015_*` |

## Repo-root supplements

Two files at the repo root act as the most-current cross-phase view:

- `01_FUNCTIONALITY.md` — current functional inventory across all
  phases delivered to date
- `02_ARCHITECTURE.md` — current architecture across all phases
- `03_OPEN_ITEMS.md` — explicitly-deferred work and roadmap for
  Phases 16-19+
