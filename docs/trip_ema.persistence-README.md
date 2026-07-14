# `trip_ema/persistence/` — SQLAlchemy Core repositories

## 1. Role in the larger application

`persistence/` is the **single layer** that touches Postgres. Every
SQL query, every transaction, every connection pool lives here.
Layers above (`orchestrator`, `notifications`, `gui`, etc.) pass
through repository classes — they never construct `text(...)`
statements or open connections directly.

Three core principles:

1. **Connection-injected.** Repositories take a `Connection` in
   their constructor, never an `Engine`. The caller owns the
   transaction lifecycle.
2. **Domain objects out.** Every read returns a frozen `domain.*`
   dataclass or a frozen `*Record` dataclass — never tuples,
   never SQLAlchemy `Row` objects.
3. **Query shapes only.** Repositories are SQL plus the
   marshalling around it. Business logic (sequencing, retries,
   per-ticker fan-out) lives in `orchestrator`, `etl`,
   `fundamentals`, `scheduler`.

```mermaid
flowchart TB
    app[Application code] --> trans[transaction context manager]
    trans --> conn[Connection]
    conn --> repo[Repository class]
    repo --> text[text\(...\) / Core stmts]
    text --> pg[(Postgres)]

    cfg[config.DatabaseConfig] --> engine[create_engine_from_config]
    engine --> pool[QueuePool]
    pool --> conn
```

## 2. Architecture and constraints

| Rule | Why |
|------|-----|
| **Repos take Connection, not Engine** | Caller controls transaction. A repository call inside `with transaction(engine) as conn:` joins the caller's transaction; a repo call inside its own `with engine.connect()` would COMMIT independently and break atomicity |
| **Domain dataclasses out** | Layers above can pickle, log, and compare repository results without knowing about SQLAlchemy. Tests can construct expected values without DB fixtures |
| **SQLAlchemy Core, not ORM** | We want immutable domain dataclasses, not tracked rows whose mutation has side effects. Core lets us hand-roll column lists and stay out of identity-map territory |
| **Phase-12 role separation** | `connection.py` creates per-process engines: ETL role for `trip-ema-etl`, engine role for the service, GUI role for the desktop console. Each role has minimum privileges granted by Alembic migration `0002_db_roles.py` |
| **Alembic-exclusive migrations** | Legacy SQL runner (`migrator.py`) preserved for history only. `alembic_bridge.py` stamps pre-Phase-12 databases at the squashed baseline so they don't replay history |
| **UPSERT-by-default on writers** | Every write target uses `INSERT ... ON CONFLICT DO UPDATE`. Re-running stages is safe; cycle idempotency rests on this |

## 3. File inventory

```
trip_ema/persistence/
├── __init__.py                              Re-exports
├── connection.py                            Engine factory + transaction context manager
├── tables.py                                SQLAlchemy Core Table definitions
├── migrator.py                              (legacy SQL runner, kept for history)
├── alembic_bridge.py                        stamp_baseline_if_needed + run_alembic_upgrade_head
│
├── bars_repo.py                             ohlcv_bars
├── tickers_repo.py                          tickers
├── symbol_history_repo.py                   symbol_history (ISIN-based renames)
├── corporate_actions_repo.py                corporate_actions
├── exchange_state_repo.py                   exchange_state (COALESCE writes)
│
├── current_signals_repo.py                  current_signals (≤1 row/ticker)
├── historical_signals_repo.py               historical_signals (append-only)
├── signal_backtest_runs_repo.py             signal_backtest_runs (resume tracking)
│
├── stock_indicator_state_repo.py            stock_indicator_state
├── stock_indicator_enrichment_repo.py       stock_indicator_enrichment
├── industry_indicator_state_repo.py         industry_indicator_state
├── industry_indicator_enrichment_repo.py    industry_indicator_enrichment
│
├── fundamentals_repo.py                     8 fundamentals repos in one file
├── subscribers_repo.py                      subscribers + subscriptions + notification_log
├── notification_log_repo.py                 notification_log (separated write path)
├── market_overview_repo.py                  market_overview_daily reads
│
├── compute_runs_repo.py                     compute_runs (activity log for ticks + backfills)
├── backfill_runs_repo.py                    backfill_runs (single-active marker)
└── backfill_queries.py                      Read-side queries for GUI backfill panel
```

