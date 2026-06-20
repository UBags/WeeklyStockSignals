"""Read queries for the GUI views.

Pure SQL functions taking a Connection, returning typed records.
Kept separate from view code so the queries can be tested without
Qt.

These run on background workers (NEVER on the GUI thread) because
they may scan ~2300-row tables.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass(frozen=True, slots=True)
class TickerRow:
    """One row in the tickers tab's main table."""
    ticker_id: int
    exchange: str
    symbol: str
    instrument_type: str
    full_name: Optional[str]
    isin: Optional[str]
    tier: int
    is_active: bool
    delisted_at: Optional[datetime]
    last_successful_bar_date: Optional[date]
    consecutive_failures: int
    parked: bool


@dataclass(frozen=True, slots=True)
class SignalEventRow:
    """For the per-ticker detail panel.

    `bar_timestamp` is the start instant of the OHLCV bar the signal
    is attached to (for weekly: IST-Monday-anchored, so e.g. a Monday
    at 00:00 UTC).

    `computed_at` is when the compute pass actually fired the signal —
    almost always strictly later than `bar_timestamp`, since the
    orchestrator's scheduled ticks fall inside the trading session.
    The two can differ by hours or days; for weekly signals on the
    in-progress bar, multiple `computed_at` values share the same
    `bar_timestamp` (one per tick that observed a regime change).
    """
    event_id: int
    bar_timestamp: datetime
    computed_at: datetime
    timeframe: str
    signal: str
    price_at_signal: float


@dataclass(frozen=True, slots=True)
class SymbolHistoryRow:
    """For the rename-history panel."""
    history_id: int
    old_symbol: str
    new_symbol: str
    changed_at: datetime
    detected_via: str


def list_tickers(
    conn: Connection,
    *,
    exchange: str = "NSE",
    include_delisted: bool = False,
    tier_filter: Optional[int] = None,
    symbol_substr: Optional[str] = None,
    limit: int = 5000,
) -> list[TickerRow]:
    """The main tickers tab query. Left-joins etl_ticker_status so we
    get failure-count and parked info inline (NULL when the ticker
    has never been touched by the ETL — equivalent to 0/False)."""
    sql = """
        SELECT t.ticker_id, t.exchange, t.symbol, t.instrument_type,
               t.full_name, t.isin, t.tier, t.is_active, t.delisted_at,
               s.last_successful_bar_date,
               COALESCE(s.consecutive_failures, 0) AS consecutive_failures,
               COALESCE(s.parked, FALSE) AS parked
        FROM tickers t
        LEFT JOIN etl_ticker_status s ON t.ticker_id = s.ticker_id
        WHERE t.exchange = :ex
    """
    params: dict = {"ex": exchange}
    if not include_delisted:
        sql += " AND t.is_active = TRUE AND t.delisted_at IS NULL"
    if tier_filter is not None:
        sql += " AND t.tier = :tier"
        params["tier"] = tier_filter
    if symbol_substr:
        sql += " AND UPPER(t.symbol) LIKE :substr"
        params["substr"] = f"%{symbol_substr.upper()}%"
    sql += " ORDER BY t.tier ASC, t.symbol ASC LIMIT :lim"
    params["lim"] = limit

    rows = conn.execute(text(sql), params)
    return [
        TickerRow(
            ticker_id=r.ticker_id, exchange=r.exchange, symbol=r.symbol,
            instrument_type=r.instrument_type, full_name=r.full_name,
            isin=r.isin, tier=r.tier, is_active=r.is_active,
            delisted_at=r.delisted_at,
            last_successful_bar_date=r.last_successful_bar_date,
            consecutive_failures=r.consecutive_failures,
            parked=r.parked,
        )
        for r in rows
    ]


def recent_signal_events(
    conn: Connection,
    ticker_id: int,
    *,
    limit: int = 10,
) -> list[SignalEventRow]:
    """The detail panel's signal-history list.

    Ordered by `computed_at DESC` (when the signal fired) rather than
    `bar_timestamp DESC` (when the bar started), so the most recently
    *generated* signal sits at the top — matching how the operator
    thinks about the panel ("what fired most recently?"). For weekly
    signals on the in-progress bar, multiple events share the same
    `bar_timestamp`; sorting by `computed_at` is what separates them
    visually.

    The DB column is `signal_type` (see signal_events DDL in
    0001_initial_schema.sql); we alias it to `signal` here so the
    dataclass field and the view's e.signal access stay consistent
    with the HTTP-side payload shape, which also aliases this way
    (see transports/http/routes/signals.py).
    """
    rows = conn.execute(text("""
        SELECT event_id, bar_timestamp, computed_at, timeframe,
               signal_type AS signal, price_at_signal
        FROM signal_events
        WHERE ticker_id = :tid
        ORDER BY computed_at DESC
        LIMIT :lim
    """), {"tid": ticker_id, "lim": limit})
    return [
        SignalEventRow(
            event_id=r.event_id, bar_timestamp=r.bar_timestamp,
            computed_at=r.computed_at,
            timeframe=r.timeframe, signal=r.signal,
            price_at_signal=float(r.price_at_signal),
        )
        for r in rows
    ]


