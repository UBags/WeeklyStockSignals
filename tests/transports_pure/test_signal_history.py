"""Tests for GET /v1/signals/{exchange}/{symbol}/history.

DB-free: we mock the SQLAlchemy Engine's connect()/execute() so the
endpoint sees handcrafted bar rows and ticker metadata.

The endpoint replays the engine. We don't mock the engine — we feed
real OHLCV bars, run the real `daily_to_weekly + TickerEngine.prime`,
and assert on the response shape. Pine parity stays in the same code
path as production.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from trip_ema.config.schema import (
    ATRConfig, CacheConfig, Config, DatabaseConfig, EMAConfig,
    EMAStageConfig, HttpConfig, MarketHoursConfig, SchedulingConfig,
)
from trip_ema.transports.http import create_app
from trip_ema.transports.http.dependencies import (
    get_cache, get_compute_scheduler, get_config, get_db_engine,
    get_distributor, get_etl_executor, get_redis_queue, get_registry,
)


# ---------- fixtures ---------- #


def _make_real_config() -> Config:
    """A minimum-viable Config the route can call to_indicator_params on."""
    return Config(
        database=DatabaseConfig(
            host="x", database="x", user="x", password_env="IGNORED",
        ),
        ema=EMAConfig(
            med=EMAStageConfig(base=8, s1=4, s2=4, final=None),
            long=EMAStageConfig(base=15, s1=4, s2=4, final=3),
            longest=EMAStageConfig(base=28, s1=7, s2=5, final=3),
        ),
        atr=ATRConfig(),
        market_hours=[MarketHoursConfig(
            exchange="NSE", open="09:15", close="15:30",
            tz="Asia/Kolkata", weekdays=[1, 2, 3, 4, 5],
        )],
        scheduling=SchedulingConfig(),
        cache=CacheConfig(),
        http=HttpConfig(admin_token_env="TRIP_EMA_ADMIN_TOKEN"),
    )


class FakeRow:
    """Mimic a SQLAlchemy Row — attribute access for column values."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _build_db_engine_with_bars(*, bars, ticker_exists=True, events=None):
    """Wire a MagicMock to return:
      - ticker metadata via the first .execute() call
      - bar rows via the second .execute() call
      - signal_events rows via the third .execute() call
    """
    engine = MagicMock()
    conn = MagicMock()
    engine.connect.return_value.__enter__.return_value = conn

    if ticker_exists:
        ticker_row = FakeRow(ticker_id=1, tz="Asia/Kolkata", source_timeframe="1D")
    else:
        ticker_row = None

    # The route calls .execute() three times: ticker lookup, bars, events.
    # We use a side_effect list of three return values; each MagicMock
    # must support both .first() (for the ticker lookup) and iteration
    # (for bars + events).
    ticker_result = MagicMock()
    ticker_result.first.return_value = ticker_row

    bars_result = MagicMock()
    bars_result.__iter__.return_value = iter(bars)

    events_result = MagicMock()
    events_result.__iter__.return_value = iter(events or [])

    conn.execute.side_effect = [ticker_result, bars_result, events_result]
    return engine


def _make_client(*, config=None, db_engine=None):
    app = create_app()
    app.dependency_overrides[get_config] = lambda: (config or _make_real_config())
    app.dependency_overrides[get_db_engine] = lambda: (db_engine or MagicMock())
    app.dependency_overrides[get_registry] = lambda: MagicMock()
    app.dependency_overrides[get_cache] = lambda: MagicMock()
    app.dependency_overrides[get_compute_scheduler] = lambda: MagicMock()
    app.dependency_overrides[get_distributor] = lambda: None
    app.dependency_overrides[get_redis_queue] = lambda: None
    app.dependency_overrides[get_etl_executor] = lambda: None
    return TestClient(app)


def _daily_bar_row(d_offset_days: int, *, close: float):
    """Build a fake row representing one daily OHLCV bar."""
    ts = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=d_offset_days)
    return FakeRow(
        bar_timestamp=ts,
        open=close - 1.0, high=close + 2.0, low=close - 2.0,
        close=close, volume=1000.0,
    )


# ---------- tests ---------- #


class TestUnknownTicker:

    def test_returns_404_when_ticker_not_in_db(self):
        engine = _build_db_engine_with_bars(bars=[], ticker_exists=False)
        client = _make_client(db_engine=engine)
        resp = client.get("/v1/signals/NSE/NONEXISTENT/history")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()


