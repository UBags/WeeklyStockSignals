"""DB integration test for the signal-enrichment backfill runner.

Regression for the two failures found during the first NSE backfill:
  (1) signal_date must survive the read into the fold — a curated
      get_for_ticker dropped it, so every signal was skipped (0 rows);
  (2) NaN features must not blow up the JSONB write (they land as null).
Seeds one ticker with 4 weekly state rows, daily bars, and two signals;
runs the runner and asserts both signals are enriched.
"""
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import text

from trip_ema.fundamentals.signal_enrichment_runner import (
    backfill_signal_enrichment,
)
from trip_ema.persistence import transaction

UTC = timezone.utc


def _seed_ticker(engine, tid, symbol):
    with transaction(engine) as conn:
        conn.execute(text(
            "INSERT INTO tickers (ticker_id, exchange, symbol, tier, "
            "is_active, delisted_at) VALUES (:t,'NSE',:s,2,TRUE,NULL) "
            "ON CONFLICT (ticker_id) DO NOTHING"), {"t": tid, "s": symbol})


def _seed_state(engine, tid, mondays):
    with transaction(engine) as conn:
        for m in mondays:
            ts = datetime.combine(m, time(0, 0), tzinfo=UTC)
            conn.execute(text(
                "INSERT INTO stock_indicator_state (ticker_id, timeframe, "
                "date, bar_timestamp, med_ema, long_ema, longest_ema, atr, "
                "upper_inner_band, lower_inner_band, upper_mid_band, "
                "lower_mid_band, upper_outer_band, lower_outer_band, "
                "display_regime, computed_at) VALUES (:t,'1W',"
                "(:ts AT TIME ZONE 'Asia/Kolkata')::date,:ts,"
                "100,99,98,2, 102,96,104,94,106,92, 1, NOW()) "
                "ON CONFLICT (ticker_id, timeframe, date) DO NOTHING"),
                {"t": tid, "ts": ts})


def _seed_daily(engine, tid, days):
    with transaction(engine) as conn:
        for d in days:
            ts = datetime.combine(d, time(0, 0), tzinfo=UTC)
            conn.execute(text(
                "INSERT INTO ohlcv_bars (ticker_id, timeframe, "
                "bar_timestamp, open, high, low, close, volume) VALUES "
                "(:t,'1D',:ts, 100,101,99,100, 1000) "
                "ON CONFLICT (ticker_id, timeframe, bar_timestamp) "
                "DO NOTHING"), {"t": tid, "ts": ts})


def _seed_signal(engine, tid, sd, stype):
    ts = datetime.combine(sd, time(0, 0), tzinfo=UTC)
    with transaction(engine) as conn:
        conn.execute(text(
            "INSERT INTO historical_signals (ticker_id, timeframe, "
            "bar_timestamp, signal_type, regime_before, regime_after, "
            "price_at_signal, computed_at, archived_at, signal_date) "
            "VALUES (:t,'1W',:ts,:st,-1,1,100.0,NOW(),NOW(),:sd) "
            "ON CONFLICT DO NOTHING"),
            {"t": tid, "ts": ts, "st": stype, "sd": sd})


def test_runner_writes_rows_with_signal_date_and_jsonb(db_engine):
    tid = 990001
    mondays = [date(2024, 1, 1), date(2024, 1, 8),
               date(2024, 1, 15), date(2024, 1, 22)]
    days = sorted({m + timedelta(days=off)
                   for m in mondays for off in range(5)})
    _seed_ticker(db_engine, tid, "ENRICHTEST")
    _seed_state(db_engine, tid, mondays)
    _seed_daily(db_engine, tid, days)
    _seed_signal(db_engine, tid, date(2024, 1, 8), "BUY")
    _seed_signal(db_engine, tid, date(2024, 1, 15), "SELL")

    outcome = backfill_signal_enrichment(db_engine, "NSE",
                                         scoped_tickers=[tid])
    assert not outcome.failed

    with db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT signal_date, signal_type, "
            "jsonb_typeof(features) AS ftype "
            "FROM signal_enrichment WHERE ticker_id=:t "
            "ORDER BY signal_date"), {"t": tid}).all()
    # signal_date survived the read -> both signals enriched
    assert {r.signal_date for r in rows} == {date(2024, 1, 8),
                                             date(2024, 1, 15)}
    assert {r.signal_type for r in rows} == {"BUY", "SELL"}
    assert rows and all(r.ftype == "object" for r in rows)