## 4. `connection.py` — engine factory + transaction

### 4.1 `build_db_url(config, role='app') -> str`

```python
def build_db_url(config: Config, role: str = "app") -> str:
    cfg = config.database
    if role == "app" or cfg.roles is None:
        user, pwd = cfg.user, resolve_db_password(config)
    elif role == "etl" and cfg.roles.etl:
        user, pwd = cfg.roles.etl.user, os.environ[cfg.roles.etl.password_env]
    elif role == "engine" and cfg.roles.engine:
        user, pwd = cfg.roles.engine.user, os.environ[cfg.roles.engine.password_env]
    elif role == "gui" and cfg.roles.gui:
        user, pwd = cfg.roles.gui.user, os.environ[cfg.roles.gui.password_env]
    else:
        user, pwd = cfg.user, resolve_db_password(config)   # fallback

    return f"postgresql+psycopg://{user}:{pwd}@{cfg.host}:{cfg.port}/{cfg.database}"
```

`role` selects the credential pair to embed. When `config.database.roles`
is unset (pre-Phase-12 deploys), every role falls back to the
top-level `(user, password_env)` — backward compatible.

### 4.2 `create_engine_from_config(config, role='app') -> Engine`

Builds the SQLAlchemy `Engine` with pool settings:

```python
create_engine(
    url,
    pool_size=cfg.pool_size,                  # default 5
    max_overflow=cfg.pool_max_overflow,       # default 10
    pool_timeout=cfg.pool_timeout_seconds,    # default 30
    pool_recycle=cfg.pool_recycle_seconds,    # default 3600
    pool_pre_ping=True,
)
```

`pool_pre_ping=True` issues a cheap `SELECT 1` before checking out a
connection — protects against managed-DB load balancers that
silently kill idle connections.

`pool_recycle=3600` proactively recycles connections older than an
hour. Belt-and-braces against the same load-balancer issue.

### 4.3 `transaction(engine) -> contextmanager`

```python
@contextmanager
def transaction(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as conn:
        yield conn
```

The canonical commit/rollback boundary used everywhere in the codebase:

```python
from trip_ema.persistence import transaction

with transaction(db_engine) as conn:
    repo = StockIndicatorStateRepository(conn)
    repo.upsert(state)
    # commits on context exit; rolls back on exception
```

`engine.begin()` opens a transaction, yields a connection, commits
on clean exit, rolls back on exception. Repositories called within
the `with` block see the same connection / same transaction.

## 5. `tables.py` — Core Table definitions

`tables.py` declares every `Table(...)` mirroring the Alembic
migration SQL one-to-one. The tables are used to **build queries**,
not to create the schema — Alembic owns DDL.

Pattern:

```python
ohlcv_bars = Table(
    "ohlcv_bars", _metadata,
    Column("ticker_id",     BigInteger, nullable=False),
    Column("timeframe",     Text,       nullable=False),
    Column("bar_timestamp", DateTime(timezone=True), nullable=False),
    Column("open",          Float,      nullable=False),
    Column("high",          Float,      nullable=False),
    Column("low",           Float,      nullable=False),
    Column("close",         Float,      nullable=False),
    Column("volume",        Float,      nullable=False),
    Column("adj_close",     Float),
    Column("updated_at",    DateTime(timezone=True), server_default=func.now()),
    PrimaryKeyConstraint("ticker_id", "timeframe", "bar_timestamp"),
)
```

**Keeping these in sync with migrations is manual discipline.** A
mismatch shows up as a runtime error (column not found). The CI
tests in `tests/persistence/test_tables_match_migrations.py` walks
both and asserts identical column names + types.

