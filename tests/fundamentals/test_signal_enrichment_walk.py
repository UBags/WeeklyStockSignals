"""Wiring test for the per-ticker signal-enrichment walk (spec §19)."""
from datetime import datetime, timedelta, timezone

from trip_ema.domain import OHLCVBar
from trip_ema.fundamentals.market_session import market_session_for
from trip_ema.fundamentals.signal_enrichment_walk import (
    compute_ticker_signal_enrichments,
)

UTC = timezone.utc


def _week(monday, base):
    return [OHLCVBar(monday + timedelta(days=i), base + i, base + 2 + i,
                     base - 1 + i, base + 1 + i, 100 + i) for i in range(5)]


def _scenario():
    weeks = [datetime(2024, 1, 1, tzinfo=UTC) + timedelta(weeks=w)
             for w in range(4)]
    session = market_session_for("NSE")
    daily, state_by_date, conv_by_monday = [], {}, {}
    for w, m in enumerate(weeks):
        bars = _week(m, 10 + 5 * w)
        daily += bars
        for b in bars:
            d = b.timestamp.astimezone(session.zone).date()
            state_by_date[d] = (b.close - 0.5, b.close - 1.0,
                                b.close - 1.5, 1.2)
        last = bars[-1]
        conv_by_monday[m] = (last.close - 0.5, last.close - 1.0,
                             last.close - 1.5, 1.2)
    return daily, session, state_by_date, conv_by_monday, weeks


def test_produces_rows_at_signal_days():
    daily, session, sbd, cbm, weeks = _scenario()
    sig_day = (weeks[2] + timedelta(days=2)).astimezone(session.zone).date()
    rows = compute_ticker_signal_enrichments(
        ticker_id=42, timeframe="1W", daily_bars=daily,
        state_by_date=sbd, conv_by_monday=cbm,
        signals_by_date={sig_day: ["BUY"]},
        session=session, now=datetime(2024, 6, 1, tzinfo=UTC),
        feature_spec_version="v0.3", industry_id=7,
    )
    assert len(rows) == 1
    r = rows[0]
    assert r.ticker_id == 42
    assert r.signal_type == "BUY"
    assert r.signal_date == sig_day
    assert r.industry_id == 7
    assert r.feature_spec_version == "v0.3"
    assert isinstance(r.features, dict)
    assert {"med_ema_structure", "med_ema_leg_amplitude",
            "atr28"}.issubset(r.features)
    assert "med_ema_structure" in r.features
    assert "div_srsi14" in r.features
    assert "atr28" in r.features


def test_multiple_types_same_day_and_no_signals():
    daily, session, sbd, cbm, weeks = _scenario()
    sig_day = (weeks[2] + timedelta(days=2)).astimezone(session.zone).date()
    rows = compute_ticker_signal_enrichments(
        ticker_id=1, timeframe="1W", daily_bars=daily,
        state_by_date=sbd, conv_by_monday=cbm,
        signals_by_date={sig_day: ["BUY", "EXIT_FROM_SELL"]},
        session=session, now=datetime(2024, 6, 1, tzinfo=UTC),
        feature_spec_version="v0.3",
    )
    assert len(rows) == 2
    assert {r.signal_type for r in rows} == {"BUY", "EXIT_FROM_SELL"}
    assert rows[0].features == rows[1].features

    empty = compute_ticker_signal_enrichments(
        ticker_id=1, timeframe="1W", daily_bars=daily,
        state_by_date=sbd, conv_by_monday=cbm, signals_by_date={},
        session=session, now=datetime(2024, 6, 1, tzinfo=UTC),
        feature_spec_version="v0.3",
    )
    assert empty == []
