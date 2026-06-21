# `trip_ema/config/` — YAML config loader, schema, secrets

Single source of truth for runtime configuration. YAML on disk →
Pydantic-validated `Config` object → injected everywhere downstream.
Secrets are never in the file: the file names environment variables
or external secrets files.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports `Config`, `load_config`, `resolve_db_password` |
| `loader.py` | `load_config(path) -> Config`. Reads YAML, resolves env-var references for secrets, validates into `Config`. Raises `ConfigError` on any failure |
| `schema.py` | Pydantic v2 models mirroring the YAML one-to-one. Top-level `Config` + sub-schemas (`DatabaseConfig`, `EmaConfig`, `AtrConfig`, `MarketHoursConfig`, `TransportsConfig`, `OrchestratorConfig`, …) |
| `exchange_overrides.py` | `resolve_indicator_params(config, exchange, timeframe)` — Phase 15a per-(exchange, timeframe) overrides for the backtest signal generator. The live engine uses one global `IndicatorParams` |
| `brevo_secrets.py` | Loader for `secrets/brevo.yaml` (chmod-600, gitignored). Fail-fast at startup if missing, world/group-readable, or `api_key` prefix ≠ `xkeysib-` |
| `example.yaml` | Minimal example — only `database`, `ema`, `atr`, `market_hours` are required. Everything else has safe defaults |
| `config_example.yaml` | Extended example showing the optional fields and multi-market setup |

## What's required vs optional

Required: `database`, `ema`, `atr`, `market_hours`. Defaults exist for
transports, orchestrator, scheduler tick intervals, observability,
and all Phase 1+ fundamentals knobs. Durations default to minutes
unless a suffix says otherwise.

## Secrets handling

- DB password: env-var reference (`database.password_env: TRIP_EMA_DB_PASSWORD`).
  Unset env var is a hard failure at load time.
- Brevo API key: separate file via `transports.brevo.secrets_file`.
  Loaded lazily on first email send (not at boot — absence of a
  startup log line is not a regression).
- Admin bearer token: env (`TRIP_EMA_ADMIN_TOKEN`). When unset, write
  endpoints 401; the GUI degrades to read-only.

## Obsolete / do not use

- `TRIP_EMA_SMTP_HOST` and any raw-SMTP fields — Outlook personal
  accounts have SMTP AUTH disabled (5.7.139); Gmail is OAuth-only
  since Sept 2024. Brevo HTTP API is the only viable transport.