## 6. Repository catalog — by table

### 6.1 OHLCV and instrument reference

| Repo | Tables | Key methods |
|------|--------|-------------|
| `BarsRepository` | `ohlcv_bars` | `fetch_history(ticker_id, timeframe, limit)`, `fetch_since(ticker_id, tf, since)`, `upsert_bar(...)`, `weekly_buckets_with_bars(ticker_id, tf, since, tz)` |
| `TickersRepository` | `tickers` | `get_by_id(id)`, `get_by_ref(ref)`, `list_active(exchange)`, `upsert(...)` |
| `SymbolHistoryRepository` | `symbol_history` | `record_rename(ticker_id, old, new, effective_date)` |
| `CorporateActionsRepository` | `corporate_actions` | `insert_split(...)`, `insert_dividend(...)`, `mark_candidate_revision(...)`, `find_pending_review()` |

### 6.2 Indicator state (V1)

| Repo | Table | Key methods |
|------|-------|-------------|
| `StockIndicatorStateRepository` | `stock_indicator_state` | `upsert(state)`, `get_history_for_ticker(id, tf, to_dt=None, limit=N)`, `get_latest_for_ticker(id, tf)`, `delete_for_ticker_from(id, tf, from_dt)` |
| `StockIndicatorEnrichmentRepository` | `stock_indicator_enrichment` | `upsert(enrichment)`, `get_for_ticker_bar(id, tf, bar_ts)`, `delete_for_ticker(id)` |
| `IndustryIndicatorStateRepository` | `industry_indicator_state` | Same shape as stock side but keyed by `industry_name` |
| `IndustryIndicatorEnrichmentRepository` | `industry_indicator_enrichment` | Same shape |

The V1 split (state + enrichment) is mirrored in the repos. State
tables have stable 12-column shape; enrichment tables hold the
evolving 23-column derived metrics.

### 6.3 Signals (V1)

| Repo | Table | Key methods |
|------|-------|-------------|
| `CurrentSignalsRepository` | `current_signals` (≤1 row/ticker) | `get_for_ticker(id)`, `list_all()`, `insert_many([signals])`, `upsert(signal)`, `delete_for_ticker(id)`, `count()` |
| `HistoricalSignalsRepository` | `historical_signals` (append-only) | `insert(signal)`, `get_for_ticker(id, limit)`, `delete_for_tickers_from(ids, from_ts)` |
| `SignalBacktestRunsRepository` | `signal_backtest_runs` | `start(...)`, `update_computed_through(...)`, `find_resumable(...)` |

`insert_many` uses Postgres bulk-insert syntax with `ON CONFLICT
(ticker_id) DO UPDATE` so a re-run of `derive_signals` cleanly
overwrites the prior cycle's row.

### 6.4 Fundamentals (8 repos in `fundamentals_repo.py`)

Bundled together because the runner uses them transactionally:

| Repo | Table | Owner |
|------|-------|-------|
| `IndustryOverrideRepository` | `industry_overrides` | Operator-curated industry classification fixes |
| `InstrumentIndustryRepository` | `instrument_industry` | yfinance-derived industry/sector per ticker |
| `InstrumentSharesRepository` | `instrument_shares` | Shares outstanding per ticker |
| `FundamentalsRunRepository` | `fundamentals_runs` | Quarterly run audit |
| `DailyMarketCapRepository` | `daily_market_cap` | Daily market cap (shares × close) |
| `QuarterlyFinancialsRepository` | `quarterly_financials` | Revenue / EPS / etc. |
| `IndicatorStateRepository` | `indicator_state` (legacy) | Pre-V1 wide table — dropped by 0030 |
| `SignalEventRepository` | `signal_events` (legacy) | Pre-V1 — dropped by 0030 |

The legacy `IndicatorStateRepository` and `SignalEventRepository`
remain in this file but their underlying tables are gone. Imports
from these classes are slated for demolition in the pending legacy
indicator_state cleanup.

