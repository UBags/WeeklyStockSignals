# Multi-Exchange Architecture — Design

**Status:** proposed, not implemented
**Prereq:** a stable NSE version. Do not start this until NSE is `READY: yes`.
**Goal:** add an exchange to `config.yaml`, restart, run one backfill. Nothing else.

---

## 0. The decision already made

**One database per exchange.** `trip_ema_nse`, `trip_ema_nyse`, `trip_ema_bse`.
Identical schema in each. No shared tables. No cross-database joins.

The alternative was adding an `exchange` column to the nine industry tables and
their primary keys — 638 references across 22 production files, straight through
the signal path.

The reason for the split is not effort. It is that

```sql
industry_index_value_pkey  PRIMARY KEY (industry_name, trade_date)
```

makes NSE's *"Software - Infrastructure"* on 2026-07-10 and NYSE's **the same
row**, and a `--exchange NYSE` run does not fail — it *upserts*, silently, over
NSE's indices. Separate databases make that **structurally impossible** rather
than *prevented by a guard someone has to maintain*. Every guard in this codebase
that could be got wrong, was got wrong at least once. Structure beats vigilance.

---

## 1. Principles

These are not abstractions; each is a bug this project has already paid for.

1. **State lives where it is observed, not where it is declared.**
   A config file that says `fully_activated: true` while the database is empty is
   a lie. Derive activation from the data. (§4)

2. **A list maintained in two places will drift.**
   The set of exchanges is declared once, in config, and *everything* iterates it
   — cron, migrations, provisioning, the orchestrator. Never hand-duplicate it.
   (§8)

3. **Fail loud, fail early, fail before the six hours.**
   An unknown exchange, a missing database, a schema behind head — all abort at
   startup, not at hour four of a backfill. (§6)

4. **Capability comes from credentials, not from code.**
   The service can create databases *only* if it was given admin credentials. No
   privilege is hardcoded. (§5)

5. **One chokepoint per decision.**
   "Which database?" is decided in exactly one function. So is "what time is it
   in this market?" (§3, §7)

---

## 2. Config shape

```yaml
database:
  host: localhost
  port: 5432
  user: trip_ema_app
  password_env: TRIP_EMA_DB_PASSWORD

  # THE source of truth for "which exchanges exist".
  # Everything iterates this map: provisioning, migrations, cron, the
  # orchestrator pool, the audit.
  databases:
    NSE:  trip_ema_nse
    NYSE: trip_ema_nyse

  # OPTIONAL. Present -> bootstrap may CREATE DATABASE and GRANT.
  # Absent  -> bootstrap verifies only and refuses to start on a gap.
  # Capability is granted by supplying credentials, never by a code path.
  admin:
    user: postgres
    password_env: TRIP_EMA_ADMIN_DB_PASSWORD

market_hours:
  - exchange: NSE
    open:  "09:15"
    close: "15:30"
    tz:    Asia/Kolkata          # no DST
    weekdays: [1, 2, 3, 4, 5]
  - exchange: NYSE
    open:  "09:30"
    close: "16:00"
    tz:    America/New_York      # HAS DST — see §7
    weekdays: [1, 2, 3, 4, 5]
```

### An explicit map, not a template

`trip_ema_{exchange.lower()}` was considered and rejected. A template silently
manufactures a database name for a typo: `--exchange NYSEE` becomes
`trip_ema_nysee`, and with auto-create that is a *phantom database that looks
real*. The map fails at config load, on a name nobody wrote down.

### Config validation (at load, before anything connects)

Every exchange in `databases:` **must** have:

- a `market_hours` entry (else the orchestrator never knows when to run), and
- a listings source (else `backfill` dies at `stage_0` — see §9).

A configured exchange missing either is a config error, not a runtime surprise.

---

## 3. Database routing — the chokepoint

Everything already funnels through one function:

```python
build_db_url(config)                    # today
build_db_url(config, exchange)          # proposed
```

