# `tests/app/` — Tests for `trip_ema/app.py`

Covers the main-service entry's orchestration logic — the wiring of
config → DB engine → registry → cache → orchestrator pool → transports
that happens in `trip_ema.app:main`.

## Files

| File | Tests |
|---|---|
| `test_build_orchestrator_components.py` | The orchestrator-pool build sequence — one `MarketOrchestrator` per configured market; each gets the right calendar, hours, tick planner |

## Needs

- No DB (uses mocks / fakes for the components that would otherwise
  need one)
- No Qt

## What's intentionally **not** here

- The full app integration is implicitly tested by `tests/api/` and
  `tests/transports/http/` (those exercise a full booted app)
- This directory tests only the **build wiring** — does the right
  glue produce the right shape, not whether the resulting service
  serves traffic correctly