def ticker_symbol_history(
    conn: Connection, ticker_id: int,
) -> list[SymbolHistoryRow]:
    """Renames for one ticker, most-recent first."""
    rows = conn.execute(text("""
        SELECT history_id, old_symbol, new_symbol, changed_at, detected_via
        FROM symbol_history
        WHERE ticker_id = :tid
        ORDER BY changed_at DESC
    """), {"tid": ticker_id})
    return [
        SymbolHistoryRow(
            history_id=r.history_id, old_symbol=r.old_symbol,
            new_symbol=r.new_symbol, changed_at=r.changed_at,
            detected_via=r.detected_via,
        )
        for r in rows
    ]


def last_run_signals(
    conn: Connection, *, exchange: str = "NSE",
) -> dict[int, str]:
    """Return {ticker_id: signal_type} for tickers that fired in the
    most recent compute pass on `exchange`.

    "Most recent compute pass" is defined as: all signal_events whose
    `computed_at` falls within a 60-second window of the global
    `MAX(computed_at)`. The orchestrator's signal-emission per tick
    completes in well under a second, so 60s comfortably groups one
    tick's events while staying narrower than the smallest inter-tick
    gap (NSE post-open to mid-session is 2h 45min in Phase 16a).

    Why "most recent" rather than "today's" or "last hour":
      - "Today's" misses the first tick of the day before it fires.
      - "Last hour" can miss a tick if the GUI is opened > 1h after
        the last tick of the day.
      - "Most recent" always returns something useful provided any
        signal has ever fired, and naturally goes empty when nothing
        has fired (initial deploys, ticker universe with no movers).

    If a ticker has multiple events in the same window (unusual:
    would require an orchestrator catching up across several missed
    weekly bars), the latest by `bar_timestamp` wins via
    `DISTINCT ON (s.ticker_id) ... ORDER BY s.bar_timestamp DESC`.

    Empty dict is a normal return — no events ever, or all events
    older than 60s before the global max (shouldn't happen but
    surfaces cleanly as no coloring).
    """
    # Phase 6-2 fix: the unification moved industry signal_events into
    # the same table as ticker signal_events. Industry compute runs
    # AFTER ticker compute in the EoD pipeline, so an unscoped
    # MAX(computed_at) anchors to the industry tick — and the 60-second
    # window then excludes the ticker rows. Scope the MAX to ticker
    # rows only.
    rows = conn.execute(text("""
        SELECT DISTINCT ON (s.ticker_id) s.ticker_id, s.signal_type
        FROM signal_events s
        JOIN tickers t USING (ticker_id)
        WHERE t.exchange = :ex
          AND s.ticker_id IS NOT NULL
          AND s.computed_at >= (
              (SELECT MAX(computed_at)
               FROM signal_events
               WHERE ticker_id IS NOT NULL)
              - INTERVAL '60 seconds'
          )
        ORDER BY s.ticker_id, s.bar_timestamp DESC
    """), {"ex": exchange})
    return {r.ticker_id: r.signal_type for r in rows}


def ticker_universe_summary(
    conn: Connection, *, exchange: str = "NSE",
) -> dict:
    """For the bottom-bar counts: active/delisted/parked/by-tier."""
    result = conn.execute(text("""
        SELECT
          COUNT(*) FILTER (WHERE t.is_active = TRUE AND t.delisted_at IS NULL)
            AS active,
          COUNT(*) FILTER (WHERE t.delisted_at IS NOT NULL) AS delisted,
          COUNT(*) FILTER (WHERE t.tier = 1 AND t.is_active = TRUE
                          AND t.delisted_at IS NULL) AS tier_1,
          COUNT(*) FILTER (WHERE t.tier = 2 AND t.is_active = TRUE
                          AND t.delisted_at IS NULL) AS tier_2,
          COUNT(*) FILTER (WHERE t.tier = 3 AND t.is_active = TRUE
                          AND t.delisted_at IS NULL) AS tier_3,
          COUNT(*) FILTER (WHERE s.parked = TRUE) AS parked
        FROM tickers t
        LEFT JOIN etl_ticker_status s ON t.ticker_id = s.ticker_id
        WHERE t.exchange = :ex
    """), {"ex": exchange}).first()
    return {
        "active": result.active,
        "delisted": result.delisted,
        "tier_1": result.tier_1,
        "tier_2": result.tier_2,
        "tier_3": result.tier_3,
        "parked": result.parked,
    }