`create_engine_from_config`, `create_engine_for_role`, `engine_spec_from_config`
(the multiprocessing children) and Alembic's `env.py` all go through it. So
"which database" is decided in exactly one place, and an unknown exchange raises
there:

```python
def build_db_url(config: Config, exchange: str) -> str:
    dbs = config.database.databases
    if exchange not in dbs:
        raise UnknownExchangeError(
            f"exchange {exchange!r} has no database configured. "
            f"Known: {sorted(dbs)}. Add it to database.databases in config.yaml "
            f"and run `provision --exchange {exchange}`."
        )
    ...
```

### Engine registry

`create_engine()` builds a **connection pool**. One engine per exchange, cached,
created lazily:

```python
class EngineRegistry:
    def get(self, exchange: str) -> Engine     # cached, lazy
    def dispose_all(self) -> None
```

**Connection budget:** `pool_size(5) + max_overflow(10) = 15` per exchange. Three
exchanges = 45 against Postgres's default `max_connections=100`. The service holds
an engine for every *activated* exchange simultaneously — count them before adding
the fourth.

**Do not fork with live engines.** `mp_context="spawn"` is already mandatory
(fork duplicates psycopg socket FDs). The registry is a module-level cache and is
spawn-safe; it must never be inherited across a fork.

### Call sites

~32 production sites, and nearly all are CLI command functions that **already have
`args.exchange` in scope**. The codebase is already exchange-parameterised at the
API level (`backfill(exchange=)`, every runner takes `exchange`) — it simply never
routed the *engine* on it. That is what makes this cheap.

---

## 4. The lifecycle — and why `fully_activated` does not go in config

You asked for a `fully_activated` flag in config, written back automatically on
first backfill. **I would not do that, and the reason matters.**

A program writing to its own config file:

- destroys comments and formatting,
- races two processes against one file,
- creates git conflicts on a tracked file, and
- most importantly: **it is state, not configuration.** Config is *declarative
  intent* — what the operator wants. Activation is an *observed fact* — what
  actually happened. Storing a fact in the intent file means the file can lie.

We have spent this entire project killing exactly that class of lie: a
`record_success()` that fired on a zero-bar fetch; a phase ledger that marked
`fetch_bars` COMPLETE having fetched three tickers. A `fully_activated: true`
sitting next to an empty database is the same bug in YAML.

### It already exists, in the right place

```sql
exchange_state (
    exchange                   text NOT NULL,
    initial_load_started_at    timestamptz,   -- set on first backfill, never overwritten
    initial_load_completed_at  timestamptz,   -- set on first SUCCESS backfill, never overwritten
    ...
)
```

`initial_load_completed_at` **is** `fully_activated`. It is per-exchange, it lives
in that exchange's own database, it is written by the thing that does the
activating, and it cannot disagree with reality.

### Three states, all derived, none stored

| state | test | meaning |
|---|---|---|
| **CONFIGURED** | in `database.databases` | operator has declared it |
| **PROVISIONED** | database exists ∧ `alembic_version == head` | schema ready, no data |
| **ACTIVATED** | `exchange_state.initial_load_completed_at IS NOT NULL` | backfill has succeeded at least once |

```python
def lifecycle(config, exchange) -> Lifecycle:
    """Derived from facts. Never stored, so it can never be stale."""
```

The bootstrap check is cheap (one `SELECT`), runs once at startup, and is cached
for the process lifetime — an exchange does not become activated mid-run except
by the backfill that activates it, which updates the cache itself.

---

## 5. Provisioning and bootstrap

### What `provision` does, per exchange, idempotently

1. **CREATE DATABASE** if absent.
   *Postgres has no `CREATE DATABASE IF NOT EXISTS`* — check `pg_database` first.
   *`CREATE DATABASE` cannot run inside a transaction block* — it needs autocommit.
2. **GRANTs.** ⚠️ **A new database does not inherit role grants.**
   This is the one that will bite: you create the DB, migrations run as owner, the
   service starts, and `trip_gui_user` cannot read a single row. `trip_gui_user`
   (read), `trip_etl_user` (write/DDL) and `trip_ema_app` (owner) must be granted
   explicitly, including `ALTER DEFAULT PRIVILEGES` so future tables inherit.
