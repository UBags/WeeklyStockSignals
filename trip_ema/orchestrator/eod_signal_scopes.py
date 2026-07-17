"""EOD per-signal stage scopes (spec §19-21 wiring).

The anti-joins the EOD cycle uses to scope the shared per-signal sequencer.
They live here, apart from recompute_market, so the SQL is testable on its
own and the cycle body stays readable.

The maturity/enrichment scopes are SELF-CORRECTING: they select what is
MISSING, not what changed this cycle. A prior EOD that half-failed leaves gaps;
the next EOD's anti-join re-selects those gaps. No ledger, no bookkeeping — the
DB state IS the ledger. The forward-walk scope is event-based (today's exits).

Every read is wrapped in `with db_engine.connect()` so the connection is
returned to the pool deterministically — a bare `db_engine.connect().execute()`
leaks the connection until GC, which under xdist surfaces as a pool-reset error
when the test's ephemeral server is torn down first.
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Engine


def tickers_needing_enrichment(
    db_engine: Engine, exchange: str, market_day: date, *,
    timeframe: str = "1W", spec_version: str,
) -> list[int]:
    """Tickers with a historical_signals row for `market_day` that have no
    signal_enrichment row at `spec_version` yet — i.e. today's flips (and any
    stragglers a prior EOD missed).

    Scoped to `exchange` via tickers. The anti-join is on the natural key
    (ticker_id, timeframe, signal_date, signal_type): a signal is "enriched"
    only when its OWN row exists at the current spec, so a spec bump correctly
    re-selects everything.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT hs.ticker_id "
                "FROM historical_signals hs "
                "JOIN tickers t ON t.ticker_id = hs.ticker_id "
                "LEFT JOIN signal_enrichment se "
                "  ON se.ticker_id = hs.ticker_id "
                " AND se.timeframe = hs.timeframe "
                " AND se.signal_date = hs.signal_date "
                " AND se.signal_type = hs.signal_type "
                " AND se.feature_spec_version = :ver "
                "WHERE t.exchange = :ex "
                "  AND hs.timeframe = :tf "
                "  AND hs.signal_date = :day "
                "  AND se.signal_enrichment_id IS NULL"
            ),
            {"ex": exchange, "tf": timeframe, "day": market_day,
             "ver": spec_version},
        ).all()
    return [r[0] for r in rows]


def tickers_needing_labels(
    db_engine: Engine, exchange: str, *,
    timeframe: str = "1W",
) -> list[int]:
    """Tickers whose labels are not yet finalised: any signal_enrichment row
    on `exchange` that has no signal_labels row, OR whose label is not
    horizon_complete. These are the rows a maturity pass finalises as forward
    weeks accrue — a DIFFERENT set from today's flips.

    Distinct ticker_ids, because the label runner scopes per ticker.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT se.ticker_id "
                "FROM signal_enrichment se "
                "JOIN tickers t ON t.ticker_id = se.ticker_id "
                "LEFT JOIN signal_labels sl "
                "  ON sl.signal_enrichment_id = se.signal_enrichment_id "
                "WHERE t.exchange = :ex "
                "  AND se.timeframe = :tf "
                "  AND (sl.signal_label_id IS NULL "
                "       OR sl.horizon_complete = FALSE)"
            ),
            {"ex": exchange, "tf": timeframe},
        ).all()
    return [r[0] for r in rows]


def tickers_with_exit_signal_on(
    db_engine: Engine, exchange: str, market_day: date, *,
    timeframe: str = "1W",
) -> list[int]:
    """Tickers that issued an EXIT_FROM_BUY or EXIT_FROM_SELL signal on
    `market_day`. An exit closes a position, so the forward-walk window of the
    BUY/SELL it terminates is now final — these are exactly the tickers whose
    walk the EOD cycle must (re)compute. Distinct ticker_ids on `exchange`.
    """
    with db_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT hs.ticker_id "
                "FROM historical_signals hs "
                "JOIN tickers t ON t.ticker_id = hs.ticker_id "
                "WHERE t.exchange = :ex "
                "  AND hs.timeframe = :tf "
                "  AND hs.signal_date = :day "
                "  AND hs.signal_type IN ('EXIT_FROM_BUY', 'EXIT_FROM_SELL')"
            ),
            {"ex": exchange, "tf": timeframe, "day": market_day},
        ).all()
    return [r[0] for r in rows]