@dataclass(frozen=True, slots=True)
class TickerComboRow:
    """One row in the Signals tab's ticker dropdown.

    The dropdown groups tickers into two sections: those with a
    signal event in the last 2 weeks (top, colored by signal type)
    and those without (below, grey background).
    `has_recent_signal` drives the partition; `latest_signal`
    drives the color (BUY=green, SELL=red, EXIT_*=blue).
    """
    # Phase 6-3: surfaced so the Signals tab dropdown can map rows
    # to rank_scores for sorting + tooltip rendering.
    ticker_id: int
    exchange: str
    symbol: str
    instrument_type: str
    full_name: Optional[str]
    has_recent_signal: bool
    latest_signal: Optional[str] = None


def ticker_universe_with_recent_signals(
    conn: Connection,
    *,
    exchange: Optional[str] = None,
) -> list[TickerComboRow]:
    """Active ticker universe, flagged for recent signal activity.

    "Recent" means: any row in `signal_events` whose `bar_timestamp`
    lies within the rolling 'this week + last week' window, where
    weeks are Monday-anchored IST (the same anchor the Pine-style
    weekly bucketing uses).

    The cutoff is computed in SQL as:
        date_trunc('week', NOW() AT TIME ZONE 'Asia/Kolkata')
            - INTERVAL '7 days'
        interpreted as IST and converted back to timestamptz UTC
        for comparison with bar_timestamp.

    Example: at any moment on Thu 2026-06-18 IST,
        date_trunc('week', NOW() AT TIME ZONE 'Asia/Kolkata')
            = 2026-06-15 00:00 (Mon of this week IST)
        - 7 days
            = 2026-06-08 00:00 (Mon of last week IST)
        AT TIME ZONE 'Asia/Kolkata'
            = 2026-06-07 18:30 UTC (timestamptz form for comparison)

    Implementation: one CTE producing distinct ticker_ids with any
    matching event, then a LEFT JOIN to the tickers table. Single
    scan of signal_events, no N+1. The LEFT JOIN means tickers with
    zero historical events still appear (with has_recent_signal =
    False), which is what the dropdown wants.

    Ordering: (has_recent_signal DESC, symbol ASC) so the consumer
    can split the list at the True->False boundary in one pass via
    `partition_ticker_rows`.
    """
    sql = """
        WITH recent_sigs AS (
            SELECT DISTINCT ON (ticker_id)
                ticker_id, signal_type
            FROM signal_events
            WHERE bar_timestamp >= (
                (date_trunc('week', (NOW() AT TIME ZONE 'Asia/Kolkata'))::timestamp
                 - INTERVAL '7 days') AT TIME ZONE 'Asia/Kolkata'
            )
            ORDER BY ticker_id, computed_at DESC
        )
        SELECT t.ticker_id, t.exchange, t.symbol, t.instrument_type,
               t.full_name,
               (r.ticker_id IS NOT NULL) AS has_recent_signal,
               r.signal_type AS latest_signal
        FROM tickers t
        LEFT JOIN recent_sigs r ON r.ticker_id = t.ticker_id
        WHERE t.is_active = TRUE AND t.delisted_at IS NULL
    """
    params: dict = {}
    if exchange is not None:
        sql += " AND t.exchange = :ex"
        params["ex"] = exchange
    sql += " ORDER BY has_recent_signal DESC, t.symbol ASC"

    rows = conn.execute(text(sql), params)
    return [
        TickerComboRow(
            ticker_id=int(r.ticker_id),
            exchange=r.exchange,
            symbol=r.symbol,
            instrument_type=r.instrument_type,
            full_name=r.full_name,
            has_recent_signal=bool(r.has_recent_signal),
            latest_signal=r.latest_signal,
        )
        for r in rows
    ]