### 6.5 Subscriber + notification log

| Repo | Tables | Key methods |
|------|--------|-------------|
| `SubscribersRepository` | `subscribers`, `subscriptions` | `find_due_for_notification(now)`, `update_last_notified_at(id, ts)`, `get_by_email(email)`, `list_active_subscriptions(subscriber_id)` |
| `NotificationLogRepository` | `notification_log` | `insert(entry)` — idempotent on `(subscriber_id, ticker_id, bar_timestamp, signal_type, cycle_id)` |

`find_due_for_notification(now)`: cadence-elapsed subscribers.
Returns rows where `now >= last_notified_at + cadence_minutes`.

### 6.6 Cycle audit + GUI surfaces

| Repo | Table | Key methods |
|------|-------|-------------|
| `ComputeRunsRepository` | `compute_runs` | `start(kind, exchange, notes)` returns run_id; `finish(run_id, ...)`; `recent(n, exchange=None)`. `kind` ∈ {intraday, eod, backfill} |
| `BackfillRunsRepository` | `backfill_runs` | `start(...)` — raises `IntegrityError` on concurrent active; `finish(run_id)`; `is_active()` for live-cycle deferral check; `release_lock()` for stale-cleanup |
| `MarketOverviewRepository` | `market_overview_daily` | `get_for_date(date)`, `recent(exchange, n)`, `range(exchange, from_date, to_date)` |

### 6.7 GUI backfill panel queries (`backfill_queries.py`)

Lightweight read queries that don't fit any single table:

- `active_symbols(exchange)` — populates the GUI's QCompleter for
  the ticker-scope field.
- `min_bar_date(exchange)` — populates the From-date field
  defaults.
- `historical_signals_delete_count_for_range(exchange, scoped_tickers, date_from)`
  — pre-flight modal: "this backfill will DELETE N historical rows."
- `market_overview_stale_count_for_range(exchange, date_from)` —
  pre-flight: "N market_overview rows will be stale until stage 10
  ships."

The DELETE-count semantics **mirror exactly** what
`backfill/pipeline.py::_stage_delete_historical` runs:

```sql
DELETE FROM historical_signals
 WHERE ticker_id = ANY(:tids)
   AND computed_at >= :from_dt
```

If the two diverge, the pre-flight modal lies. Don't change one
without the other.

## 7. `alembic_bridge.py` — migration state transitions

Three database states a process can encounter at startup:

| State | `schema_migrations` | `alembic_version` | Action |
|-------|---------------------|-------------------|--------|
| **(A)** Fresh DB | doesn't exist | doesn't exist | `alembic upgrade head` creates everything. Bridge is a no-op |
| **(B)** Pre-Phase-12 DB | populated through `010` | doesn't exist | Bridge stamps `0001_initial_schema` WITHOUT re-running DDL; then `upgrade head` applies any post-baseline revisions |
| **(C)** Post-Phase-12 DB | (irrelevant) | populated | Bridge no-op; `upgrade head` applies pending revisions if any |

### 7.1 `stamp_baseline_if_needed(engine, *, config_path=None) -> bool`

```python
def stamp_baseline_if_needed(engine, *, config_path=None) -> bool:
    if _alembic_has_a_version(engine):
        return False    # State (C) — already on Alembic
    if not _legacy_has_reached_baseline(engine):
        return False    # State (A) — no stamp needed; fresh DB
    # State (B) — stamp the baseline
    from alembic import command
    from alembic.config import Config as AlembicConfig
    alembic_cfg = AlembicConfig("alembic.ini")
    if config_path:
        alembic_cfg.cmd_opts = type("X", (), {"x": [f"config={config_path}"]})()
    command.stamp(alembic_cfg, _BASELINE_REVISION)
    return True
```

Idempotent: re-calls on a stamped DB return `False` without
modifying anything.

`_BASELINE_REVISION = "0001_initial_schema"` — must match the
`revision = ...` string in `alembic_pkg/versions/0001_initial_schema.py`.
A rename of that revision breaks the bridge.

