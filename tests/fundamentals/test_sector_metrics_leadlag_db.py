"""DB-backed parity test for the §4.2 regime-based sector lead/lag
(leadlag C'b-2). Seeds a deterministic square-wave breadth and a
non-constituent stock that LEADS it by 2 weeks; asserts the runner's
_leadlag_rows classifies it LEADER at best_lag 2, and that an all-identical
constituent debiases (1/N) to COUPLED at lag 0.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import math

from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.domain.market_session import market_session_for
from trip_ema.fundamentals.sector_metrics_runner import _leadlag_rows
from trip_ema.persistence import transaction
from trip_ema.persistence.tickers_repo import TickersRepository

UTC = timezone.utc
IND = "TestLeadLag Industry"
FIRST_MONDAY = date(2024, 1, 1)   # a Monday
N_WEEKS = 100
PERIOD = 13


def _sq(i: int) -> int:
    return 1 if math.sin(2 * math.pi * i / PERIOD) >= 0 else -1


def _mondays(n: int) -> list[date]:
    return [FIRST_MONDAY + timedelta(weeks=i) for i in range(n)]


def _seed_state(conn, ticker_id: int, seq: list[tuple[date, int]]) -> None:
    for monday, regime in seq:
        ts = datetime.combine(monday, time(0, 0), tzinfo=UTC)
        conn.execute(
            text("""
                INSERT INTO stock_indicator_state (
                    ticker_id, timeframe, date, bar_timestamp,
                    med_ema, long_ema, longest_ema, atr,
                    upper_inner_band, lower_inner_band,
                    upper_mid_band, lower_mid_band,
                    upper_outer_band, lower_outer_band,
                    display_regime, computed_at
                ) VALUES (
                    :tid, '1W', (:ts AT TIME ZONE 'Asia/Kolkata')::date, :ts,
                    100.0, 99.0, 98.0, 2.0,
                    102.0, 96.0, 104.0, 94.0, 106.0, 92.0,
                    :reg, NOW()
                )
                ON CONFLICT (ticker_id, timeframe, date)
                DO UPDATE SET display_regime = EXCLUDED.display_regime
            """),
            {"tid": ticker_id, "ts": ts, "reg": regime},
        )


def test_regime_leadlag_detects_non_constituent_leader(db_engine):
    session = market_session_for("NSE")
    mondays = _mondays(N_WEEKS)
    now = datetime.now(UTC).replace(microsecond=0)

    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        c_ids = [repo.insert(TickerRef("NSE", s, "EQ"), created_at=now)
                 for s in ("LLC1", "LLC2", "LLC3")]
        l_id = repo.insert(TickerRef("NSE", "LLLEAD", "EQ"), created_at=now)

        conn.execute(text("""
            INSERT INTO industry_index (
                industry_name, sector_name, methodology,
                base_value, base_date, rebalance_schedule,
                min_constituents, status
            ) VALUES (
                :ind, 'Technology', 'MODIFIED_LASPEYRES',
                1000.0, :bd, 'SEMIANNUAL_APR_OCT', 1, 'ACTIVE'
            ) ON CONFLICT (industry_name) DO NOTHING
        """), {"ind": IND, "bd": FIRST_MONDAY})

        eff_from = FIRST_MONDAY - timedelta(days=7)
        for tid in c_ids:
            conn.execute(text("""
                INSERT INTO industry_index_constituent (
                    industry_name, ticker_id, rebalance_date,
                    effective_from, effective_to, weight_at_rebalance,
                    market_cap_at_rebalance, close_at_rebalance
                ) VALUES (
                    :ind, :tid, :ef, :ef, NULL, 0.33, 1000000000.0, 100.0
                )
            """), {"ind": IND, "tid": tid, "ef": eff_from})

        # L is a classified member but NOT a constituent.
        conn.execute(text("""
            INSERT INTO instrument_industry (
                ticker_id, sector, industry, classification_source
            ) VALUES (:tid, 'Technology', :ind, 'YFINANCE')
        """), {"tid": l_id, "ind": IND})

        # Constituents all carry the SAME square wave -> breadth == square wave.
        for tid in c_ids:
            _seed_state(conn, tid, [(mondays[i], _sq(i))
                                    for i in range(N_WEEKS)])
        # L leads the breadth by 2 weeks: L[i] == breadth[i+2].
        _seed_state(conn, l_id, [(mondays[i], _sq(i + 2))
                                 for i in range(N_WEEKS - 2)])

    ranked = set(c_ids) | {l_id}
    with transaction(db_engine) as conn:
        rows = _leadlag_rows(conn, IND, session, ranked)

    by = {(r.ticker_id, r.window): r for r in rows}

    lead = by[(l_id, 52)]
    assert lead.is_constituent is False
    assert lead.best_lag == 2
    assert lead.coupling_class == "LEADER"

    # All-identical constituent: 1/N debias leaves the same square wave, so it
    # is COUPLED at lag 0. A wrong debias weight would shift this.
    cons = by[(c_ids[0], 52)]
    assert cons.is_constituent is True
    assert cons.best_lag == 0
    assert cons.coupling_class == "COUPLED"