class TestEmptyHistory:

    def test_returns_empty_payload_when_no_bars(self):
        """Ticker exists but has no bars in the window."""
        engine = _build_db_engine_with_bars(bars=[])
        client = _make_client(db_engine=engine)
        resp = client.get("/v1/signals/NSE/RELIANCE/history?years=3")
        assert resp.status_code == 200
        body = resp.json()
        assert body["bars"] == []
        assert body["signal_events"] == []
        assert body["warmup_bars_count"] == 0
        assert "No bars" in body["notes"]


class TestEngineReplay:

    def test_replays_engine_and_returns_bar_states(self):
        """Feed enough daily bars to produce post-warmup weekly states,
        and verify the response carries indicator values."""
        # ~3 years of daily bars at $100 with mild drift
        bars = [
            _daily_bar_row(i, close=100.0 + i * 0.1)
            for i in range(750)
        ]
        engine = _build_db_engine_with_bars(bars=bars)
        client = _make_client(db_engine=engine)

        resp = client.get(
            "/v1/signals/NSE/RELIANCE/history?years=1",
            # 1 year = user window; the route pads internally for warmup
        )
        assert resp.status_code == 200
        body = resp.json()
        # We should get weekly bars (~52 for 1 year)
        assert len(body["bars"]) > 0
        # The latest bar should have non-None indicator values (warmed up)
        last = body["bars"][-1]
        assert last["med_ema"] is not None
        assert last["long_ema"] is not None
        assert last["longest_ema"] is not None
        assert last["atr"] is not None
        # OHLCV always present
        assert last["close"] > 0
        # core_bg / display_bg are strings, not enum objects
        assert last["core_bg"] in ("BULLISH", "NEUTRAL", "BEARISH")
        assert last["is_warmed_up"] is True

    def test_warmup_bars_have_none_indicators(self):
        """When we pass only enough bars to span warmup partially, the
        leading bars in the returned window have None indicator values."""
        # 100 daily bars ≈ 20 weeks — not enough for full warmup
        bars = [
            _daily_bar_row(i, close=100.0)
            for i in range(100)
        ]
        engine = _build_db_engine_with_bars(bars=bars)
        client = _make_client(db_engine=engine)
        # Use years=10 so the user window includes the early bars
        resp = client.get("/v1/signals/NSE/RELIANCE/history?years=10")
        assert resp.status_code == 200
        body = resp.json()
        # Some leading bars have None for at least one indicator
        med_none_count = sum(1 for b in body["bars"] if b["med_ema"] is None)
        assert med_none_count > 0
        # warmup_bars_count = bars where is_warmed_up=False (any indicator NaN).
        # The longest EMA chain warms up last, so this is >= the med-only count.
        assert body["warmup_bars_count"] >= med_none_count


class TestSignalEventsInResponse:

    def test_includes_events_within_window(self):
        """Verify the route plumbs signal_events through."""
        bars = [_daily_bar_row(i, close=100.0 + i * 0.1) for i in range(400)]
        # Two signal events
        events = [
            FakeRow(
                event_id=1,
                bar_timestamp=datetime(2025, 6, 1, tzinfo=timezone.utc),
                signal="BUY",
                price_at_signal=120.0,
            ),
            FakeRow(
                event_id=2,
                bar_timestamp=datetime(2025, 9, 1, tzinfo=timezone.utc),
                signal="SELL",
                price_at_signal=125.0,
            ),
        ]
        engine = _build_db_engine_with_bars(bars=bars, events=events)
        client = _make_client(db_engine=engine)
        resp = client.get("/v1/signals/NSE/RELIANCE/history?years=3")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["signal_events"]) == 2
        signals = sorted(e["signal"] for e in body["signal_events"])
        assert signals == ["BUY", "SELL"]
        # event_id flows through
        assert any(e["event_id"] == 1 for e in body["signal_events"])


class TestQueryParamValidation:

    def test_invalid_years_value_rejected(self):
        client = _make_client()
        # ge=1 le=20
        resp = client.get("/v1/signals/NSE/RELIANCE/history?years=0")
        assert resp.status_code == 422

    def test_too_many_years_rejected(self):
        client = _make_client()
        resp = client.get("/v1/signals/NSE/RELIANCE/history?years=100")
        assert resp.status_code == 422
