"""Per-batch enrichment lookups for HTML email rendering.

For each NotificationBatch sent to a subscriber, we want a richer view
than the plain-text 'one event per line' format: per-ticker market cap,
sector classification, the sector's current display regime, day-over-day
sector direction, and the ticker's last 5 signal events. None of this
data lives on `SignalEvent` itself — it's spread across
instrument_industry, daily_market_cap, indicator_state (industry rows),
industry_index_value, and signal_events. This module bundles the lookups
into a single function the email transport invokes per send.

Design:
  - `BatchEnrichment` is a frozen container — one entry per ticker
    referenced by the batch.
  - `enrich_batch(engine, batch)` runs four small queries (one per
    data family) and assembles the container. Bounded DB roundtrips
    per send; no N+1.
  - All lookups gracefully degrade. A ticker with no market cap row
    (delisted, never priced) still gets an entry — the field is just
    None and the renderer prints '—'. The intent is best-effort
    decoration, not 'fail the send'.
  - Pure SQL + dataclasses. The transport calls in via a thin
    `enrichment_provider` callable so the transport itself stays
    DB-agnostic for tests.

Sector zone / sector direction:
  - 'Zone' is the latest indicator_state.display_regime for the
    industry the ticker belongs to. -1 = BEARISH / red, 0 = NEUTRAL /
    grey, +1 = BULLISH / green. Uses indicator_state's industry rows
    (industry_name IS NOT NULL).
  - 'Direction' is the sign of the day-over-day change in
    industry_index_value. ▲ when today's value > yesterday's, ▼ when
    lower, ● when equal or only one day of data is available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import Engine, text

from ..domain import NotificationBatch, TickerRef

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HistoricalSignal:
    """One past signal event used in the 'Last 5 trading days signals'
    column. Stored in ascending order (oldest → newest) by the
    enrichment loader."""
    bar_timestamp: datetime
    signal_type: str          # 'BUY' | 'SELL' | 'EXIT_FROM_BUY' | 'EXIT_FROM_SELL'
    price_at_signal: float


@dataclass(frozen=True, slots=True)
class TickerEnrichment:
    """Everything we know about one ticker for HTML rendering.

    Every field except `ticker_ref` is optional/defaulted; rendering
    code must tolerate missing values and substitute a placeholder
    (typically '—' or NEUTRAL coloring).
    """
    ticker_ref: TickerRef
    sector: Optional[str] = None
    industry: Optional[str] = None
    market_cap_rupees: Optional[float] = None
    sector_zone: int = 0          # -1 bearish, 0 neutral, +1 bullish
    sector_direction: int = 0     # -1 down, 0 flat/unknown, +1 up
    recent_signals: tuple[HistoricalSignal, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class BatchEnrichment:
    """Enrichment payload for one NotificationBatch.

    Keyed by (exchange, symbol, instrument) tuple — same identity that
    SignalEvent.ticker carries — so the renderer's lookup is O(1).
    """
    by_ticker: dict[tuple[str, str, str], TickerEnrichment]

    def get(self, ref: TickerRef) -> Optional[TickerEnrichment]:
        return self.by_ticker.get((ref.exchange, ref.symbol, ref.instrument))


# Empty enrichment used when the batch has zero events, or when we
# bail early due to a DB error. Renderer treats this as 'no data'
# and falls back to '—' / NEUTRAL across the board.
EMPTY_ENRICHMENT = BatchEnrichment(by_ticker={})


def enrich_batch(engine: Engine, batch: NotificationBatch) -> BatchEnrichment:
    """Look up enrichment data for every ticker in the batch.

    Returns a `BatchEnrichment` keyed by ticker tuple. Tickers with no
    matching data still get an entry, so the renderer always finds
    something to render — just with None / 0 / empty defaults.

    Errors (DB down, table missing) are caught and logged; we return
    EMPTY_ENRICHMENT rather than propagating, so a degraded DB never
    blocks email delivery. The send falls back to text-only rendering
    one layer up.
    """
    if not batch.events:
        return EMPTY_ENRICHMENT

    # Collect unique tickers from the batch.
    unique_refs: dict[tuple[str, str, str], TickerRef] = {}
    for ev in batch.events:
        key = (ev.ticker.exchange, ev.ticker.symbol, ev.ticker.instrument)
        unique_refs.setdefault(key, ev.ticker)
    refs = list(unique_refs.values())

    try:
        with engine.connect() as conn:
            ticker_rows = _fetch_ticker_basics(conn, refs)
            if not ticker_rows:
                # None of the tickers in the batch are known to the
                # tickers table. Strange but not fatal — render
                # decorationless.
                logger.warning(
                    "enrich_batch: no ticker rows matched %d unique refs", len(refs),
                )
                return EMPTY_ENRICHMENT

            ticker_ids = [r["ticker_id"] for r in ticker_rows]
            industries = sorted({
                r["industry"] for r in ticker_rows if r["industry"]
            })

            zones = _fetch_sector_zones(conn, industries) if industries else {}
            directions = _fetch_sector_directions(conn, industries) if industries else {}
            recent_by_ticker_id = _fetch_recent_signals(conn, ticker_ids)
    except Exception:
        logger.exception(
            "enrich_batch failed; falling back to text-only render"
        )
        return EMPTY_ENRICHMENT

    out: dict[tuple[str, str, str], TickerEnrichment] = {}
    for row in ticker_rows:
        ref = TickerRef(
            exchange=row["exchange"],
            symbol=row["symbol"],
            instrument=row["instrument"],
        )
        industry = row["industry"]
        zone = zones.get(industry, 0) if industry else 0
        direction = directions.get(industry, 0) if industry else 0
        recent = recent_by_ticker_id.get(row["ticker_id"], ())
        out[(ref.exchange, ref.symbol, ref.instrument)] = TickerEnrichment(
            ticker_ref=ref,
            sector=row["sector"],
            industry=industry,
            market_cap_rupees=(
                float(row["market_cap_rupees"])
                if row["market_cap_rupees"] is not None
                else None
            ),
            sector_zone=int(zone),
            sector_direction=int(direction),
            recent_signals=recent,
        )
    return BatchEnrichment(by_ticker=out)


# ---------------------------------------------------------------------------
# SQL helpers — each returns a small intermediate dict so the assembly
# above is straightforward dict-keyed lookup.
# ---------------------------------------------------------------------------


def _fetch_ticker_basics(conn, refs: list[TickerRef]) -> list[dict]:
    """Resolve (exchange, symbol, instrument) tuples to ticker_id +
    sector + industry + latest market_cap. One query for all refs.

    Uses Postgres `unnest` over three parallel text arrays so the
    parameter binding stays simple (psycopg2 handles list-of-str
    directly). The latest mcap is fetched via a correlated subquery
    rather than a join — keeps the result one-row-per-ticker without
    DISTINCT ON.
    """
    # NOTE: Parens around each bind param are REQUIRED. SQLAlchemy's text()
    # parser uses a `(?!:)` lookahead when extracting bind-param names; with
    # `:exchanges::text[]` it backs off greedily and binds `:exchange`
    # (singular) instead of `:exchanges`, then the params-dict lookup fails
    # at execute time with "A value is required for bind parameter
    # 'exchange'". That exception is swallowed by enrich_batch's broad
    # try/except and the renderer ends up with EMPTY_ENRICHMENT, producing
    # an email where every row shows `—` / NEUTRAL for mcap, sector,
    # zone, direction, and last-5. The `(:foo)::text[]` form forces the
    # parser to see the closing paren as a non-colon lookahead and binds
    # the correct (plural) name. Equivalent SQL on the Postgres side.
    sql = text("""
        WITH wanted AS (
            SELECT * FROM unnest(
                (:exchanges)::text[],
                (:symbols)::text[],
                (:instruments)::text[]
            ) AS u(exchange, symbol, instrument)
        )
        SELECT
            t.ticker_id,
            t.exchange,
            t.symbol,
            t.instrument,
            ii.sector,
            ii.industry,
            (
                SELECT dmc.market_cap
                FROM daily_market_cap dmc
                WHERE dmc.ticker_id = t.ticker_id
                ORDER BY dmc.trade_date DESC
                LIMIT 1
            ) AS market_cap_rupees
        FROM tickers t
        JOIN wanted w
            ON w.exchange   = t.exchange
           AND w.symbol     = t.symbol
           AND w.instrument = t.instrument
        LEFT JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
    """)
    rows = conn.execute(sql, {
        "exchanges":   [r.exchange   for r in refs],
        "symbols":     [r.symbol     for r in refs],
        "instruments": [r.instrument for r in refs],
    }).mappings().all()
    return [dict(r) for r in rows]


def _fetch_sector_zones(conn, industries: list[str]) -> dict[str, int]:
    """Latest display_regime per industry from indicator_state's
    industry rows. Returns {industry_name: regime_int} where regime is
    -1 / 0 / +1.

    DISTINCT ON is the right tool here: one row per industry, picking
    the most recent trade_date.
    """
    if not industries:
        return {}
    sql = text("""
        SELECT DISTINCT ON (industry_name)
            industry_name,
            display_regime
        FROM indicator_state
        WHERE industry_name = ANY(:industries)
        ORDER BY industry_name, trade_date DESC
    """)
    rows = conn.execute(sql, {"industries": industries}).all()
    return {r.industry_name: int(r.display_regime) for r in rows}


def _fetch_sector_directions(conn, industries: list[str]) -> dict[str, int]:
    """Sign of day-over-day change in industry_index_value per industry.

    Returns {industry_name: -1 | 0 | +1}. Industries with fewer than 2
    days of data return 0 (treated as 'flat / unknown' by the renderer).
    """
    if not industries:
        return {}
    sql = text("""
        WITH latest_two AS (
            SELECT
                industry_name,
                trade_date,
                value,
                ROW_NUMBER() OVER (
                    PARTITION BY industry_name
                    ORDER BY trade_date DESC
                ) AS rn
            FROM industry_index_value
            WHERE industry_name = ANY(:industries)
        ),
        pivoted AS (
            SELECT
                industry_name,
                MAX(CASE WHEN rn = 1 THEN value END) AS today_val,
                MAX(CASE WHEN rn = 2 THEN value END) AS prev_val
            FROM latest_two
            WHERE rn <= 2
            GROUP BY industry_name
        )
        SELECT
            industry_name,
            CASE
                WHEN prev_val IS NULL THEN 0
                WHEN today_val > prev_val THEN  1
                WHEN today_val < prev_val THEN -1
                ELSE 0
            END AS direction
        FROM pivoted
    """)
    rows = conn.execute(sql, {"industries": industries}).all()
    return {r.industry_name: int(r.direction) for r in rows}


def _fetch_recent_signals(
    conn, ticker_ids: list[int]
) -> dict[int, tuple[HistoricalSignal, ...]]:
    """Last 5 signal_events per ticker_id, returned in ascending order.

    Implemented via ROW_NUMBER() window so we get exactly 5 per ticker
    in one query.
    """
    if not ticker_ids:
        return {}
    sql = text("""
        WITH numbered AS (
            SELECT
                ticker_id,
                bar_timestamp,
                signal_type,
                price_at_signal,
                ROW_NUMBER() OVER (
                    PARTITION BY ticker_id
                    ORDER BY bar_timestamp DESC
                ) AS rn
            FROM signal_events
            WHERE ticker_id = ANY(:tids)
        )
        SELECT ticker_id, bar_timestamp, signal_type, price_at_signal
        FROM numbered
        WHERE rn <= 5
        ORDER BY ticker_id, bar_timestamp ASC
    """)
    rows = conn.execute(sql, {"tids": ticker_ids}).all()
    out: dict[int, list[HistoricalSignal]] = {}
    for r in rows:
        out.setdefault(r.ticker_id, []).append(HistoricalSignal(
            bar_timestamp=r.bar_timestamp,
            signal_type=r.signal_type,
            price_at_signal=float(r.price_at_signal),
        ))
    return {tid: tuple(sigs) for tid, sigs in out.items()}