# `trip_ema/domain/` — Domain types (Layer 1, pure)

Immutable types — dataclasses and enums — used everywhere else in the
codebase. Pure Python, zero dependencies on persistence, math, scheduling,
or transports. The bottom of the layer stack: every other layer may
import from `domain/`, but `domain/` imports from nothing in-package.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marker |
| `ohlcv.py` | `OHLCV` — single bar (timestamp + open/high/low/close/volume), the fundamental input type for the engine |
| `state.py` | `BarState` — engine output per bar (EMAs, ATR, bands, core_regime, display_regime). `Timeframe` enum (`1D`, `1W`, plus `15m`/`1h`/`2h` reserved for Phase 16) |
| `events.py` | `ChangeSignal` enum (`BUY`, `SELL`, `EXIT_FROM_BUY`, `EXIT_FROM_SELL`, `NO_CHANGE`). `SignalEvent` — a row destined for `signal_events` (live) or the backtest JSON |

## Conventions

- **Frozen dataclasses** (`@dataclass(frozen=True, slots=True)`) — no
  mutation after construction; the engine builds a new `BarState` per
  bar rather than mutating
- **No NumPy / Pandas at this layer** — types are plain Python so they
  can be sent over JSON, persisted via SQLAlchemy, or sent across
  process boundaries cheaply
- **Enums use plain strings** at the DB boundary (so columns are
  `VARCHAR`, not Postgres `ENUM TYPE`), avoiding a translation layer
  at every read/write. The Python-side `enum.StrEnum` is for type
  safety only

## Engine-state shape

`BarState` is the most-touched type in the codebase. Contains:
- `timestamp`, `close`, `volume` from the source bar
- Three EMAs: `med_ema`, `long_ema`, `longest_ema`
- ATR + six bands: `atr`, `upper_inner_band`, `lower_inner_band`,
  `upper_mid_band`, `lower_mid_band`, `upper_outer_band`, `lower_outer_band`
- `core_regime` and `display_regime` (BULLISH / NEUTRAL / BEARISH; the
  former is "raw"; the latter is the gated version downstream actually
  reads)

## Tests

`domain/` types have no dedicated test file — they're exercised
indirectly by every test that touches engine or persistence. The
shape is enforced by Pydantic / dataclass machinery rather than
runtime checks.
