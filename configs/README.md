# `configs/` — Runtime configuration files (non-secret)

Loaded by the `trip-ema` service at startup. The main `config.yaml` lives
at the repo root; this directory holds **auxiliary** YAML files that
`config.yaml` references by path.

## Contents

| File | Referenced by | Purpose |
|---|---|---|
| `nse_holidays_2026.yaml` | `market_hours[].holidays_override_file` in `config.yaml` | Operator overrides on top of python-holidays' built-in IN holidays — adds bourse-specific closures (e.g. Muhurat trading day, special sessions) that python-holidays doesn't model |

## Override-file schema

```yaml
extra_holidays:
  - 2026-01-26   # operator-added closures
special_trading_days:
  - 2026-11-12   # special sessions (e.g. Muhurat Trading on Diwali)
```

`extra_holidays` adds dates that should be treated as closed even if
python-holidays returns "open"; `special_trading_days` does the inverse.

## Conventions

- Files here are **non-secret** — credentials live in environment
  variables, referenced from `config.yaml` via `*_env` keys.
- Files are read at startup, not hot-reloaded. Restart the service to
  pick up changes.
- The path in `config.yaml` is interpreted relative to the directory
  the service is started from (typically the repo root).

## Related modules

- `trip_ema/etl/holidays_calendar.py` — loads & merges
  python-holidays with the override file
- `trip_ema/scheduler/market_calendar.py` — wraps the above for
  "is this a trading day?" queries from the orchestrator
