# `docs/dev/` — Developer documentation

Internal docs for developers working *on* `trip-ema`. Distinguished
from `../operator/` (which is for people running the deployed service)
by audience.

## Contents

| File | Purpose |
|---|---|
| `migrations.md` | How to author Alembic migrations, the Alembic-vs-legacy-SQL parity requirement, the upgrade bridge for pre-Phase-12 DBs |
| `parity.md` | The Pine parity harness — what it asserts, how to regenerate fixtures from TradingView, why `1e-9` tolerance |
| `testing.md` | Test suite organization — DB-free vs DB-required vs Qt-required, how to run each slice, the two-invocation Qt pattern |
| `transports.md` | The HTTP transport layer's organization — router decomposition, dependency injection, schema patterns |

## When to read which

| Task | Doc |
|---|---|
| Adding a schema change | `migrations.md` |
| Touching `trip_ema/indicators/` or `trip_ema/engine/` | `parity.md` (the harness will catch you if you drift) |
| Writing tests, or wondering why CI splits them into two invocations | `testing.md` |
| Adding a new HTTP route | `transports.md` |

## Companion docs

- The 14 invariants in `../architecture/invariants.md` are the rules
  these docs explain how to follow.
- `../changelog/` documents what shipped in each phase; this directory
  documents how to do similar work in the future.