### 7.2 `run_alembic_upgrade_head(config_path=None)`

Wraps `alembic upgrade head` so `app.py` has a single call site for
both fresh-DB creation (state A) and incremental updates (states B
and C).

### 7.3 Wire-up at startup

```python
# app.py
def build_app():
    config = load_config(...)
    engine_admin = create_engine_from_config(config, role="app")  # admin creds

    stamp_baseline_if_needed(engine_admin, config_path=config_path)
    run_alembic_upgrade_head(config_path=config_path)

    engine_service = create_engine_from_config(config, role="engine")
    ...
```

Bridge runs before migrations. Migrations use the admin role
(needs DDL privileges); subsequent service work uses the engine
role.

## 8. The `compute_runs` activity log

Lifecycle:

```python
repo = ComputeRunsRepository(conn)
run_id = repo.start(kind="intraday", exchange="NSE", notes="Tick at 09:30 IST")
try:
    ...
    repo.finish(run_id, tickers_attempted=2667, tickers_fetched=2496)
except Exception as e:
    repo.finish(run_id, failed_stage="stage_2_stock_state",
                error_message=str(e))
```

**Why `finish` sets `finished_at = NOW()` server-side**: client
clock might disagree with server clock. Operators trusting the log
want consistent timing.

**Crashed runs**: rows with `finished_at IS NULL`. The `recent()`
query returns them as in-flight; the GUI's activity log surfaces
them with duration "running" so operators spot stuck runs.

Kinds enforced by both Python `_VALID_KINDS = {"intraday", "eod",
"backfill"}` and a DB CHECK constraint. The Python check gives a
friendlier error message; the DB check is the source of truth.

## 9. The `backfill_runs` single-active marker

The single most important invariant in the backfill subsystem:
**at most one backfill active at a time**. Enforced by:

```sql
CREATE UNIQUE INDEX backfill_runs_active_uniq
    ON backfill_runs ((1)) WHERE finished_at IS NULL;
```

The constant-1 expression makes the index unique on a fixed value;
the `WHERE finished_at IS NULL` partial clause means only active
rows participate. Two concurrent INSERTs with NULL `finished_at`
both try to insert the same constant — second one raises
`IntegrityError`.

```python
def start(self, *, pid, exchange, date_from, date_to, refetch_bars,
          ticker_scope) -> int:
    row = self._conn.execute(text("""
        INSERT INTO backfill_runs (
            pid, exchange, date_from, date_to, refetch_bars, ticker_scope
        ) VALUES (:pid, :ex, :df, :dt, :rb, :ts)
        RETURNING run_id
    """), {...}).first()
    return int(row.run_id)
```

`IntegrityError` propagates up to the CLI caller, which prints
"another backfill is active; use --release-lock if stale".

```python
def is_active(self) -> bool:
    result = self._conn.execute(text(
        "SELECT 1 FROM backfill_runs WHERE finished_at IS NULL LIMIT 1"
    )).first()
    return result is not None
```

Used by `recompute_market` at the top of the cycle to defer when a
backfill is running.

## 10. The `weekly_buckets_with_bars` server-side fast path

```python
def weekly_buckets_with_bars(
    self, ticker_id, timeframe, since_inclusive, tz,
) -> set[datetime]:
    sql = text("""
        SELECT
            (date_trunc('week', bar_timestamp AT TIME ZONE :tz)
                AT TIME ZONE :tz) AS local_week_utc
        FROM ohlcv_bars
        WHERE ticker_id = :ticker_id
          AND timeframe = :tf
          AND bar_timestamp >= :since_inclusive
        GROUP BY local_week_utc
    """)
    return {row.local_week_utc for row in self._conn.execute(sql, {...})}
```

The fast path for gap detection. Equivalent Python:

```python
bars = self.fetch_since(ticker_id, timeframe, since)
return {_monday_of(b.timestamp, tz=tz) for b in bars}
```

