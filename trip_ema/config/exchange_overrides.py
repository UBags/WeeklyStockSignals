"""Per-(exchange, timeframe) indicator parameter overrides (Phase 15a).

The live engine uses one global IndicatorParams loaded from
`config.ema` + `config.atr`. The backtest signal-generator (Phase 15)
introduces per-exchange, per-timeframe tunability so operators can
align the backtest output with whatever they actually run for each
market. Per design decision D4, the live engine continues to use the
global block until a later phase migrates it; ONLY the backtest path
consults exchange_overrides.

YAML shape (added at the top level of config.yaml):

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

Resolution rule (`resolve_indicator_params`):
  1. If `config.exchange_overrides[exchange][timeframe.value]` exists,
     build IndicatorParams from that override.
  2. Otherwise fall back to `config.to_indicator_params()` (the
     existing flatten of the top-level `ema:` + `atr:` blocks).

Backward compatibility: a config.yaml with no `exchange_overrides:`
block keeps working unchanged. The resolver returns the global
params for every (exchange, timeframe).

Validation (in Config.model_validator below):
  - Timeframe keys must be "1D" or "1W" (Phase 15 supports only
    these; intraday timeframes are deferred per design D3).
  - Exchange keys must be present in `config.market_hours` — typos
    silently producing empty overrides would be a footgun.
  - Bad config fails at load time with a clear error message,
    matching the convention of every other config validator in
    this package.
"""

from __future__ import annotations

from ..domain import IndicatorParams, Timeframe

# Imports kept narrow so this module stays a leaf in the import
# graph (consumed by schema and signal_generator, depends only on
# domain). The actual Config class is referenced by name (string
# annotation) in resolve_indicator_params's signature to avoid a
# circular import with schema.py.


# Timeframes the backtest pipeline supports in Phase 15. Intraday
# (15m/1h/2h) is deferred per design D3 — those timeframes require
# ETL changes to populate ohlcv_bars at those resolutions.
SUPPORTED_BACKTEST_TIMEFRAMES: frozenset[str] = frozenset({"1D", "1W"})


def resolve_indicator_params(
    config,  # type: "Config" — string-annotated to avoid circular import
    exchange: str,
    timeframe: Timeframe,
) -> IndicatorParams:
    """Return the IndicatorParams to use for (exchange, timeframe)
    during backtest signal generation.

    Precedence:
        1. `config.exchange_overrides[exchange][timeframe.value]`
           if present — build params from that override.
        2. `config.to_indicator_params()` (global fallback) otherwise.

    The function does NOT validate that `exchange` is a configured
    exchange; that's the caller's responsibility (the signal
    generator calls `validate_exchange` from etl.exchanges before
    reaching this code). If `exchange` isn't in
    exchange_overrides, the fallback kicks in — this is the
    correct behavior for an exchange with no per-market tuning.
    """
    overrides = getattr(config, "exchange_overrides", None)
    if overrides:
        per_exchange = overrides.get(exchange)
        if per_exchange:
            override = per_exchange.get(timeframe.value)
            if override is not None:
                return _override_to_indicator_params(override)
    return config.to_indicator_params()


def _override_to_indicator_params(override) -> IndicatorParams:
    """Flatten an ExchangeIndicatorOverride into IndicatorParams.

    Mirrors Config.to_indicator_params() but reads from the
    override's ema/atr sub-blocks rather than the top-level ones.
    """
    return IndicatorParams(
        med_base=override.ema.med.base,
        med_s1=override.ema.med.s1,
        med_s2=override.ema.med.s2,
        med_final=override.ema.med.final,
        long_base=override.ema.long.base,
        long_s1=override.ema.long.s1,
        long_s2=override.ema.long.s2,
        long_final=override.ema.long.final,
        longest_base=override.ema.longest.base,
        longest_s1=override.ema.longest.s1,
        longest_s2=override.ema.longest.s2,
        longest_final=override.ema.longest.final,
        atr_length=override.atr.length,
        inner_mult=override.atr.inner_mult,
        mid_mult=override.atr.mid_mult,
        outer_mult=override.atr.outer_mult,
        volume_sma_length=override.ema.volume_sma_length,
    )
