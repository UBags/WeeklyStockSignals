"""End-to-end DB integration for the forward-walk runner (spec 02).

Seeds a real (migrated) database with tickers, daily bars, signal_enrichment
rows and the NIFTYBEES benchmark, runs `backfill_forward_walk`, and asserts the
signal_labels rows land with the right measured facts. This is the seam the pure
tests can't cover: the SQL reads, the upsert, and the fan_out worker wiring all
run for real. The numeric expectations are pinned by the pure-core tests in
tests/fundamentals/test_forward_walk_runner.py / tests/domain/test_forward_walk.py.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import text

from trip_ema.domain import OHLCVBar, TickerRef, Timeframe
from trip_ema.fundamentals.forward_walk_runner import backfill_forward_walk
from trip_ema.persistence import transaction
from trip_ema.persistence.bars_repo import BarsRepository
from trip_ema.persistence.tickers_repo import TickersRepository


def D(m: int, d: int) -> date:
    return date(2024, m, d)


def _ticker(conn, symbol: str, *, exchange: str = "NSE") -> int:
    return TickersRepository(conn).insert(
        TickerRef(exchange, symbol, "EQ"), created_at=datetime.now(UTC),
    )


def _bar(conn, tid: int, d: date, o, h, low, c, *, vol: float = 1000.0) -> None:
    # 00:00 UTC -> 05:30 IST, so the local trading date is `d`.
    ts = datetime(d.year, d.month, d.day, tzinfo=UTC)
    BarsRepository(conn).insert_bar(
        tid, Timeframe.DAILY, OHLCVBar(ts, o, h, low, c, vol),
    )


def _enrichment(conn, se_id: int, tid: int, sdate: date, stype: str,
                *, atr20: float | None = 2.0) -> None:
    conn.execute(text(
        "INSERT INTO signal_enrichment (signal_enrichment_id, ticker_id, "
        "timeframe, signal_date, signal_type, features, feature_spec_version, "
        "atr20, day_of_week, data_is_valid) VALUES "
        "(:id, :t, '1W', :d, :st, '{}', 'v0.5', :atr, 1, TRUE)"
    ), {"id": se_id, "t": tid, "d": sdate, "st": stype, "atr": atr20})


_TESTCO_BARS = [
    (D(1, 1), 100, 101, 99, 100),
    (D(1, 2), 100, 106, 100, 105),   # entry bar
    (D(1, 3), 105, 107, 104, 106),   # exit bar (N)
    (D(1, 4), 106, 108, 103, 104),
    (D(1, 5), 104, 105, 100, 101),
]
# NIFTYBEES flat OHLC = close; +3% over [01-02, 01-03]
_NIFTY_BARS = [(D(1, 1), 198), (D(1, 2), 200), (D(1, 3), 206),
               (D(1, 4), 205), (D(1, 5), 204)]


def _seed_nifty(conn) -> None:
    nifty = _ticker(conn, "NIFTYBEES")
    for d, c in _NIFTY_BARS:
        _bar(conn, nifty, d, c, c, c, c)


def test_forward_walk_end_to_end(db_engine):
    with transaction(db_engine) as conn:
        tid = _ticker(conn, "TESTCO")
        for d, o, h, low, c in _TESTCO_BARS:
            _bar(conn, tid, d, o, h, low, c)
        _seed_nifty(conn)
        _enrichment(conn, 9001, tid, D(1, 1), "BUY")
        _enrichment(conn, 9002, tid, D(1, 3), "EXIT_FROM_BUY")  # terminator only

    outcome = backfill_forward_walk(db_engine, "NSE", scoped_tickers=[tid])
    assert outcome.done == 1 and outcome.failed == []

    with db_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT signal_enrichment_id, forward_walk_done, side, entry_date, "
            "exit_date, exit_reason, window_censored, bars_in_window, "
            "realized_ret, excess_ret_market, excess_ret_industry, "
            "hit_gain_5_pct, bars_to_gain_5_pct, hit_gain_7_pct, "
            "bars_to_gain_7_pct, mfe_pct, mfe_bar "
            "FROM signal_labels WHERE forward_walk_done "
            "ORDER BY signal_enrichment_id"
        )).mappings().all()

    assert len(rows) == 1               # only the BUY; EXIT is terminator-only
    r = rows[0]
    assert r["signal_enrichment_id"] == 9001
    assert r["forward_walk_done"] is True
    assert r["side"] == "LONG"
    assert r["entry_date"] == D(1, 2) and r["exit_date"] == D(1, 3)
    assert r["exit_reason"] == "NEXT_SIGNAL"
    assert r["window_censored"] is False
    assert r["bars_in_window"] == 2
    assert abs(r["realized_ret"] - 0.06) < 1e-9
    assert abs(r["excess_ret_market"] - 0.03) < 1e-9
    assert abs(r["excess_ret_industry"] - 0.03) < 1e-9    # fallback -> market
    assert r["hit_gain_5_pct"] is True and r["bars_to_gain_5_pct"] == 0
    assert r["hit_gain_7_pct"] is True and r["bars_to_gain_7_pct"] == 1
    assert abs(r["mfe_pct"] - 0.07) < 1e-9 and r["mfe_bar"] == 1


def test_forward_walk_is_idempotent(db_engine):
    with transaction(db_engine) as conn:
        tid = _ticker(conn, "IDEMPCO")
        for d, o, h, low, c in _TESTCO_BARS:
            _bar(conn, tid, d, o, h, low, c)
        _enrichment(conn, 9101, tid, D(1, 1), "BUY")
        _enrichment(conn, 9102, tid, D(1, 3), "SELL")

    backfill_forward_walk(db_engine, "NSE", scoped_tickers=[tid])
    backfill_forward_walk(db_engine, "NSE", scoped_tickers=[tid])  # re-run

    with db_engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM signal_labels WHERE forward_walk_done"
        )).scalar_one()
    assert n == 2                        # BUY + SELL, one row each, no dupes


def test_last_signal_window_is_data_end_censored(db_engine):
    with transaction(db_engine) as conn:
        tid = _ticker(conn, "OPENPOS")
        for d, o, h, low, c in _TESTCO_BARS[:3]:     # 01-01..03
            _bar(conn, tid, d, o, h, low, c)
        _enrichment(conn, 9201, tid, D(1, 1), "BUY")  # only signal -> open

    backfill_forward_walk(db_engine, "NSE", scoped_tickers=[tid])

    with db_engine.connect() as conn:
        r = conn.execute(text(
            "SELECT exit_reason, window_censored, exit_date, "
            "forward_walk_done, realized_ret "
            "FROM signal_labels WHERE signal_enrichment_id = 9201"
        )).mappings().one()
    assert r["forward_walk_done"] is True
    assert r["exit_reason"] == "DATA_END"
    assert r["window_censored"] is True      # right-censored
    assert r["exit_date"] == D(1, 3)         # ran to the last available bar
    assert r["realized_ret"] is not None     # ...but still a real walk


def test_exit_only_ticker_produces_no_walk_rows(db_engine):
    with transaction(db_engine) as conn:
        tid = _ticker(conn, "EXITONLY")
        for d, o, h, low, c in _TESTCO_BARS[:2]:
            _bar(conn, tid, d, o, h, low, c)
        _enrichment(conn, 9301, tid, D(1, 1), "EXIT_FROM_BUY")

    outcome = backfill_forward_walk(db_engine, "NSE", scoped_tickers=[tid])
    assert outcome.failed == []

    with db_engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM signal_labels")
        ).scalar_one()
    assert n == 0                            # EXIT_FROM_* never gets a walk row