3. **`alembic upgrade head`** against that database.
4. **Seed** `exchange_state` (one row) and the default subscriber.
5. **Warn** if the exchange has no listings source (§9).

### Bootstrap on service startup

```
for exchange in config.database.databases:
    match lifecycle(exchange):
        CONFIGURED (no DB):
            admin creds present -> provision it, log loudly
            admin creds absent  -> FAIL FAST:
                "NYSE is configured but has no database.
                 Run: provision --exchange NYSE"

        PROVISIONED (schema < head):
            admin creds present -> alembic upgrade head, log loudly
            admin creds absent  -> FAIL FAST (never run on a stale schema)

        PROVISIONED (at head, no data):
            OK. Register engine. Skip all periodic jobs (§8).

        ACTIVATED:
            OK. Register engine. Run everything.
```

This is what makes it "not run CLI commands continuously": **add the exchange to
config, restart the service, and it provisions itself** — *if and only if* you gave
it admin credentials. Take them away and it becomes verify-only. The capability is
in the credentials, not in a code path, so there is no `--force` to regret.

---

## 6. Schema propagation

### The new risk this architecture creates

N databases means N schemas, which means **drift**: you migrate NSE, forget NYSE,
and NYSE silently runs last month's schema. You would find out through wrong
signals. That is the same failure shape as everything else in this project, so it
gets a gate rather than a convention.

### Three defences, in order

1. **`migrate-all` iterates config.** Never migrate one database by hand.
   Alembic reads the target from `-x exchange=NSE`; `env.py` calls
   `build_db_url(config, exchange)` — the same chokepoint as §3, so it cannot
   drift from the app's idea of where the database is.

2. **Startup asserts every configured database is at head.** Behind, and the
   service refuses to start (or upgrades, with admin creds). A service running on
   a stale schema is not a degraded service; it is a wrong one.

3. **The fidelity audit gains a schema check.**
   `alembic_version.version_num != head` → **`BLOCKED`**, which the backfill's
   pre-flight already aborts on (Commit O). So a drifted database cannot even
   *start* a backfill.

`migrate-all` is **all-or-nothing in reporting**, not in transaction: Postgres DDL
across databases cannot share a transaction. If NYSE fails, NSE stays migrated —
but the command exits non-zero, names the failure, and startup will then refuse
until it is fixed. Never leave a partial migration undetected.

---

## 7. Time and timezone — the real NYSE blocker

**This is the hardest part, and it has nothing to do with databases.** Separate
databases give NYSE *isolation*. They do not give it *correctness*.

### The bug

```python
# trip_ema/backfill/pipeline.py
_IST = ZoneInfo("Asia/Kolkata")
...
as_of_dt = datetime.combine(d, time(16, 30), tzinfo=_IST)   # the daywalk
```

16:30 IST is NSE's post-close. For NYSE that is **06:00 ET — three hours before
the opening bell.**

And it goes deeper than one constant. `bar_timestamp` is documented as *"the
**IST-Monday** week anchor"*. A Friday NYSE close at 16:00 ET is **Saturday 02:30
IST**. If the week anchor is computed in IST, **Friday's US bar lands in the
following week.** Every NYSE weekly bar would be off by one — and the weekly bar
*is* the strategy.

There are **42 naked `datetime.now()` / `date.today()` / `datetime.utcnow()` calls
in production code**. Each is a place that silently assumes the machine's timezone
is the market's timezone. On this NUC (TZ=Asia/Kolkata, running NSE) that
assumption is invisibly true, which is exactly why it has never surfaced.

### On the "world time server" idea — it does not fix this

You suggested fetching the current time from a network time service. That conflates
two different problems, and solves the one you do not have:

| problem | symptom | correct fix |
|---|---|---|
| **Clock accuracy** — is the machine's clock right? | drift of seconds | **NTP** (`systemd-timesyncd`/`chrony`). An OS concern. The NUC already does this. |
| **Timezone semantics** — which timezone defines a *trading day* and a *week*? | NYSE bars in the wrong week | **the fix below** |

