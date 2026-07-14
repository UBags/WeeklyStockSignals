# `trip_ema/config/` — YAML config loader, schema, secrets

## 1. Role in the larger application

`config/` is the **single source of truth** for runtime configuration.
A single Pydantic-validated `Config` object is built once at startup
from a YAML file plus a small set of environment variables and is
injected into every other layer that needs it.

The package is responsible for:

1. **Reading + validating YAML** → typed `Config` instance
   (`loader.py` + `schema.py`).
2. **Loading external secrets** at startup with fail-fast contracts
   (`brevo_secrets.py`).
3. **Resolving per-`(exchange, timeframe)` indicator overrides** for
   the Phase-15 backtest pipeline (`exchange_overrides.py`).
4. **Documenting the YAML surface** for operators
   (`example.yaml`, `config_example.yaml`).

```mermaid
flowchart LR
    yaml[config.yaml] --> loader
    env[env vars: TRIP_EMA_DB_PASSWORD, TRIP_EMA_ADMIN_TOKEN, ...] --> loader
    secrets[secrets/brevo.yaml] --> brevo[brevo_secrets.load_brevo_api_key]

    loader --> schema[Config<br/>Pydantic v2 model]
    brevo -.fail-fast guard.-> loader

    schema --> app[app.py wire-up]
    schema --> overrides[exchange_overrides.resolve_indicator_params]
    overrides --> backtest[fundamentals.signal_generator]
```

## 2. Architecture and constraints

| Rule | Why |
|------|-----|
| **No secrets in the YAML file.** Every secret is referenced by an env-var name (`password_env`) or by an external file path | The YAML is committable; secrets aren't. Operators rotate credentials by changing env values or files without touching VCS |
| **Fail-fast at load.** Missing env vars, missing secrets files, and validation errors all raise during startup, not at first use | A misconfigured deployment crashing at boot is loud; one running fine but silently dropping email is awful |
| **Pydantic-validated `Config` is the only type downstream sees.** Raw YAML dicts never escape `loader.py` | Compile-time-ish guarantees on field types; one place to wire validation; consistent error messages |
| **`Config` is `frozen=True`.** No layer can mutate config at runtime | Stops the "I'll just patch this field" anti-pattern in code review |
| **Optional sections degrade gracefully.** If `transports.brevo` is absent, email transport is unavailable but the service starts | The single `config.yaml` covers dev, staging, prod by omitting blocks |

## 3. File inventory

```
trip_ema/config/
├── __init__.py             Re-exports Config, load_config, resolve_db_password
├── loader.py               load_config() + secrets resolution
├── schema.py               Config + every sub-schema (Pydantic v2 models)
├── exchange_overrides.py   resolve_indicator_params() for Phase-15 backtest
├── brevo_secrets.py        Brevo API-key loader with permission checks
├── example.yaml            Minimal example — required sections only
└── config_example.yaml     Extended example — all optional sections shown
```

## 4. `loader.py` — the entry point

### 4.1 `load_config(path: str | Path) -> Config`

```python
def load_config(path):
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")
    try:
        with p.open("r") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"YAML parse error in {p}: {e}")

    try:
        config = Config.model_validate(raw)
    except Exception as e:
        raise ConfigError(f"Config validation failed for {p}: {e}")

    # Fail-fast env-var checks
    if config.database.password_env not in os.environ:
        raise ConfigError(f"Database password env var '...' is not set")

    if config.redis is not None and config.redis.password_env:
        if config.redis.password_env not in os.environ:
            raise ConfigError(f"Redis password env var '...' is not set")

    # Fail-fast Brevo secrets check
    if config.transports.brevo is not None:
        try:
            load_brevo_api_key(config.transports.brevo.secrets_file)
        except BrevoSecretsError as e:
            raise ConfigError(str(e))

    return config
```

The function does five things in order:

