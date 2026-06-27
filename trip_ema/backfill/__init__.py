"""V1 backfill — operator-facing pipeline for recomputing the V1
state + signals from a historical date.

Step 11b1 contents:
  - `pipeline.backfill` — the 10-stage orchestration entry point
  - `pipeline.BackfillResult` — per-stage counters + failure
    indicator returned to the caller

Step 11b2 will add the CLI subcommand in `trip_ema.etl.cli` that
parses operator flags and invokes `pipeline.backfill`.

Pipeline stages (per design in Step 11 planning):
  1. (if refetch_bars) yfinance bar refetch for scoped tickers
  2. stock_indicator_state full recompute for scoped tickers
  3. stock_indicator_enrichment full recompute for scoped tickers
  4. IIV refresh for all industries, full date range
  5. industry_indicator_state full recompute for all industries
  6. industry_indicator_enrichment full recompute for all industries
  7. DELETE historical_signals for scoped tickers from from_monday
  8. Walk Mondays, derive at each, INSERT historical_signals
  9. Rebuild current_signals for scoped tickers (latest-bar flip
     semantics — UPSERT on flip, DELETE on no-flip)
 10. (cache rebuild is a service-side concern; backfill CLI doesn't
     touch the in-memory cache — service restart picks it up)

Industry stages (4, 5, 6) always run for ALL industries regardless
of which stocks were in scope (per Step 11 design — "always do all
industries; keeps it neat").

Failure handling: if a stage raises, the run is marked finished in
`backfill_runs` (so the live cycle resumes) and the BackfillResult
carries `failed_stage` + `error_message`. Operator is responsible
for cleanup. Per-stage operations are idempotent (UPSERT for state,
DELETE+INSERT for historical_signals), so a clean re-run produces
consistent end state.
"""
from __future__ import annotations

from .pipeline import BackfillResult, backfill

__all__ = ["BackfillResult", "backfill"]