But executes as ONE SQL with GROUP BY rather than transferring every
bar in the window. For an hourly source over a multi-week gap,
that's hundreds of rows vs. a handful.

**DST correctness via double `AT TIME ZONE`**:

1. `bar_timestamp AT TIME ZONE :tz` → `timestamptz` to local naive
   timestamp.
2. `date_trunc('week', ...)` → naive Monday-00:00 local.
3. `AT TIME ZONE :tz` → re-interprets local-Monday-midnight as a
   `timestamptz`, applying correct UTC offset for that specific
   date (DST-aware).

Matches `indicators.aggregation._monday_of` semantics exactly.

## 11. Bars upsert with `adj_close` (post-migration 010)

`adj_close` is yfinance's split/dividend-adjusted close, used for
backtests. The engine never sees it — Pine parity is held by
feeding **raw close** to the engine.

```python
def upsert_bar(self, ticker_id, timeframe, bar, *, adj_close=None):
    values = {"ticker_id": ticker_id, "timeframe": timeframe.value,
              "bar_timestamp": bar.timestamp,
              "open": bar.open, "high": bar.high, "low": bar.low,
              "close": bar.close, "volume": bar.volume}
    if adj_close is not None:
        values["adj_close"] = adj_close
    stmt = pg_insert(ohlcv_bars).values(**values).on_conflict_do_update(
        index_elements=["ticker_id", "timeframe", "bar_timestamp"],
        set_=set_,
    )
```

ETL writes `adj_close` when available; the backfill / state
computation reads `close` not `adj_close`.

## 12. Connection pooling and concurrency

| Setting | Default | Effect |
|---------|---------|--------|
| `pool_size` | 5 | Steady-state connection count |
| `pool_max_overflow` | 10 | Extra connections allowed under burst |
| `pool_timeout` | 30 s | Max wait for a free connection |
| `pool_recycle` | 3600 s | Connections older than this recycled |
| `pool_pre_ping` | True | Cheap `SELECT 1` before checkout |

Total max concurrent connections per engine: `pool_size +
pool_max_overflow = 15`. At production scale (single-thread
scheduler + ~10 notification workers + occasional HTTP), this is
comfortable.

**Each role has its own engine** with its own pool. Total connection
budget across the service ≈ 4 engines × 15 = 60. Postgres default
`max_connections = 100` — fits.

**Engine selection by process:**

| Process | Role | Privileges |
|---------|------|------------|
| `trip-ema` (main service) | engine | RW on indicator/signal/notification tables; R on tickers/bars |
| `trip-ema-etl` | etl | RW on ohlcv_bars / tickers / fundamentals; minimal elsewhere |
| `trip-ema-gui` | gui | R on everything; W on a few config tables |
| `alembic upgrade` | app | DDL (CREATE TABLE, ALTER, etc.) |

## 13. Failure modes

| Failure | Behaviour | Recovery |
|---------|-----------|----------|
| DB unreachable on `engine.begin()` | `OperationalError` propagates to caller | Surrounding cycle catches, logs, returns failure result |
| `pool_pre_ping` detects dead connection | Pool quietly opens a fresh one; the caller sees a normal connection | None needed |
| Pool exhausted (15 concurrent ops) | `TimeoutError` after `pool_timeout` seconds | Indicates a leaked connection (caller forgot `with`); restart needed |
| Connection killed mid-transaction | Transaction rolls back; exception propagates | Caller retries via the cycle's next pass |
| `IntegrityError` on backfill start | Single-active enforcement | CLI tells operator to wait or `--release-lock` |
| `ProgrammingError` on a column | Schema drift between `tables.py` and migrations | Update one to match the other; run the bridge |
| `compute_runs.finish` on an already-finished row | UPDATE succeeds; duration recalc uses latest `finished_at` | Avoid double-finishing in caller |

## 14. Testing

`tests/persistence/`:

