# Configuration

Configuration is YAML, validated by Pydantic on load. The canonical
example with every field commented is at
[`trip_ema/config/example.yaml`](https://github.com/example/trip-ema/blob/main/trip_ema/config/example.yaml).

## Minimum required

Only four sections are mandatory:

- `database` — Postgres connection + password env var
- `ema` — the three EMA stages (defaults match the Pine indicator)
- `atr` — ATR length + band multipliers
- `market_hours` — at least one exchange's hours

Everything else has safe defaults. The example YAML calls out which
sections are optional.

## Passwords

Never put passwords in the YAML. The config holds the *name* of
an environment variable; the actual password is set in the
deployment environment. Example:

```yaml
database:
  password_env: TRIP_EMA_DB_PASSWORD
```

Then at deploy time:

```bash
export TRIP_EMA_DB_PASSWORD=$(vault read -field=password ...)
trip-ema --config config.yaml
```

`load_config()` validates that every referenced env var is set
at startup; missing values fail fast rather than at first DB call.

## Key sections

- `redis` — required for the notifier worker (Phase 9+)
- `notifier.max_attempts` — DLQ threshold (Phase 12, default 5)
- `database.roles` — per-process DB role overrides (Phase 12)
- `observability` — Prometheus + OpenTelemetry (Phase 10)
- `transports.circuit_breaker` — per-transport failure isolation
- `etl` — ETL-specific knobs (used by `trip-ema-etl` only)

See the example.yaml for every field and its default.