def top_n_tickers_in_industry_by_mcap(
    conn: Connection,
    industry_name: str,
    *,
    n: int = 12,
    exchange: str = "NSE",
) -> list[TickerRow]:
    """Top N tickers in `industry_name` by latest available market cap.

    Used by the Signals tab's IndustryPanel to populate the 4x3 mini
    grid of related stocks. Returns fewer than N rows if the industry
    has fewer active tickers — caller (the panel) leaves trailing
    grid slots blank rather than reshaping.

    Latest market cap: each ticker's most recent `daily_market_cap`
    row. The DISTINCT ON (ticker_id) pattern picks the latest per
    ticker in one scan; the outer query ranks those rows by mcap
    DESC and trims to N.

    Active filter: same as the dropdown — is_active=TRUE and
    delisted_at IS NULL. ETFs and indices are excluded by joining
    on instrument_industry (which is populated only for equity-style
    instruments).
    """
    rows = conn.execute(text("""
        WITH latest_mcap AS (
            SELECT DISTINCT ON (ticker_id)
                ticker_id, market_cap
            FROM daily_market_cap
            ORDER BY ticker_id, trade_date DESC
        )
        SELECT t.ticker_id, t.exchange, t.symbol, t.instrument_type,
               t.full_name, t.isin, t.tier, t.is_active, t.delisted_at,
               NULL::date AS last_successful_bar_date,
               0 AS consecutive_failures,
               FALSE AS parked
        FROM tickers t
        JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
        JOIN latest_mcap m ON m.ticker_id = t.ticker_id
        WHERE ii.industry = :ind
          AND t.exchange = :ex
          AND t.is_active = TRUE
          AND t.delisted_at IS NULL
        ORDER BY m.market_cap DESC NULLS LAST, t.symbol ASC
        LIMIT :lim
    """), {"ind": industry_name, "ex": exchange, "lim": n})
    return [
        TickerRow(
            ticker_id=r.ticker_id, exchange=r.exchange, symbol=r.symbol,
            instrument_type=r.instrument_type, full_name=r.full_name,
            isin=r.isin, tier=r.tier, is_active=r.is_active,
            delisted_at=r.delisted_at,
            last_successful_bar_date=r.last_successful_bar_date,
            consecutive_failures=r.consecutive_failures,
            parked=r.parked,
        )
        for r in rows
    ]

# ---------- Overall Indicators (Tickers tab right column) ---------- #


@dataclass(frozen=True)
class BenchmarkWindowSnapshot:
    """One window's worth of benchmark stats for a stock + its industry.

    Returned as a member of IndustryIndicators.short/medium/long. The
    rank_score is computed at fetch time using the same formula as
    notifications/benchmark_rerank:
        0.5 × stock_rs_pct + 0.3 × industry_rs_pct + 0.2 × stock_r2
    Surfacing it on the GUI makes the email ordering auditable
    end-to-end. None for all fields when the corresponding stats row
    is missing (sub-threshold history).
    """
    # Industry vs NIFTY
    industry_alpha: Optional[float]
    industry_beta: Optional[float]
    industry_rs_pct: Optional[float]
    # Stock vs Industry
    stock_alpha: Optional[float]
    stock_beta: Optional[float]
    stock_rs_pct: Optional[float]
    stock_r2: Optional[float]
    # Composite for ranking
    rank_score: Optional[float]

@dataclass(frozen=True)
class IndustryIndicators:
    """Summary indicators about the industry a ticker belongs to.

    Populates the "Overall Indicators" card on the Tickers tab. All
    fields nullable so the renderer can show "—" for tickers whose
    industry has no indicator_state coverage yet.

    regime: latest display_regime for the industry (-1 / 0 / +1).
    trend:  sign of (med_ema_latest - med_ema_two_bars_back). Same
            tri-state semantics as regime so the renderer can reuse
            the bullish/bearish/neutral coloring logic.
    """
    industry_name: Optional[str]
    regime: Optional[int]              # -1 / 0 / +1
    trend: Optional[int]               # -1 / 0 / +1
    latest_trade_date: Optional[date]
    # Phase 6-3 (revised): benchmark stats indexed by window. Three
    # snapshots — SHORT (26w), MEDIUM (52w), LONG (156w) — so the
    # Tickers tab can render them in a tabbed view. Any window may be
    # None when no stats row exists yet (sub-threshold history).
    short: Optional["BenchmarkWindowSnapshot"] = None
    medium: Optional["BenchmarkWindowSnapshot"] = None
    long: Optional["BenchmarkWindowSnapshot"] = None