Fetching time over HTTP would make it *worse*: a network round-trip on the signal
path, a new failure mode (what does the pipeline do when the time server is
down?), latency added to every timestamp, and an external dependency for something
the kernel already has correct. **The machine clock is not the problem. The
semantics are.**

### The fix: `MarketClock` — one chokepoint for "what time is it here?"

```python
class MarketClock:
    """Every time-and-date decision for ONE exchange.

    The machine's timezone is irrelevant and must never be consulted. The
    exchange's own tz (from market_hours) is the only authority for what day it
    is, what week it is, and when the session closed.
    """
    def __init__(self, exchange: str, config: Config): ...

    def now(self) -> datetime:            # tz-aware, in the market's tz
    def today(self) -> date:              # the market's date, not the machine's
    def week_anchor(self, d: date) -> date:      # Monday IN THE MARKET'S TZ
    def session_close_dt(self, d: date) -> datetime:   # 15:30 IST / 16:00 ET
    def is_session_day(self, d: date) -> bool
```

Rules, enforced not merely intended:

1. **Store UTC.** Already true — every column is `timestamptz`. Unchanged.
2. **Compute in market-local.** The week anchor is Monday *in the market's
   timezone*. The daywalk `as_of` is *that market's* post-close.
3. **DST is free.** `ZoneInfo("America/New_York")` handles it; IST has none. Using
   `ZoneInfo` everywhere makes DST correct **by construction** — no offset
   arithmetic anywhere, ever.
4. **Ban the naked clock.** A test walks the AST of `trip_ema/` and fails on any
   `datetime.now()` without `tz=`, any `date.today()`, any `datetime.utcnow()`.
   That is the only way 42 call sites stay fixed. A convention decays; a failing
   test does not.
5. **A regression test per exchange.** For each configured exchange, assert the
   week anchor of a known Friday is the correct Monday, across a DST boundary.

### Order of work

This must land **before any NYSE backfill**, and it is independent of the database
split. It can be done on NSE alone, where the correct answer is already known —
which is the safest possible place to do it, because every existing NSE test must
still pass unchanged.

---

## 8. The activation gate

Every periodic job asks one question first:

```python
if lifecycle(exchange) is not ACTIVATED:
    logger.info("%s is PROVISIONED but not ACTIVATED — skipping %s", exchange, job)
    return
```

| job | gated? |
|---|---|
| intraday tick | ✅ skip until activated |
| EOD | ✅ |
| listings sync | ✅ |
| annual industry rebalance | ✅ |
| monthly reference re-check | ✅ |
| **backfill** | ❌ — **backfill is the activator.** It is the one job that runs on a PROVISIONED exchange, and completing it is what makes the exchange ACTIVATED. |

So a freshly-configured NYSE sits harmlessly at PROVISIONED: schema present, no
data, every scheduled job silently skipping. Nothing breaks, nothing spams, nothing
half-runs. Then `backfill --exchange NYSE` populates it, sets
`initial_load_completed_at`, and every job starts including it — **with no
configuration change and no cron edit.**

### Cron: one line, forever

```
0 9 * * * ... reference-recheck --all-exchanges --if-due
```

**Not one line per exchange.** Add NYSE to config and forget the cron line, and
NYSE silently never gets its monthly re-check — a list maintained in two places
that quietly disagree, which is Principle 2 and the same disease as schema drift.
`--all-exchanges` iterates config, and the activation gate skips the ones that are
not ready.

Same for `audit --all-exchanges`, `provision --all`, `migrate-all`.

---

## 9. What is global? **Nothing.** Especially not `subscribers`.

The temptation is a shared `trip_ema_meta` database holding `subscribers`.
**Resist it**, for a concrete reason.

