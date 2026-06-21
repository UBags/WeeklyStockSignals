"""Tests for GET /v1/signals/{exchange}/{symbol}.

# Step 9 note

The OLD cache held up to 200 BarStates per ticker; the verbose
endpoint returned that history. The new cache holds a single
CachedSnapshot per ticker (latest indicator_state + latest
signal_event). The verbose endpoint returns a single-entry `bars`
tuple reconstructed from the latest indicator_state; consumers that
want full history go through `/v1/signals/{exchange}/{symbol}/history`
(Step 7's chart-history endpoint, integration-tested separately).

So this file keeps the terse-response tests and the error-path tests
(the route's identity/timeframe/instrument handling is unchanged) and
drops the verbose-history tests — that contract is intentionally
narrower under Step 8. The verbose route still works; it just returns
the latest single bar's BarState rather than a window.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from trip_ema.cache.signal_cache import (
    CachedSnapshot,
    IndicatorStateSnapshot,
    SignalEventSnapshot,
)
from trip_ema.domain import ChangeSignal, TickerRef, Timeframe


def _seed_cache(
    cache,
    *,
    ref: TickerRef = TickerRef("NSE", "NIFTY", "INDEX"),
    timeframe: Timeframe = Timeframe.WEEKLY,
    last_change: ChangeSignal = ChangeSignal.BUY,
    close: float = 24856.20,
) -> None:
    t = datetime(2026, 5, 18, tzinfo=timezone.utc)
    ind = IndicatorStateSnapshot(
        trade_date=date(2026, 5, 18),
        med_ema=24800.0, long_ema=24700.0, longest_ema=24500.0,
        atr=120.0,
        upper_inner_band=24824.0, lower_inner_band=24476.0,
        upper_mid_band=25040.0,   lower_mid_band=24260.0,
        upper_outer_band=25280.0, lower_outer_band=24020.0,
        display_regime=1,
        computed_at=t,
    )
    ev = SignalEventSnapshot(
        event_id=1,
        bar_timestamp=t,
        signal=last_change,
        price_at_signal=close,
        computed_at=t,
    )
    cache.put_snapshot(CachedSnapshot(
        ticker_id=1,
        ticker_ref=ref,
        timeframe=timeframe,
        latest_indicator_state=ind,
        latest_signal_event=ev,
        snapshot_taken_at=t,
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


# ---------- Verbose responses — narrowed contract ---------- #

def test_verbose_response_returns_single_bar_from_latest_snapshot(http_app):
    """Under Step 8's narrowed contract, verbose returns the BarState
    reconstructed from the cache's single latest indicator_state row,
    not a window of historical bars. Callers that need history use
    the /history endpoint instead."""
    _seed_cache(http_app.cache)
    response = http_app.client.get(
        "/v1/signals/NSE/NIFTY?instrument=INDEX&verbose=true",
    )
    assert response.status_code == 200
    body = response.json()
    assert "bars" in body
    assert len(body["bars"]) == 1
    bar = body["bars"][0]
    assert bar["display_bg"] == "BULLISH"
    # Unified regime: core_bg mirrors display_bg at the wire boundary.
    assert bar["core_bg"] == "BULLISH"
    assert "med_ema" in bar
    assert "atr" in bar


# ---------- Error paths ---------- #

def test_unknown_ticker_returns_404(http_app):
    response = http_app.client.get("/v1/signals/NSE/UNKNOWN")
    assert response.status_code == 404


def test_known_ticker_but_unknown_timeframe_returns_404(http_app):
    _seed_cache(http_app.cache, timeframe=Timeframe.WEEKLY)
    response = http_app.client.get(
        "/v1/signals/NSE/NIFTY?instrument=INDEX&timeframe=1D",
    )
    assert response.status_code == 404


def test_default_instrument_is_eq(http_app):
    """Calling without instrument= should default to EQ."""
    _seed_cache(http_app.cache, ref=TickerRef("NSE", "RELIANCE", "EQ"))
    response = http_app.client.get("/v1/signals/NSE/RELIANCE")
    assert response.status_code == 200
    assert response.json()["ticker"]["instrument"] == "EQ"