1. **Read the file.** Missing file → `ConfigError`.
2. **YAML parse.** Malformed YAML → `ConfigError`.
3. **Pydantic validate.** Any field-shape problem → `ConfigError`
   (Pydantic's `ValidationError` is wrapped).
4. **Verify DB password env var is set.** If not → `ConfigError`.
   The password itself is **not** stored on the `Config` object —
   it's resolved at connect time via `resolve_db_password`.
5. **Verify Redis password env var is set** (when Redis is
   configured). Same fail-fast contract.
6. **Verify Brevo secrets file** (when Brevo is configured) — see
   §5 below.

The order matters: secrets checks come after Pydantic validation
because they need parsed `config.database`, `config.redis`,
`config.transports.brevo` to know what to check.

### 4.2 `resolve_db_password(config) -> str` and `resolve_redis_password(config) -> str | None`

Connect-time helpers. Re-read the env var. If `load_config` succeeded
but someone unset the env var afterward, these raise `KeyError`. In
production they always succeed because the env var was checked at
load.

```python
def resolve_db_password(config: Config) -> str:
    return os.environ[config.database.password_env]
```

### 4.3 `ConfigError`

```python
class ConfigError(Exception):
    """Raised when config is missing, malformed, or references unset env vars."""
```

The single error type callers catch. `load_config` always wraps the
underlying exception (YAML error, Pydantic error, Brevo error) into
`ConfigError` so the caller doesn't need to know what failed
specifically.

## 5. `brevo_secrets.py` — external secrets

### 5.1 The format

`secrets/brevo.yaml`:

```yaml
api_key: xkeysib-AAAA....ZZZZ
```

One key, one value. Lives outside the main `config.yaml` so the main
config can stay committable.

### 5.2 `load_brevo_api_key(path) -> str`

```python
def load_brevo_api_key(path: str | Path) -> str:
    p = Path(path)
    if not p.exists():
        raise BrevoSecretsError(f"Brevo secrets file not found: {p}")
    if not p.is_file():
        raise BrevoSecretsError(f"...not a regular file: {p}")

    # Permission check: world/group readable is rejected
    file_mode = p.stat().st_mode
    forbidden = stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH
    if file_mode & forbidden:
        raise BrevoSecretsError(
            f"Brevo secrets file has insecure permissions: {p} "
            f"(mode={oct(stat.S_IMODE(file_mode))}). "
            f"Fix with: chmod 600 {p}"
        )

    # Parse and validate shape
    data = yaml.safe_load(p.read_text()) or {}
    raw_key = data.get("api_key")
    if not isinstance(raw_key, str):
        raise BrevoSecretsError(
            f"Brevo secrets file missing required field 'api_key' (string): {p}"
        )
    key = raw_key.strip()
    if not key:
        raise BrevoSecretsError(f"Brevo secrets file 'api_key' is empty: {p}")
    if not key.startswith("xkeysib-"):
        raise BrevoSecretsError(
            f"Brevo secrets file 'api_key' has unexpected format "
            f"(expected prefix 'xkeysib-'): {p}"
        )

    return key
```

**Five fail-fast guarantees**:

1. **File existence** — present or it's an error.
2. **Regular file** — not a directory or socket.
3. **Permissions** — group/world readable or writable is rejected.
   Brevo keys grant full send privileges on the sender's account;
   a 0644 secrets file on a shared machine is a real risk. The
   convention is 0600.
4. **`api_key` field present, string, non-empty.**
5. **Prefix `xkeysib-`** — every Brevo API key starts with this.
   Catches paste errors immediately instead of waiting for a 401
   from Brevo at first send.

**Why not echo the partial key in error messages?** Error messages
end up in logs (especially `/tmp/trip-ema.log`). Echoing even a
substring of a credential makes log files more sensitive than they
need to be. The error tells you "wrong format"; the operator looks
at the file.

### 5.3 Lazy vs eager loading

`load_brevo_api_key` is called twice in normal operation:

1. **At startup** by `load_config`, purely for the fail-fast check.
   The key is read but discarded.
2. **At first email send** by `notifications/email_transport.py`,
   which actually uses the key.

The double-read is intentional — startup validates without
keeping the key in memory. The transport reads it again on first
send. Subsequent sends use the cached key on the transport instance.

## 6. `schema.py` — the type-validated surface

### 6.1 Top-level `Config`

```python
class Config(BaseModel):
    database:           DatabaseConfig
    ema:                EMAConfig
    atr:                ATRConfig
    market_hours:       list[MarketHoursConfig]
    scheduling:         SchedulingConfig             = SchedulingConfig()
    cache:              CacheConfig                  = CacheConfig()
    http:               HTTPConfig                   = HTTPConfig()
    observability:      ObservabilityConfig          = ObservabilityConfig()
    transports:         TransportsConfig             = TransportsConfig()
    etl:                ETLConfig                    = ETLConfig()
    redis:              RedisConfig | None           = None
    notifier:           NotifierConfig               = NotifierConfig()
    exchange_overrides: dict[str, dict[str, ExchangeIndicatorOverride]] = {}
```

**Required**: `database`, `ema`, `atr`, `market_hours`. Everything
else has a default; YAML can omit them and the service still starts.

The class is `frozen=True, extra="forbid"`:

- **`frozen`** — fields can't be reassigned after construction.
- **`extra="forbid"`** — unknown YAML keys raise. Catches typos
  like `markethours:` (no underscore) at startup.

### 6.2 `DatabaseConfig`

```python
class DatabaseConfig(BaseModel):
    driver:                  Literal["postgresql"] = "postgresql"
    host:                    str
    port:                    int = 5432
    database:                str
    user:                    str
    password_env:            str         # name of env var with password
    tickers_table:           str = "tickers"
    bars_table:              str = "ohlcv_bars"

    pool_size:               int = 5
    pool_max_overflow:       int = 10
    pool_timeout_seconds:    int = 30
    pool_recycle_seconds:    int = 3600  # -1 = disable

    roles:                   DbRolesConfig | None = None
```

Connection-pool defaults are SQLAlchemy's, surfaced so they're
tunable without code. `pool_recycle_seconds=3600` exists
specifically to protect against managed-DB load balancers that
silently close idle connections after 30-60 minutes.

`url_without_password()` returns `postgresql://user:***@host:port/db`
for safe-to-log URL diagnostics.

### 6.3 `DbRolesConfig` (Phase 12 role separation)

```python
class DbRoleCredentials(BaseModel):
    user:         str
    password_env: str

class DbRolesConfig(BaseModel):
    etl:    DbRoleCredentials | None = None
    engine: DbRoleCredentials | None = None
    gui:    DbRoleCredentials | None = None
```

Optional per-process credentials. When set, ETL / engine / GUI
processes use their respective roles; when `None` (default), all
processes use the top-level `database.user` / `database.password_env`.

Roles are created by Alembic migration `0002_db_roles.py`; operators
grant LOGIN + set passwords as a deployment step then uncomment the
block. Pre-Phase-12 deployments keep working without any change.

**Migration caveat.** `alembic upgrade head` must run as the
top-level user (full DDL privileges); the per-role users don't have
DDL. So the process running migrations at startup keeps using the
top-level creds.

### 6.4 `EMAConfig` and `ATRConfig`

```python
class EMAStageConfig(BaseModel):
    base: int = Field(ge=2)
    s1:   int = Field(ge=2)
    s2:   int = Field(ge=2)
    final: int | None = None

class EMAConfig(BaseModel):
    med:                EMAStageConfig
    long:               EMAStageConfig
    longest:            EMAStageConfig
    volume_sma_length:  int = 2          # 1 = raw, 2 = Pine convention

class ATRConfig(BaseModel):
    length:      int   = 14
    inner_mult:  float = 0.2
    mid_mult:    float = 2.0
    outer_mult:  float = 4.0
```

`Config.to_indicator_params()` flattens these into a single
`domain.IndicatorParams` instance used by the live engine.

### 6.5 `MarketHoursConfig`

```python
class MarketHoursConfig(BaseModel):
    exchange:                    str
    open:                        str            # "HH:MM" in local tz
    close:                       str            # "HH:MM" in local tz
    tz:                          str            # IANA, e.g. "Asia/Kolkata"
    weekdays:                    list[int]      # 1=Mon..7=Sun

    # Legacy tick scheduling (used when tick_schedule is None)
    post_open_offset_minutes:    int = 5
    pre_close_offset_minutes:    int = 15
    intraday_cadence_minutes:    int = 60

    # Calendar
    holidays_country:            str | None = None        # python-holidays code
    holidays_override_file:      str | None = None        # operator overrides

    # Phase-16a explicit tick schedule (replaces legacy 3-field cadence)
    tick_schedule:               list[TickScheduleEntry] | None = None
```

Per-market trading hours and tick scheduling. Field validators
verify the `HH:MM` parses and the `tz` is recognised by `zoneinfo`
at **startup**, not at first tick.

### 6.6 `TickScheduleEntry` (Phase 16a)

```python
class TickScheduleEntry(BaseModel):
    name:           str                                 # alphanumeric + underscore
    anchor:         Literal["open", "close", "midpoint"]
    offset_minutes: int = 0                             # signed; negative = before

    # Cross-field validators:
    # - midpoint anchor must have offset_minutes == 0
    # - midpoint anchor requires at least one open- and one close-anchored entry
    #   in the same schedule
    # - names must be unique within the schedule
```

The new tick-schedule schema. Each entry names one of the four
daily ticks (`post_open`, `mid_session`, `pre_close`, `post_close`)
and anchors it relative to `open`, `close`, or `midpoint`. Relative
offsets mean NYSE early-close days (13:00 ET) auto-slide every
instant — no half-day branching in code.

Example:

```yaml
tick_schedule:
  - { name: post_open,   anchor: open,     offset_minutes:  15 }
  - { name: mid_session, anchor: midpoint                       }
  - { name: pre_close,   anchor: close,    offset_minutes: -30 }
  - { name: post_close,  anchor: close,    offset_minutes:  60 }
```

**Cross-entry validation** (in `MarketHoursConfig._validate_tick_schedule`):

1. Schedule not empty (omit the block instead of `[]`).
2. Tick names unique within the schedule.
3. If a `midpoint`-anchored entry exists, the schedule must also
   contain at least one `open`-anchored and one `close`-anchored
   entry to compute the midpoint from.

### 6.7 `OrchestratorConfig`

```python
class OrchestratorConfig(BaseModel):
    enabled:                              bool = False
    catch_up_on_startup:                  bool = False
    catch_up_skip_if_recent_minutes:      int  = 30
    fetch_timeout_seconds:                int  = 600
    fetch_retry_delay_minutes:            int  = 5
```

When `enabled=True`, the per-market `MarketOrchestrator` threads are
spawned and the legacy `ComputeScheduler` is suppressed.

`catch_up_on_startup`: if the service starts up after missing one or
more scheduled ticks today, fire one catch-up `run_once`
immediately. Set to `True` in environments where restarts often
happen mid-day; default `False` skips missed ticks (next-tick loop
catches up naturally).

`catch_up_skip_if_recent_minutes`: belt-and-braces against firing on
a normal restart that happens shortly after the last pass.

### 6.8 `TransportsConfig` and `BrevoConfig`

```python
class BrevoConfig(BaseModel):
    secrets_file:    str       # path to the YAML with the api_key
    sender_email:    str       # e.g. "weeklystocksignals@outlook.com"
    sender_name:     str       # display name in the From header
    base_url:        str = "https://api.brevo.com/v3"

class CircuitBreakerConfig(BaseModel):
    enabled:                  bool = True
    failure_threshold:        int  = 5
    reset_timeout_seconds:    int  = 30
    half_open_max_successes:  int  = 1

class TransportsConfig(BaseModel):
    brevo:           BrevoConfig | None = None
    circuit_breaker: CircuitBreakerConfig = CircuitBreakerConfig()
    # ... (webhook + slack settings)
```

`brevo` is optional — when `None`, the email transport is
unavailable and the notifier composes nothing. With `brevo` set,
`loader.py` runs the fail-fast secrets check.

### 6.9 `ETLConfig`

```python
class ETLConfig(BaseModel):
    exchange:                       str = "NSE"
    tier_1_size:                    int = 200
    tier_2_size:                    int = 500
    park_failure_threshold:         int = 3
    yfinance_batch_size:            int = 50
    yfinance_concurrency:           int = 4
    yfinance_request_delay_seconds: float = 0.5
    reconcile_window_days:          int = 7
    parked_dir:                     str = "var/etl/parked"
    checkpoint_dir:                 str = "var/etl"
    eod_run_hour_ist:               int = 17
    eod_run_minute_ist:             int = 30
    intraday_lookback_days:         int = 5
```

Consumed by the `trip-ema-etl` CLI as a separate process. The main
service ignores this section.

### 6.10 `RedisConfig` and `NotifierConfig`

```python
class RedisConfig(BaseModel):
    host:         str = "localhost"
    port:         int = 6379
    db:           int = 0
    password_env: str | None = None    # optional

class NotifierConfig(BaseModel):
    worker_concurrency: int = 4
    queue_name:         str = "trip_ema_notifications"
```

`RedisConfig` is **optional**. When absent, the (legacy) subscriber
distributor runs in legacy in-process mode. When present, the
distributor enqueues to Redis and the `trip-ema-notifier` worker
process dispatches.

V1 deprecates the queued path — `notifications/compose_email.py`
+ `send_email.py` are the live route. The Redis path is preserved
for backwards compat.

## 7. `exchange_overrides.py` — Phase-15 per-market tuning

### 7.1 The YAML shape

```yaml
exchange_overrides:
  NSE:
    "1W":
      ema:
        med:     { base: 8,  s1: 4, s2: 4 }
        long:    { base: 15, s1: 4, s2: 4, final: 3 }
        longest: { base: 28, s1: 7, s2: 5, final: 3 }
        volume_sma_length: 2
      atr:
        length: 14
        inner_mult: 0.2
        mid_mult: 2.0
        outer_mult: 4.0
    "1D":
      ema: { ... }
      atr: { ... }
  NYSE:
    "1W":
      ema: { ... }
      atr: { ... }
```

### 7.2 `resolve_indicator_params`

```python
SUPPORTED_BACKTEST_TIMEFRAMES = frozenset({"1D", "1W"})

def resolve_indicator_params(
    config:     Config,
    exchange:   str,
    timeframe:  Timeframe,
) -> IndicatorParams:
    overrides = getattr(config, "exchange_overrides", None)
    if overrides:
        per_exchange = overrides.get(exchange)
        if per_exchange:
            override = per_exchange.get(timeframe.value)
            if override is not None:
                return _override_to_indicator_params(override)
    return config.to_indicator_params()
```

**Resolution precedence** (first match wins):

1. `config.exchange_overrides[exchange][timeframe.value]` → build
   `IndicatorParams` from the override.
2. Global fallback — `config.to_indicator_params()` (the flatten
   of the top-level `ema:` + `atr:` blocks).

The live engine **does not** consult overrides (per design OOS-7).
Only the Phase-15 backtest signal-generator
(`fundamentals/signal_generator.py`) reads them. This is by design:
backtest knobs that diverge from production should not silently
move production signals.

### 7.3 Validation

In `Config.model_validator`:

- **Timeframe keys** must be `"1D"` or `"1W"`. Intraday timeframes
  are deferred (Phase 15 D3).
- **Exchange keys** must appear in `config.market_hours`. A typo
  like `NESE` would silently produce empty overrides — caught at
  load time.

## 8. Wiring at startup (`app.py`)

```python
def build_app():
    config = load_config(os.environ.get("TRIP_EMA_CONFIG", "config.yaml"))

    # DB engine
    db_engine = build_db_engine(config)

    # Caches
    cache               = SignalCache()
    current_signals_cache = CurrentSignalsCache()

    # Lifecycle priming
    prime_cache_from_db(db_engine=db_engine, cache=cache)
    initial_signals = current_signals_repo.list_all(conn)
    current_signals_cache.rebuild_from(initial_signals)

    # Orchestrator pool (per-market)
    orchestrator_pool = build_orchestrator_pool(
        config=config,
        db_engine=db_engine,
        cache=cache,
        current_signals_cache=current_signals_cache,
    )

    # HTTP app state
    app_state = AppState(
        config=config, db_engine=db_engine,
        cache=cache, current_signals_cache=current_signals_cache,
        orchestrator_pool=orchestrator_pool,
    )
    set_state(app_state)

    return create_app()
```

Config flows downward to every layer that needs it. Sub-sections
are passed through to their respective owners — `config.market_hours`
to the orchestrator pool, `config.transports.brevo` to the email
transport, `config.exchange_overrides` to the backtest generator.

## 9. Common pitfalls

1. **`TRIP_EMA_SMTP_HOST` is obsolete.** Microsoft disabled SMTP
   AUTH for personal Outlook accounts in 2025; Google made Gmail
   OAuth-only for new accounts in 2024. Brevo HTTP API is the only
   viable email transport. Any SMTP-related config block in `example.yaml`
   from before that change should be ignored.
2. **`Config` is `extra="forbid"`.** An unknown YAML key like
   `scheduling.compute_cadence_min:` (typo, missing `utes`) raises
   `ConfigError` at startup. The error message names the offending
   key.
3. **Brevo loading is fail-fast at startup, lazy at first send.**
   The startup check reads the key for validation and discards it.
   The transport reads it again on its first send and caches it
   thereafter. Absence of an `'Email transport: Brevo HTTP API'`
   log line right after startup is **not** a regression.
4. **`exchange_overrides` ignored by the live cycle.** Adding an
   override for NSE/1W doesn't change live behaviour. Only the
   backtest signal-generator consumes it.
5. **Migrations must run as the top-level user.** If you set
   `database.roles.engine` and remove privileges from the
   top-level user, `alembic upgrade head` at startup will fail.
6. **`market_hours.weekdays` is ISO 1-7.** Monday = 1, Sunday = 7.
   The validator sorts and de-duplicates. Empty list raises.
7. **HH:MM in local TZ.** `open: "09:15"` for NSE means 09:15 IST.
   The validator parses it as `dt_time.fromisoformat` — anything
   else (e.g. `"9:15 AM"`) raises at startup.

## 10. Testing

`tests/config/`:

- `test_loader.py` — fail-fast paths: missing file, malformed YAML,
  unset env vars, missing Brevo file.
- `test_schema.py` — Pydantic validators for every constraint.
- `test_brevo_secrets.py` — permission checks, format checks,
  shape checks.
- `test_exchange_overrides.py` — resolution precedence, fallback
  to global.
- `test_market_hours_validators.py` — HH:MM parsing, TZ resolution,
  weekday range.
- `test_alembic_bridge_no_db.py` — DB-free tests of the bridge's
  decision logic.

`tests/config/` is **DB-free** by design — it runs on every PR
without provisioning Postgres.

## 11. Extension paths

- **A new top-level config section.** Add a new sub-model in
  `schema.py`, add the field on `Config`, give it a default
  (so existing YAML files don't break), wire it into whichever
  layer needs it.
- **A new fail-fast secret.** Add a loader module like
  `brevo_secrets.py` for the new secret type; call it from
  `loader.py` between the Pydantic validation and the return.
- **Per-market override block.** The `exchange_overrides` pattern
  generalises — add a similar `dict[str, dict[str, ...]]` field
  to `Config` for the new override type, add a resolver in this
  package, expose it where needed.

## 12. Related packages

- `trip_ema/domain/` — provides `IndicatorParams` and `Timeframe`,
  which `Config` builds and consumes.
- `trip_ema/persistence/connection.py` — consumes `DatabaseConfig`
  + `DbRolesConfig` to build per-role engines.
- `trip_ema/scheduler/market_calendar.py` — consumes
  `holidays_country` + `holidays_override_file` via
  `MarketHoursConfig.to_calendar_config()`.
- `trip_ema/scheduler/tick_planner.py` — consumes
  `tick_schedule` + legacy fields via
  `MarketHoursConfig.to_tick_config()`.
- `trip_ema/notifications/email_transport.py` — consumes
  `transports.brevo.secrets_file` to load the API key on first
  send.
- `trip_ema/fundamentals/signal_generator.py` — consumes
  `exchange_overrides` via `resolve_indicator_params`.
- `configs/` (top-level repo dir) — operator-editable auxiliary
  files referenced from `config.yaml` (e.g.
  `nse_holidays_2026.yaml`).