The planned notifier signature includes `which_stocks [None | ALL | List]` — a
list of stocks. That is a **ticker reference**. In separate databases,
`ticker_id 1704` is RELIANCE in NSE and something else entirely in NYSE. A shared
`subscribers` table referencing `ticker_id` **reintroduces exactly the primary-key
collision this whole design exists to eliminate** — one layer up, and *across
databases*, where no foreign key can enforce anything at all. That is strictly
worse than the problem we are fleeing.

**So `subscribers` lives in each exchange's database.** A subscription *is*
per-exchange — the notifier spec already has `which_exchanges`.
`uddipan@gmail.com` is a row in `trip_ema_nse.subscribers` and, later, in
`trip_ema_nyse.subscribers`.

- **One email spanning exchanges?** That is the common notifier layer (already a
  queued workstream). It reads N databases and merges at send time. An app-level
  join is honest; a cross-database FK is a lie you cannot enforce.
- **Cost:** changing an email address is N updates. With one subscriber, nothing.
- **If it ever hurts**, add a meta database *then* — holding only email and
  preferences, with **no ticker references**, so there is nothing to FK.

### Listings sources — the fourth NYSE blocker

```python
def refresh_listings(...):
    if exchange != "NSE":
        raise UnsupportedExchangeError(
            "the listings downloader is NSE-specific"
        )
```

**There is no listings source for any exchange but NSE.** You cannot get NYSE
tickers into a database at all — not because of the schema, but because nothing
fetches them. This is unavoidable, independent of everything above, and it is real
work: an exchange-specific downloader + parser behind a common interface.

`provision --exchange NYSE` must warn about this rather than let you discover it at
`stage_0` of a backfill.

---

## 10. Commit sequence

Nothing here starts until NSE is `READY: yes`.

| # | commit | risk | note |
|---|---|---|---|
| **P** | `MarketClock` + ban the naked clock (AST test) + fix the 42 call sites | **high** | **Do this FIRST, on NSE alone.** The correct answer is known there, and every existing test must still pass. Independent of the DB split. |
| **Q** | `build_db_url(config, exchange)` + `databases:` map + `EngineRegistry`; ~32 call sites | medium | The routing change. Testable with one database. |
| **R** | Alembic `-x exchange`; `migrate-all`; **schema-version check in the audit → BLOCKED** | medium | The drift gate is not optional |
| **S** | `provision` (CREATE DATABASE, **GRANTs**, migrate, seed) + startup bootstrap | medium | Capability via admin credentials only |
| **T** | `lifecycle()` + the activation gate on every periodic job; `--all-exchanges` | low | The `fully_activated` behaviour, derived not stored |
| **U** | Retire the `ForeignIndustryLayerError` guard → assert every ticker in *this* DB matches *this* DB's exchange | low | The guard becomes a cheap invariant |
| **V** | NYSE listings source | **high** | Unavoidable, and unrelated to all of the above |

**Migrating the existing data:**

```sql
ALTER DATABASE trip_ema RENAME TO trip_ema_nse;
```

One second. Zero data movement. That is the entire migration.

---

## 11. Risks, honestly

1. **The timezone work (P) is the real project.** The database split is mechanical
   and safe. Rewriting 42 clock call sites in the signal path is not. It must be
   done on NSE, where a wrong answer is immediately visible.

2. **Schema drift** is the new failure mode this architecture *creates*. It is
   fully mitigated only if the audit's schema check (R) actually blocks. Skip it
   and you will eventually run NYSE on last month's schema and trust its signals.

3. **GRANTs on a new database** are the most likely thing to be forgotten, because
   everything appears to work — until the GUI, running as `trip_gui_user`, sees
   nothing.

4. **Connection budget.** 15 per exchange. Fine at three, worth checking at six.

5. **What we give up:** cross-exchange SQL. There is none today, and the only
   plausible consumer (a merged email) is better served in the app anyway. But it
   is a real door being closed, and reopening it later means a meta database or an
   FDW — not a schema change.

---

## 12. The one-line summary

Add the exchange to `config.yaml`. Restart. Run `backfill --exchange NYSE` once.

Everything else — the database, the schema, the grants, the seed rows, the
scheduling — happens because the code **iterates the config** rather than because
someone remembered to run a command.