| File | Coverage |
|------|----------|
| `test_connection.py` | Engine factory per role, URL construction, password resolution, transaction commit/rollback |
| `test_bars_repo.py` | fetch_history limits, fetch_since cutoffs, upsert conflict handling, weekly_buckets_with_bars DST behaviour |
| `test_tickers_repo.py` | get/list filters, ref-based lookup |
| `test_current_signals_repo.py` | get_for_ticker, list_all, insert_many UPSERT semantics |
| `test_historical_signals_repo.py` | append-only, get_for_ticker ordering, range delete |
| `test_stock_indicator_state_repo.py` | upsert idempotency, get_history with to_dt cap, delete_for_ticker_from |
| `test_compute_runs_repo.py` | start/finish lifecycle, kind validation, in-flight detection |
| `test_backfill_runs_repo.py` | Single-active enforcement (concurrent starts raise), is_active, release_lock |
| `test_alembic_bridge.py` | All three state transitions (A/B/C), idempotency, no-op when alembic absent |
| `test_tables_match_migrations.py` | Walk tables.py + migrations; column names + types match |

All tests use the `postgres_engine` fixture from
`tests/persistence/conftest.py` — a clean schema per test via
TRUNCATE + sequence reset.

## 15. Common pitfalls

1. **Don't open your own `engine.connect()` inside a repository
   method.** Take `Connection` in `__init__`. Caller's
   `with transaction(engine) as conn:` is the lifecycle.
2. **Don't use `text(":param::type")`.** SQLAlchemy bind regex has
   a `(?!:)` lookahead that refuses to substitute when a colon
   follows. Use `CAST(:param AS type)` instead.
3. **`Connection.execute(text(...))` returns a `Result` for SELECT
   but a `CursorResult` with `rowcount` for UPDATE/DELETE.** Don't
   mix; check the method you need.
4. **`fetch_history(ticker, tf, limit=200)` returns oldest-first
   by `bar_timestamp ASC`.** Most other repo methods returning
   lists are newest-first by `... DESC`. Read the docstrings.
5. **`StockIndicatorStateRepository.get_history_for_ticker` returns
   newest-first.** `states[0]` is the latest. `derive_signal_for_ticker_this_cycle`
   relies on this.
6. **The legacy `IndicatorStateRepository` writes to a
   dropped table.** Calls fail at runtime. The class lives on for
   legacy imports during the cutover; remove it with the legacy
   demolition.
7. **`adj_close` is NULL for pre-migration-010 historical bars.**
   Backtests that need split-adjusted prices should skip rows
   with NULL `adj_close` or fall back to raw `close`.

## 16. Extension paths

- **A new table.** Add an Alembic revision. Add a `Table(...)` to
  `tables.py`. Add a `XxxRepository` class. Add `tests/persistence/test_xxx.py`.
- **A read-only view repo.** Build query in SQL; return frozen
  dataclasses. No write methods, no `text("INSERT ...")`.
- **Bulk operations.** Use `pg_insert(...).on_conflict_do_update(...)`
  + `executemany` semantics. Repo's `insert_many` pattern.
- **Per-role pool tuning.** Extend `DbRoleCredentials` with
  per-role `pool_size` overrides. The current uniform 5/10/30/3600
  works at this scale but ETL bursts harder than GUI.

## 17. Related packages

- `trip_ema/config/schema.py` — `DatabaseConfig`, `DbRolesConfig`
  consumed by `connection.py`.
- `trip_ema/domain/` — every dataclass returned by every read.
- `trip_ema/orchestrator/` — invokes repos through `with
  transaction(engine)` blocks.
- `trip_ema/etl/runner.py` — heavy bars+tickers writer.
- `trip_ema/fundamentals/v1_runners.py` — heavy state+enrichment
  writer.
- `trip_ema/backfill/pipeline.py` — uses `backfill_runs_repo` and
  most others.
- `trip_ema/gui/db.py` — opens a read-only engine via
  `create_engine_from_config(role='gui')`.
- `alembic_pkg/versions/` — every migration that creates one of
  the tables this package reads/writes.
