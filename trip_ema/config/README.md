# `trip_ema/config/` — Configuration schema, loader, and resolution

The single source of truth for what `config.yaml` is allowed to contain
and how each block is parsed.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `loader.py` | `load_config(path)` — read YAML, resolve `*_env` env-var references, instantiate Pydantic `Config` |
| `schema.py` | Pydantic models for every config block — `DatabaseConfig`, `EmaConfig`, `AtrConfig`, `MarketHoursEntry`, `HttpConfig`, `EtlConfig`, `RedisConfig`, `NotifierConfig`, `ObservabilityConfig`, `TransportsConfig` (with sub-models `CircuitBreakerConfig`, `BrevoConfig`), `DbRolesConfig`, `ExchangeIndicatorOverride`, ... |
| `exchange_overrides.py` | (Phase 15a) `resolve_indicator_params(config, exchange, timeframe)` — checks `exchange_overrides[exchange][timeframe]`, falls back to the global `ema:`/`atr:` block |
| `brevo_secrets.py` | Load + validate Brevo API key from a YAML file outside the main config (`secrets/brevo.yaml`). Called from `load_config()` — fail-fast on missing file, world/group-readable perms, missing `api_key` field, or wrong key prefix (`xkeysib-`) |
| `config_example.yaml` + `example.yaml` | Example configs for documentation / tests |

## Lifecycle

```
load_config(path) 
   yaml.safe_load
   resolve_env_vars            # password_env: TRIP_EMA_DB_PWD  ->  read os.environ["TRIP_EMA_DB_PWD"]
   Config(**parsed)            # Pydantic validation, model_validators run
   return Config (frozen)      # consumed by app.py, CLI, GUI, etc.
```

The resulting `Config` object is immutable. Config edits require a
restart — there's no hot-reload path.

### Fail-fast at startup

Two classes of secrets are resolved during `load_config()` and crash
the process loudly if missing or malformed:

- **Env-var-referenced secrets** (DB password, Redis password, admin
  token) — named via `*_env` fields. An unset env var raises
  `ConfigError` at boot, not a silent fallback at connect time.
- **File-based secrets** (Brevo API key) — named via a path field
  (`transports.brevo.secrets_file`). The file must exist, must not be
  world/group-readable, and must contain a well-formed `api_key`
  field with the expected prefix (`xkeysib-` for Brevo).

The contract is uniform: a misconfigured secret crashes at boot,
never silently substitutes a no-op (`NullTransport` or similar) that
would lose notifications between configuration mistake and operator
intervention. This closes the bug class introduced by the prior
SMTP transport's `TRIP_EMA_SMTP_HOST`-unset code path.

## Phase 15a addition: `exchange_overrides`

Optional top-level block:

```yaml
exchange_overrides:
  NSE:
    "1W":
      ema: { ... }
      atr: { ... }
    "1D":
      ema: { ... }
      atr: { ... }
```

Consumed **only** by the backtest signal generator
(`trip-ema-etl generate-signals` / `POST /v1/admin/signals/generate`).
The live engine continues to use the global `ema:`/`atr:` block until
Phase 17 migrates it (per design OOS-7).

Validation:
- `_validate_exchange_overrides` rejects unknown exchanges (not in
  `market_hours`) and unknown timeframes (not in `{"1D", "1W"}`)
- Fails at config-load time with a clear error rather than at runtime

## Validation patterns

- `model_validator(mode="after")` — for cross-block validation
  (e.g. "if `redis:` is set, `notifier.queue_name` is required")
- `field_validator` — for single-field constraints (port ranges,
  cron expressions, etc.)
- Frozen dataclasses everywhere — no mutation post-validation

## Tests

- `tests/config/test_loader.py` — env-var resolution, YAML parsing
- `tests/config/test_schema_step2.py` — model validators
- `tests/config/test_exchange_overrides.py` — Phase 15a resolution +
  validation (12 tests)
- `tests/config/test_db_roles.py` — Phase 12 DB-roles config block
- `tests/config/test_alembic_bridge_no_db.py` — alembic upgrade bridge
  decision logic
