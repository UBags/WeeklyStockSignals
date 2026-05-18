"""Tests for GET /v1/signals/{exchange}/{symbol}."""

from __future__ import annotations

from datetime import datetime, timezone

from trip_ema.cache import build_cached_signal
from trip_ema.domain import (
    BarState,
    ChangeSignal,
    CoreRegime,
    TickerRef,
    Timeframe,
)


def _seed_cache(
    cache,
    *,
    ref: TickerRef = TickerRef("NSE", "NIFTY", "INDEX"),
    timeframe: Timeframe = Timeframe.WEEKLY,
    last_change: ChangeSignal = ChangeSignal.BUY,
    close: float = 24856.20,
    n_history: int = 5,
) -> None:
    t = datetime(2026, 5, 18, tzinfo=timezone.utc)
    state = BarState(
        timestamp=t,
        med_ema=24800, long_ema=24700, longest_ema=24500,
        atr=120.0,
        upper_inner=24824, lower_inner=24476,
        upper_mid=25040, lower_mid=24260,
        upper_outer=25280, lower_outer=24020,
        pierces_upper_mid=False, pierces_lower_mid=False,
        core_bg=CoreRegime.BULLISH, display_bg=CoreRegime.BULLISH,
    )
    history = [state] * n_history
    cache.put(build_cached_signal(
        ticker=ref, timeframe=timeframe,
        latest_state=state, latest_close=close,
        state_history=history, last_change=last_change,
        last_change_bar=t, last_change_computed_at=t,
        computed_at=t,
    ))


# ---------- Terse responses (default) ---------- #

def test_terse_response_returns_latest_change_and_price(http_app):
    _seed_cache(http_app.cache, last_change=ChangeSignal.BUY, close=24856.20)
    response = http_app.client.get(
        "/v1/signals/NSE/NIFTY?instrument=INDEX",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == {
        "exchange": "NSE", "symbol": "NIFTY", "instrument": "INDEX",
    }
    assert body["latest_change"] == "BUY"
    assert body["latest_price"] == 24856.20
    assert body["timeframe"] == "1W"
    # No bars field on terse responses
    assert "bars" not in body


def test_terse_response_exits(http_app):
    _seed_cache(http_app.cache, last_change=ChangeSignal.EXIT_FROM_BUY)
    response = http_app.client.get("/v1/signals/NSE/NIFTY?instrument=INDEX")
    assert response.status_code == 200
    assert response.json()["latest_change"] == "EXIT_FROM_BUY"


# ---------- Verbose responses ---------- #

def test_verbose_response_returns_full_history(http_app):
    _seed_cache(http_app.cache, n_history=10)
    response = http_app.client.get(
        "/v1/signals/NSE/NIFTY?instrument=INDEX&verbose=true",
    )
    assert response.status_code == 200
    body = response.json()
    assert "bars" in body
    assert len(body["bars"]) == 10
    first_bar = body["bars"][0]
    # Field shape sanity-check
    assert first_bar["display_bg"] == "BULLISH"
    assert first_bar["core_bg"] == "BULLISH"
    assert "med_ema" in first_bar
    assert "atr" in first_bar


def test_verbose_response_honors_last_n_bars(http_app):
    _seed_cache(http_app.cache, n_history=50)
    response = http_app.client.get(
        "/v1/signals/NSE/NIFTY"
        "?instrument=INDEX&verbose=true&last_n_bars=5",
    )
    assert response.status_code == 200
    assert len(response.json()["bars"]) == 5


def test_verbose_nan_values_serialized_as_null(http_app):
    """NaN floats in BarState should appear as null in JSON, not as
    'NaN' (which isn't valid JSON)."""
    import json

    cache = http_app.cache
    t = datetime(2026, 5, 18, tzinfo=timezone.utc)
    nan = float("nan")
    state = BarState(
        timestamp=t,
        med_ema=nan, long_ema=nan, longest_ema=nan, atr=nan,
        upper_inner=nan, lower_inner=nan,
        upper_mid=nan, lower_mid=nan,
        upper_outer=nan, lower_outer=nan,
        pierces_upper_mid=False, pierces_lower_mid=False,
        core_bg=CoreRegime.NEUTRAL, display_bg=CoreRegime.NEUTRAL,
    )
    ref = TickerRef("NSE", "X", "EQ")
    cache.put(build_cached_signal(
        ticker=ref, timeframe=Timeframe.WEEKLY,
        latest_state=state, latest_close=100.0,
        state_history=[state], last_change=ChangeSignal.NO_CHANGE,
        last_change_bar=None, last_change_computed_at=None,
        computed_at=t,
    ))
    response = http_app.client.get(
        "/v1/signals/NSE/X?instrument=EQ&verbose=true",
    )
    assert response.status_code == 200
    raw = response.content.decode()
    # Must be valid JSON — no 'NaN' tokens
    assert "NaN" not in raw
    body = json.loads(raw)
    assert body["bars"][0]["med_ema"] is None
    assert body["bars"][0]["atr"] is None


# ---------- Error paths ---------- #

def test_unknown_ticker_returns_404(http_app):
    response = http_app.client.get("/v1/signals/NSE/UNKNOWN")
    assert response.status_code == 404
    assert "No cached state" in response.json()["detail"]


def test_known_ticker_but_unknown_timeframe_returns_404(http_app):
    _seed_cache(http_app.cache, timeframe=Timeframe.WEEKLY)
    response = http_app.client.get(
        "/v1/signals/NSE/NIFTY?instrument=INDEX&timeframe=1D",
    )
    assert response.status_code == 404


def test_invalid_last_n_bars_returns_422(http_app):
    """last_n_bars has ge=1 le=1000 constraints."""
    _seed_cache(http_app.cache)
    response = http_app.client.get(
        "/v1/signals/NSE/NIFTY?instrument=INDEX&last_n_bars=0",
    )
    assert response.status_code == 422


def test_default_instrument_is_eq(http_app):
    """Calling without instrument= should default to EQ."""
    _seed_cache(http_app.cache, ref=TickerRef("NSE", "RELIANCE", "EQ"))
    response = http_app.client.get("/v1/signals/NSE/RELIANCE")
    assert response.status_code == 200
    assert response.json()["ticker"]["instrument"] == "EQ"
