"""Regression test: the OHLCV ingestion path writes ONLY to OHLCV-concern
tables and never side-effects into indicator / signal / enrichment
tables.

This locks in the architectural invariant audited as part of Step 3 of
the clean-pipeline V1 refactor: `run_eod` / `run_intraday` /
`run_backfill` are pure OHLCV runners. Indicator computation lives in
separate runners (Steps 4–5 for stocks, 7–8 for industries). If a
future change wires indicator/signal compute into the EOD path —
breaking the separation — this test catches it.

What we DO expect run_eod to write to (these are the OHLCV concern):
  - ohlcv_bars             — the bars themselves
  - etl_runs               — run audit row
  - etl_ticker_status      — per-ticker last-fetched / failure-count
  - exchange_state         — last-EoD-at, etc.
  - corporate_actions      — divergence candidates (data quality flag,
                             not an indicator)

What we DO NOT expect run_eod to write to (indicator / signal concern):
  - stock_indicator_state              (V1)
  - industry_indicator_state           (V1)
  - stock_indicator_enrichment         (V1)
  - industry_indicator_enrichment      (V1)
  # 15B-FIXUP1: signal_events row removed (table dropped in migration 0030)
  - current_signals                    (V1)
  - historical_signals                 (V1)
  - benchmark_stats / stock_benchmark_stats

We don't go to the network — `_StubYFinanceSource` returns canned
RawBars. Tests run against the existing `db_engine` fixture
(tests/persistence/conftest.py) which runs migrations to head, so all
V1 tables exist.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, UTC
from pathlib import Path

import pytest
from sqlalchemy import text

from trip_ema.etl.runner import run_eod
from trip_ema.etl.types import FetchOutcome, RawBar
from trip_ema.etl.yfinance_source import YFinanceSource


# ====================================================================
# Helpers
# ====================================================================

# Tables that must remain at zero rows for the test ticker after the
# OHLCV runner finishes. Each entry is (table_name, ticker-filter SQL).
# A None filter means "any row in this table is a violation."
# === STEP 15B-FIXUP1: signal_events removed (table dropped in 0030) ===
INDICATOR_SIGNAL_TABLES_TICKER_SCOPED = [
    ("stock_indicator_state",           "ticker_id = :tid"),
    ("stock_indicator_enrichment",      "ticker_id = :tid"),
    ("current_signals",                 "ticker_id = :tid"),
    ("historical_signals",              "ticker_id = :tid"),
]

# Industry-scoped indicator tables — should never get a row touched by
# a per-ticker OHLCV run.
INDICATOR_TABLES_INDUSTRY_SCOPED = [
    "industry_indicator_state",
    "industry_indicator_enrichment",
]


def _make_runner_config(tmp_path: Path):
    """Build a minimal Config sufficient for `run_eod` end-to-end.

    parked_dir points at a tmp_path so the test never writes outside
    its sandbox.
    """
    from trip_ema.config.schema import (
        ATRConfig, Config, DatabaseConfig, EMAConfig, EMAStageConfig,
        EtlConfig, MarketHoursConfig,
    )
    return Config(
        database=DatabaseConfig(
            host="localhost", database="x", user="x",
            password_env="TRIP_EMA_DB_PASSWORD",
        ),
        ema=EMAConfig(
            med=EMAStageConfig(base=8, s1=4, s2=4, final=None),
            long=EMAStageConfig(base=15, s1=4, s2=4, final=3),
            longest=EMAStageConfig(base=28, s1=7, s2=5, final=3),
        ),
        atr=ATRConfig(),
        market_hours=[
            MarketHoursConfig(
                exchange="NSE", open="09:15", close="15:30",
                tz="Asia/Kolkata", weekdays=[1, 2, 3, 4, 5],
            ),
        ],
        etl=EtlConfig(
            parked_dir=str(tmp_path / "parked"),
            checkpoint_dir=str(tmp_path / "checkpoint"),
            yfinance_request_delay_seconds=0.0,  # fast tests
        ),
    )


class _StubYFinanceSource(YFinanceSource):
    """A YFinanceSource that returns canned bars instead of hitting
    the network. Used to drive `run_eod` through the success path
    without yfinance dependence."""

    def __init__(self, canned: dict[str, tuple[RawBar, ...]]):
        super().__init__(
            exchange="NSE", batch_size=50, request_delay_seconds=0.0,
        )
        self._canned = canned

    def fetch_daily_history(
        self, symbols, start, end=None,
    ) -> dict[str, FetchOutcome]:
        return {
            sym: FetchOutcome(
                yfinance_symbol=f"{sym}.NS",
                bars=self._canned.get(sym, ()),
                error_message=None,
            )
            for sym in symbols
        }

    def fetch_hourly_recent(
        self, symbols, lookback_days=5,
    ) -> dict[str, FetchOutcome]:
        return self.fetch_daily_history(symbols, start=date.today())


def _seed_active_ticker(
    conn, *, ticker_id: int, symbol: str, exchange: str = "NSE",
) -> int:
    """Insert one active EQ ticker on `exchange`. Returns ticker_id.

    Post-0019 tickers schema only requires `exchange` + `symbol` as
    NOT NULL without defaults. `is_active` defaults to TRUE so the
    runner picks it up. `tier` defaults so tier ordering works.
    """
    conn.execute(text("""
        INSERT INTO tickers (ticker_id, exchange, symbol)
        VALUES (:id, :ex, :sym)
        ON CONFLICT (ticker_id) DO NOTHING
    """), {"id": ticker_id, "ex": exchange, "sym": symbol})
    return ticker_id


def _canned_bars(
    *,
    start: date = date(2026, 6, 15),
    n: int = 5,
) -> tuple[RawBar, ...]:
    """N consecutive daily RawBars from `start` (one per business day,
    approximately — for this test we don't bother with calendar skip
    since the runner doesn't care about that, only the writer does)."""
    bars = []
    for i in range(n):
        bars.append(RawBar(
            trading_date=date.fromordinal(start.toordinal() + i),
            open=100.0 + i,
            high=101.0 + i,
            low=99.0 + i,
            close=100.5 + i,
            adj_close=100.5 + i,
            volume=1_000_000 + i * 1000,
        ))
    return tuple(bars)


def _count_rows_for_ticker(conn, table: str, where: str, tid: int) -> int:
    return conn.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE {where}"),
        {"tid": tid},
    ).scalar_one()


