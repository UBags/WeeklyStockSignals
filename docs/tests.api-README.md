# `tests/api/` — Tests for `trip_ema/api/`

Covers priming (`prime_all_tickers`) and the two HTTP read paths (cached
+ stateless).

## Files

| File | Tests |
|---|---|
| `conftest.py` | DB fixture + `AppState` builder for the integration tests below |
| `test_cached.py` | Cached read path against `SignalCache` |
| `test_stateless.py` | Stateless compute path — caller-supplied bars, ephemeral engine, no cache touch |
| `test_lifecycle.py` | Priming behavior, idempotency, reprime scenarios |
| `test_end_to_end.py` | Full priming → compute → cached read sequence; the "happy path" smoke for the read side |

## Needs

- Postgres (provisioned via `pytest-postgresql`)
- No Qt

## What's intentionally **not** here

- The HTTP transport / route handlers themselves — those are in
  `tests/transports/http/`
- The compute scheduler — in `tests/scheduler/`
- Engine math — in `tests/engine/` + `tests/indicators/`

This directory tests the **lifecycle + read-shape** layer, not the
underlying engine.