def industry_indicators_for_ticker(conn, ticker_id: int) -> IndustryIndicators:
    """Return regime + trend + Phase 6-3 windowed benchmark stats.

    Three SQL roundtrips (all cheap; <15ms total):
      1. indicator_state — latest 3 weekly rows for regime + trend.
      2. stock_benchmark_stats — latest row per window
         (SHORT/MEDIUM/LONG), INDUSTRY benchmark.
      3. industry_benchmark_stats — latest row per window for the
         industry vs NIFTY.

    The composite rank_score is computed here per-window using the
    same formula as notifications/benchmark_rerank:
        0.5 × stock_RS + 0.3 × industry_RS + 0.2 × R²
    """
    state_rows = conn.execute(text("""
        SELECT s.med_ema, s.display_regime, s.trade_date, ii.industry
        FROM instrument_industry ii
        JOIN indicator_state s
          ON s.industry_name = ii.industry
        WHERE ii.ticker_id = :tid
          AND s.industry_name IS NOT NULL
        ORDER BY s.trade_date DESC
        LIMIT 3
    """), {"tid": ticker_id}).all()

    if not state_rows:
        return IndustryIndicators(
            industry_name=None, regime=None, trend=None,
            latest_trade_date=None,
        )

    latest = state_rows[0]
    industry = latest.industry
    regime = (
        int(latest.display_regime)
        if latest.display_regime is not None else None
    )

    trend: Optional[int] = None
    if len(state_rows) >= 3:
        delta = float(state_rows[0].med_ema) - float(state_rows[2].med_ema)
        if delta > 0:
            trend = 1
        elif delta < 0:
            trend = -1
        else:
            trend = 0

    # Stock vs Industry — latest row per window. DISTINCT ON gives
    # us one row per window_label, ordered by latest as_of_date.
    stock_rows = conn.execute(text("""
        SELECT DISTINCT ON (window_label)
               window_label, alpha_annual, beta, rs_pct, r_squared
        FROM stock_benchmark_stats
        WHERE ticker_id = :tid
          AND benchmark_kind = 'INDUSTRY'
        ORDER BY window_label, as_of_date DESC
    """), {"tid": ticker_id}).all()
    stock_by_window = {r.window_label: r for r in stock_rows}

    # Industry vs NIFTY — latest row per window.
    industry_rows = conn.execute(text("""
        SELECT DISTINCT ON (window_label)
               window_label, alpha_annual, beta, rs_pct
        FROM industry_benchmark_stats
        WHERE industry_name = :n
        ORDER BY window_label, as_of_date DESC
    """), {"n": industry}).all()
    industry_by_window = {r.window_label: r for r in industry_rows}

    def _f(value):
        return float(value) if value is not None else None

    def _build_snapshot(window_label: str) -> Optional[BenchmarkWindowSnapshot]:
        s = stock_by_window.get(window_label)
        i = industry_by_window.get(window_label)
        if s is None and i is None:
            return None
        ind_alpha = _f(i.alpha_annual) if i else None
        ind_beta = _f(i.beta) if i else None
        ind_rs = _f(i.rs_pct) if i else None
        s_alpha = _f(s.alpha_annual) if s else None
        s_beta = _f(s.beta) if s else None
        s_rs = _f(s.rs_pct) if s else None
        s_r2 = _f(s.r_squared) if s else None
        # rank_score uses the SAME 0.5/0.3/0.2 weights for every window
        # so a stock's LONG rank_score can be compared meaningfully with
        # its SHORT rank_score (high LONG, low SHORT = momentum fading).
        rank_score = (
            0.5 * (s_rs or 0.0)
            + 0.3 * (ind_rs or 0.0)
            + 0.2 * (s_r2 or 0.0)
        )
        return BenchmarkWindowSnapshot(
            industry_alpha=ind_alpha, industry_beta=ind_beta,
            industry_rs_pct=ind_rs,
            stock_alpha=s_alpha, stock_beta=s_beta,
            stock_rs_pct=s_rs, stock_r2=s_r2,
            rank_score=rank_score,
        )

    return IndustryIndicators(
        industry_name=industry,
        regime=regime,
        trend=trend,
        latest_trade_date=latest.trade_date,
        short=_build_snapshot("SHORT"),
        medium=_build_snapshot("MEDIUM"),
        long=_build_snapshot("LONG"),
    )