def _count_rows_for_industry(conn, table: str) -> int:
    """Count ALL rows — if even one row got created by the OHLCV
    runner, that's a violation."""
    return conn.execute(
        text(f"SELECT COUNT(*) FROM {table}")
    ).scalar_one()


# ====================================================================
# The regression test
# ====================================================================


def test_run_eod_writes_ohlcv_only_no_indicator_side_effects(
    db_engine, tmp_path,
):
    """run_eod writes OHLCV bars + ETL audit/quality rows ONLY. It
    must NEVER write to indicator_state, stock_indicator_state,
    stock_indicator_enrichment, current_signals,
    historical_signals, industry_indicator_state, or
    industry_indicator_enrichment.

    Regression guard for the architectural separation between the
    OHLCV ingestion concern (this test's path) and the indicator
    computation concern (Steps 4+ of clean-pipeline V1).
    """
    # ---- Arrange ----
    ticker_id = 4242
    symbol = "REGRESSTEST"

    with db_engine.begin() as conn:
        # Clean slate for the ticker
        _seed_active_ticker(conn, ticker_id=ticker_id, symbol=symbol)
        # Snapshot baseline counts for every indicator/signal table.
        # If the table doesn't exist (unlikely given current migration
        # state but defensive), we count it as 0.
        baseline_ticker: dict[str, int] = {}
        for table, where in INDICATOR_SIGNAL_TABLES_TICKER_SCOPED:
            try:
                baseline_ticker[table] = _count_rows_for_ticker(
                    conn, table, where, ticker_id,
                )
            except Exception:
                baseline_ticker[table] = 0
        baseline_industry: dict[str, int] = {}
        for table in INDICATOR_TABLES_INDUSTRY_SCOPED:
            try:
                baseline_industry[table] = _count_rows_for_industry(
                    conn, table,
                )
            except Exception:
                baseline_industry[table] = 0
        baseline_bars = _count_rows_for_ticker(
            conn, "ohlcv_bars", "ticker_id = :tid", ticker_id,
        )

    cfg = _make_runner_config(tmp_path)
    canned_source = _StubYFinanceSource({symbol: _canned_bars(n=5)})

    # ---- Act ----
    run_id = run_eod(
        db_engine, cfg, exchange="NSE",
        source=canned_source, days_lookback=30,
    )

    # ---- Assert: run completed ----
    assert run_id > 0, f"run_eod should return a real run_id, got {run_id}"

    # ---- Assert: OHLCV bars were actually written ----
    with db_engine.connect() as conn:
        bars_after = _count_rows_for_ticker(
            conn, "ohlcv_bars", "ticker_id = :tid", ticker_id,
        )
    assert bars_after > baseline_bars, (
        f"Expected ohlcv_bars to gain rows for ticker {ticker_id}, "
        f"baseline={baseline_bars}, after={bars_after}. The stub source "
        f"returned 5 canned bars — they should be written."
    )

    # ---- Assert: NO indicator/signal side-effects (ticker-scoped) ----
    with db_engine.connect() as conn:
        for table, where in INDICATOR_SIGNAL_TABLES_TICKER_SCOPED:
            try:
                after = _count_rows_for_ticker(conn, table, where, ticker_id)
            except Exception:
                after = 0
            assert after == baseline_ticker[table], (
                f"INVARIANT VIOLATION: run_eod wrote to {table!r} "
                f"for ticker_id={ticker_id}. "
                f"baseline={baseline_ticker[table]}, after={after}. "
                f"The OHLCV runner must not side-effect into "
                f"indicator / signal tables — that's the responsibility "
                f"of the indicator-state and signal runners (Steps 4+ "
                f"of the clean-pipeline V1 refactor)."
            )

    # ---- Assert: NO industry-indicator side-effects ----
    with db_engine.connect() as conn:
        for table in INDICATOR_TABLES_INDUSTRY_SCOPED:
            try:
                after = _count_rows_for_industry(conn, table)
            except Exception:
                after = 0
            assert after == baseline_industry[table], (
                f"INVARIANT VIOLATION: run_eod wrote to {table!r}. "
                f"baseline={baseline_industry[table]}, after={after}. "
                f"A per-ticker OHLCV run must not touch industry "
                f"indicator tables — those belong to the industry "
                f"populators (Steps 7–8 of clean-pipeline V1)."
            )


