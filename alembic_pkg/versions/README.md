# `alembic_pkg/versions/` — Alembic revision files

One Python file per schema change, named `NNNN_<slug>.py`. Alembic
loads them, walks the `down_revision` chain, and applies pending
revisions when `alembic upgrade head` runs.

See `../README.md` for the catalogue of revisions with descriptions.

## Conventions

- Numeric prefix gives the **applied order** — `0001`, `0002`, etc.
  Alembic itself doesn't require numeric prefixes (it uses random
  hashes by default), but this project prefers a monotonic numeric
  scheme for human readability.
- The slug after the prefix names the feature: `0005_signal_backtest_runs.py`.
- Each revision sets `down_revision = "NNNN_<prev_slug>"` to form the
  linear chain.
- `upgrade()` and `downgrade()` are both required, though `downgrade()`
  is sometimes a no-op for irreversible changes (with a comment
  explaining why).
- Migrations that need a SQL body file load it from `../sql/`.

## Pairing with legacy SQL

Every revision here that creates or drops a table — or alters one in a
way that changes the table set — needs a matching parity file under
`../../migrations/legacy/NNN_*.sql`. The legacy-parity test
(`tests/persistence/test_legacy_migrator.py`) applies both chains
against fresh databases and asserts the resulting schemas match.

If a revision only changes data (no DDL), no parity file is needed.

## Failure semantics

`alembic upgrade head` is run at startup by `trip-ema`. A failure is
**fatal** (Phase 12 invariant). If you ship a revision that can't
apply cleanly to a representative production database, the service
won't start. Tier-3 operators need the `[migrations]` extra installed
(`pip install trip-ema[migrations]`) for this to work.

## Tests

- `tests/persistence/test_alembic_bridge.py` — verifies the legacy-DB
  stamping path (`stamp_baseline_if_needed()`) on pre-Phase-12 databases
- `tests/persistence/test_legacy_migrator.py` — runs both chains against
  fresh Postgres instances and asserts identical resulting schemas
- `tests/config/test_alembic_bridge_no_db.py` — pure-Python tests of
  the bridge's decision logic without needing a DB
