"""End-to-end integration test for the Phase 11 chart flow.

Exercises the full pipeline from "raw bars in Postgres" to "JSON
response from GET /v1/signals/.../history":

  1. Run the legacy migrator (via the db_engine fixture) → tables exist
  2. Run the Alembic baseline-stamp bridge → alembic_version row present
  3. Insert a small synthetic ticker + ~500 daily bars covering a real
     trend so the engine generates BUY/SELL events
  4. Prime the TickerRegistry (the same code path `trip-ema` startup uses)
  5. Run ComputeScheduler.run_once → signal_events get written, cache
     gets populated
  6. Issue HTTP requests via TestClient with dependency overrides:
       GET /v1/signals/{exchange}/{symbol}            (latest signal)
       GET /v1/signals/{exchange}/{symbol}/history    (chart replay)
  7. Assert response shape — bar count is sensible, indicator values
     are non-None after warmup, regime labels valid, signal_events
     align with bar timestamps.

This isn't a true cross-process e2e (we don't actually spawn
`trip-ema-etl` and `trip-ema` as subprocesses). Doing that would
require real yfinance access and full subprocess orchestration —
both expensive to engineer and brittle in CI. This service-level
integration test catches what matters in practice:

  - Schema-shape mismatches between migrations and the engine's
    persistence layer
  - Priming → registry → cache wiring bugs
  - Engine-replay-via-`/history` endpoint breakage
  - Alembic-bridge interaction with the legacy migrator state

Lives in tests/api/ so it runs in the Postgres-backed CI job
(`postgres-integration` in .github/workflows/tests.yml). The slow
job is gated to main-branch pushes and manual dispatch.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text


# ---------- Test data: synthetic bars with a clear trend ----------

NUM_DAYS = 500   # ~2 years of daily bars before weekend filtering


def _generate_synthetic_bars(start_ts: datetime) -> list[dict]:
    """Generate up to NUM_DAYS daily bars (weekends skipped) with a
    synthetic price series containing a clear up-trend, sideways
    middle, and down-trend.

    Designed so the engine produces at least one BUY and one SELL
    signal after warmup completes. Returns dicts ready for direct
    INSERT to ohlcv_bars."""
    bars = []
    price = 100.0
    for i in range(NUM_DAYS):
        ts = start_ts + timedelta(days=i)
        if ts.weekday() >= 5:
            continue
        if i < 150:
            drift = 0.4
            noise = math.sin(i * 0.3) * 0.5
        elif i < 300:
            drift = 0.0
            noise = math.sin(i * 0.2) * 1.5
        else:
            drift = -0.3
            noise = math.cos(i * 0.25) * 0.4

        open_ = price
        price = max(price + drift + noise, 10.0)
        close = price
        high = max(open_, close) + abs(noise) * 0.5
        low = min(open_, close) - abs(noise) * 0.5
        volume = 100000.0 + (i * 137 % 50000)

        bars.append({
            "timeframe": "1D",
            "bar_timestamp": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        })
    return bars


def _insert_ticker_and_bars(engine, exchange: str, symbol: str) -> int:
    """Insert a ticker row + synthetic bars. Returns ticker_id.

    The synthetic series spans NUM_DAYS calendar days, ending ~today.
    We anchor relative to date.today() (rather than a hardcoded
    datetime like 2024-01-01) because the ComputeScheduler's
    gap-detector compares the high-water bar timestamp against today,
    and once the gap exceeds a few weeks the scheduler refuses to
    advance the engine, leaving `latest_change=NO_CHANGE` and breaking
    every downstream assertion. Mirrors the `_bar_seed_start` helper
    used in tests/scheduler/test_compute_scheduler.py."""
    # End the series one calendar day before today, then walk back
    # NUM_DAYS. This keeps the latest bar within the gap-detector's
    # tolerance window without overlapping with today's potentially
    # still-open trading day.
    today_utc = datetime.now(timezone.utc).date()
    end_date = datetime.combine(
        today_utc, datetime.min.time(), tzinfo=timezone.utc,
    ) - timedelta(days=1)
    start_ts = end_date - timedelta(days=NUM_DAYS - 1)
    bars = _generate_synthetic_bars(start_ts)

    with engine.begin() as conn:
        result = conn.execute(
            text("""
                INSERT INTO tickers (exchange, symbol, instrument, full_name)
                VALUES (:exchange, :symbol, 'EQ', :name)
                RETURNING ticker_id
            """),
            {"exchange": exchange, "symbol": symbol, "name": f"{symbol} Test Inc"},
        )
        ticker_id = result.scalar()
        conn.execute(
            text("""
                INSERT INTO ohlcv_bars
                  (ticker_id, timeframe, bar_timestamp, open, high, low, close, volume)
                VALUES
                  (:tid, :timeframe, :bar_timestamp,
                   :open, :high, :low, :close, :volume)
            """),
            [{**bar, "tid": ticker_id} for bar in bars],
        )
    return ticker_id


# ---------- The test ----------


class TestEndToEndChartFlow:
    """A real-but-fast service-level integration test.

    bars → prime → compute → cache → HTTP /history endpoint.
    """

    def test_bars_to_chart_history_full_pipeline(self, db_engine):
        # ---- Step 1: tables exist (db_engine fixture ran migrations) ----
        #
        # Phase 12 made Alembic the exclusive migration system; the
        # `db_engine` fixture in tests/persistence/conftest.py runs
        # `alembic upgrade head` on a fresh DB, which produces the
        # `alembic_version` bookkeeping table (NOT `schema_migrations`,
        # which is the legacy SQL-runner's table — that only appears
        # if the DB went through the legacy migrator first).
        with db_engine.connect() as conn:
            tables = conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public'"
            )).scalars().all()
        for required in ("tickers", "ohlcv_bars", "signal_events",
                         "alembic_version"):
            assert required in tables, f"Missing table: {required}"

        # ---- Step 2: Alembic bookkeeping is already at head ----
        #
        # Pre-Phase-12 this test exercised `stamp_baseline_if_needed`
        # — the migration of a legacy DB into Alembic's tracking. The
        # bridge is now a no-op against fresh-Alembic DBs (the only
        # kind this fixture produces), so we just sanity-check that
        # Alembic has a version recorded.
        from trip_ema.persistence.alembic_bridge import (
            _alembic_has_a_version, stamp_baseline_if_needed,
        )
        try:
            import alembic  # noqa: F401
            alembic_installed = True
        except ImportError:
            alembic_installed = False

        if alembic_installed:
            assert _alembic_has_a_version(db_engine) is True
            # Bridge is idempotent — no-ops on a DB that's already at
            # an Alembic version.
            assert stamp_baseline_if_needed(db_engine) is False

        # ---- Step 3: insert synthetic ticker and bars ----
        ticker_id = _insert_ticker_and_bars(
            db_engine, exchange="NSE", symbol="E2ETEST",
        )
        assert ticker_id > 0

        with db_engine.connect() as conn:
            bar_count = conn.execute(text(
                "SELECT COUNT(*) FROM ohlcv_bars WHERE ticker_id = :tid"
            ), {"tid": ticker_id}).scalar()
        assert bar_count > 300, f"Expected >300 weekday bars, got {bar_count}"

        # ---- Step 4: build registry + cache + prime ----
        from trip_ema.api import prime_all_tickers
        from trip_ema.cache import SignalCache
        from trip_ema.domain import IndicatorParams, Timeframe
        from trip_ema.registry import TickerRegistry

        registry = TickerRegistry(
            IndicatorParams(),
            [Timeframe.WEEKLY],
            history_limit=1000,
            state_history_retain=600,
        )
        cache = SignalCache()
        prime_result = prime_all_tickers(
            db_engine=db_engine, registry=registry, cache=cache,
        )
        # `prime_all_tickers` returns a LifecycleResult, not a raw
        # PrimeResult. The actual ticker count lives at
        # `.prime_result.tickers_primed`. (This test was originally
        # written against an older API where the function returned the
        # PrimeResult directly.)
        assert prime_result.prime_result.tickers_primed >= 1, (
            f"prime_all_tickers should have primed our test ticker; "
            f"got {prime_result}"
        )

        # ---- Step 5: run a compute pass ----
        from trip_ema.scheduler import ComputeScheduler
        scheduler = ComputeScheduler(
            db_engine=db_engine, registry=registry, cache=cache,
        )
        scheduler.run_once()

        # ---- Step 6: HTTP layer via TestClient ----
        from trip_ema.transports.http import create_app
        from trip_ema.transports.http.dependencies import (
            get_cache, get_compute_scheduler, get_config,
            get_db_engine, get_registry,
        )
        from tests.transports.http.conftest import _build_test_config

        app = create_app()
        config = _build_test_config()
        app.dependency_overrides[get_db_engine] = lambda: db_engine
        app.dependency_overrides[get_cache] = lambda: cache
        app.dependency_overrides[get_registry] = lambda: registry
        app.dependency_overrides[get_compute_scheduler] = lambda: scheduler
        app.dependency_overrides[get_config] = lambda: config

        with TestClient(app) as client:
            # /healthz — sanity
            resp = client.get("/healthz")
            assert resp.status_code == 200

            # GET /v1/signals/NSE/E2ETEST — latest signal from cache
            resp = client.get("/v1/signals/NSE/E2ETEST")
            assert resp.status_code == 200, f"Body: {resp.text}"
            body = resp.json()
            # Response shape: ticker info is nested under `ticker`,
            # not at the top level. The schema (TerseResponsePayload)
            # exposes `ticker: TickerRefPayload` with `exchange` and
            # `symbol` fields. (Earlier versions of this test asserted
            # body["exchange"] directly — that was the response shape
            # before the schemas were refactored.)
            assert body["ticker"]["exchange"] == "NSE"
            assert body["ticker"]["symbol"] == "E2ETEST"

            # GET /v1/signals/NSE/E2ETEST/history — chart replay
            resp = client.get(
                "/v1/signals/NSE/E2ETEST/history",
                params={"years": 2},
            )
            assert resp.status_code == 200, f"Body: {resp.text}"
            hist = resp.json()
            assert "bars" in hist
            assert "signal_events" in hist
            assert len(hist["bars"]) > 30, (
                f"Expected dozens of weekly bars; got {len(hist['bars'])}"
            )

            # ---- Step 7: validate response shape ----
            # Last bar should be post-warmup; all indicator values non-None.
            # Field names mirror HistoricalBarPayload in
            # trip_ema/transports/http/schemas.py — `bar_timestamp`
            # (not `timestamp`), `core_bg`/`display_bg` (not `regime`).
            sample = hist["bars"][-1]
            for field in (
                "bar_timestamp", "open", "high", "low", "close", "volume",
                "med_ema", "long_ema", "longest_ema", "atr",
                "upper_inner", "upper_mid", "upper_outer",
                "lower_inner", "lower_mid", "lower_outer",
                "core_bg", "display_bg", "is_warmed_up",
            ):
                assert field in sample, f"Missing field '{field}' in history bar"

            assert sample["med_ema"] is not None
            assert sample["long_ema"] is not None
            assert sample["longest_ema"] is not None
            assert sample["atr"] is not None
            assert sample["atr"] > 0

            # The regime fields (core_bg / display_bg) are strings,
            # not enum values. Valid values are documented on the
            # domain Regime enum.
            for bar in hist["bars"]:
                for field in ("core_bg", "display_bg"):
                    val = bar.get(field)
                    if val is not None:
                        assert val in (
                            "BULLISH", "NEUTRAL", "BEARISH",
                        ), f"Invalid {field}: {val}"

            # Bars in chronological order (by bar_timestamp).
            timestamps = [bar["bar_timestamp"] for bar in hist["bars"]]
            assert timestamps == sorted(timestamps), (
                "History bars are not in chronological order"
            )

            # signal_events — each must reference a bar timestamp
            # that exists in the bars list. Field name in the schema
            # is `signal` (not `signal_type`).
            bar_timestamps = set(timestamps)
            for event in hist["signal_events"]:
                assert event.get("signal") in (
                    "BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL",
                    "NO_CHANGE",
                ), f"Unexpected signal: {event.get('signal')}"
                event_ts = event.get("bar_timestamp")
                assert event_ts in bar_timestamps, (
                    f"Event at {event_ts} doesn't match any bar timestamp"
                )

        app.dependency_overrides.clear()