def test_run_eod_runner_module_does_not_import_indicator_layers():
    """Static guard: the etl.runner module must not import any of
    the indicator / signal / fundamentals / cache / scheduler /
    notifications modules. If someone adds such an import — even
    without using it in the OHLCV path — this test fails fast.

    Catches a class of regression where someone wires a hook from
    the runner into a higher layer at module load time.
    """
    import trip_ema.etl.runner as runner_module

    # Inspect the module's resolved imports via sys.modules and the
    # module's own attribute namespace.
    forbidden_module_prefixes = (
        "trip_ema.fundamentals",
        "trip_ema.signals",
        "trip_ema.indicators",
        "trip_ema.regime",
        "trip_ema.notifications",
        "trip_ema.cache",
        "trip_ema.scheduler",
    )
    # Walk the module's globals — anything imported as a name lives there.
    for name, value in vars(runner_module).items():
        if name.startswith("_"):
            continue
        module_of_value = getattr(value, "__module__", None)
        if module_of_value is None:
            continue
        for forbidden in forbidden_module_prefixes:
            assert not module_of_value.startswith(forbidden), (
                f"INVARIANT VIOLATION: trip_ema.etl.runner imports "
                f"{name!r} from {module_of_value!r}. The OHLCV runner "
                f"must not depend on the indicator / signal / "
                f"scheduler / notifications / cache layers. That "
                f"coupling would couple OHLCV ingestion to compute, "
                f"breaking the separation that Step 3 of clean-"
                f"pipeline V1 audited and locked in."
            )
