"""Read queries for the GUI views.

Pure SQL functions taking a Connection, returning typed records.
Kept separate from view code so the queries can be tested without
Qt.

These run on background workers (NEVER on the GUI thread) because
they may scan ~2300-row tables.
"""

from __future__ import annotations
from decimal import Decimal

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.engine import Connection
from ..domain.market_session import market_tz


@dataclass(frozen=True, slots=True)
class TickerRow:
    """One row in the tickers tab's main table.

    `market_cap` is the latest available daily_market_cap value (INR),
    populated only by queries that join daily_market_cap (e.g.
    `top_n_tickers_in_industry_by_mcap`). Other constructors leave it
    None — the field is informational, never required.
    """
    ticker_id: int
    exchange: str
    symbol: str
    instrument_type: str
    full_name: str | None
    isin: str | None
    tier: int
    is_active: bool
    delisted_at: datetime | None
    last_successful_bar_date: date | None
    consecutive_failures: int
    parked: bool
    market_cap: Decimal | None = None
    industry: str | None = None


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
    # Step 12a: int | None — current_signals contributes
    # rows with event_id=None (no surrogate id; PK is ticker_id
    # alone). historical_signals contributes its real event_id.
    event_id: int | None
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
    tier_filter: int | None = None,
    symbol_substr: str | None = None,
    limit: int = 5000,
) -> list[TickerRow]:
    """The main tickers tab query. Left-joins etl_ticker_status so we
    get failure-count and parked info inline (NULL when the ticker
    has never been touched by the ETL — equivalent to 0/False)."""
    sql = """
        WITH latest_mcap AS (
            SELECT DISTINCT ON (ticker_id)
                ticker_id, market_cap
            FROM daily_market_cap
            ORDER BY ticker_id, trade_date DESC
        )
        SELECT t.ticker_id, t.exchange, t.symbol, t.instrument_type,
               t.full_name, t.isin, t.tier, t.is_active, t.delisted_at,
               s.last_successful_bar_date,
               COALESCE(s.consecutive_failures, 0) AS consecutive_failures,
               COALESCE(s.parked, FALSE) AS parked,
               ii.industry AS industry,
               lm.market_cap AS market_cap
        FROM tickers t
        LEFT JOIN etl_ticker_status   s  ON t.ticker_id = s.ticker_id
        LEFT JOIN instrument_industry ii ON t.ticker_id = ii.ticker_id
        LEFT JOIN latest_mcap         lm ON t.ticker_id = lm.ticker_id
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
            market_cap=r.market_cap,
            industry=r.industry,
        )
        for r in rows
    ]


# Step 12a: V1 read path — queries.py reads current_signals + historical_signals.


def _week_anchors(
    conn: Connection, exchange: str,
) -> tuple[datetime, datetime]:
    """Return (current_monday_ts, previous_monday_ts) as timestamptz
    values matching the bar_timestamp shape used in
    current_signals / historical_signals for weekly bars
    (Monday 00:00:00 UTC of the IST week).

    Week boundaries follow Postgres' date_trunc('week', ...) which
    is ISO 8601 (Monday-start). Sunday 23:59:59 IST belongs to the
    same week as its preceding Monday. This matches the operator's
    week definition: "00:00:01 Monday to 23:59:59 Sunday IST".

    The weekly bar's bar_timestamp is stored as Monday 00:00:00 UTC
    (renders as Monday 05:30:00 IST) — anchored to a Monday but
    using UTC midnight as the time-of-day. The helper returns the
    same shape so direct equality comparison works.

    Session-TZ independence: the SQL uses `AT TIME ZONE 'UTC'` on
    a naive timestamp (not `::timestamptz` cast against a date),
    which is deterministic regardless of the Postgres session TZ.
    An earlier version used the ::timestamptz cast, which returned
    Monday 00:00 in the SESSION TZ — silently empty on any session
    with TZ != UTC. See STEP-12C-POSTMORTEM comment inside the SQL.
    """
    row = conn.execute(text("""
        SELECT
            -- STEP-12C-POSTMORTEM: explicit AT TIME ZONE 'UTC' so the
            -- returned timestamptz is Monday 00:00 UTC regardless of
            -- session TZ. Previously ::timestamptz cast against a
            -- ::date returned Monday 00:00 in the SESSION TZ, which
            -- silently returned empty when session TZ was Asia/Kolkata.
            -- :tz is the EXCHANGE's zone, not India's. It decides which
            -- DAY "now" falls on, and therefore which week. The OUTER
            -- AT TIME ZONE 'UTC' below is unchanged and must stay: see the
            -- note above — a ::timestamptz cast on a ::date returns Monday
            -- 00:00 in the SESSION tz, which silently returned EMPTY.
            (date_trunc('week',
                        (NOW() AT TIME ZONE :tz))::date::timestamp
                AT TIME ZONE 'UTC')              AS current_monday_ts,
            ((date_trunc('week',
                         (NOW() AT TIME ZONE :tz))::date
              - INTERVAL '7 days'
             )::timestamp
                AT TIME ZONE 'UTC')              AS previous_monday_ts
    """), {"tz": market_tz(exchange).key}).first()
    return row.current_monday_ts, row.previous_monday_ts


def recent_signal_events(
    conn: Connection,
    ticker_id: int,
    limit: int = 5,
) -> list[SignalEventRow]:
    """Return the most recent N signals for one ticker.

    Step 12a: V1 read path. NO JOIN between current_signals and
    historical_signals — two separate queries composed in Python.

    Strategy (operator's spec):
      1. Check current_signals for a row for this ticker. If found,
         take it as row #1 (newest).
      2. Top up from historical_signals (bar_timestamp DESC,
         computed_at DESC) for the remaining (limit - 1) rows. If
         current_signals had no match, take all `limit` from
         historical.

    `SignalEventRow.event_id` is int | None. current_signals has
    no surrogate id (PK is ticker_id alone, no event log
    semantics), so its contribution carries event_id=None.
    historical_signals rows carry their actual event_id. No GUI
    consumer reads event_id today; the field is preserved on the
    dataclass to keep `tests/persistence/test_gui_queries.py`'s
    assertions usable when comparing identities.
    """
    if limit <= 0:
        return []

    out: list[SignalEventRow] = []

    # ---- 1. current_signals (at most 1 row per ticker) ----
    current = conn.execute(text("""
        SELECT bar_timestamp, computed_at, timeframe,
               signal_type AS signal, price_at_signal
          FROM current_signals
         WHERE ticker_id = :tid
    """), {"tid": ticker_id}).first()

    if current is not None:
        out.append(SignalEventRow(
            event_id=None,
            bar_timestamp=current.bar_timestamp,
            computed_at=current.computed_at,
            timeframe=current.timeframe,
            signal=current.signal,
            price_at_signal=float(current.price_at_signal),
        ))
        if limit == 1:
            return out

    # ---- 2. historical_signals top-up ----
    remaining = limit - len(out)
    rows = conn.execute(text("""
        SELECT signal_id AS event_id, bar_timestamp, computed_at,
               timeframe, signal_type AS signal, price_at_signal
          FROM historical_signals
         WHERE ticker_id = :tid
         ORDER BY bar_timestamp DESC, computed_at DESC
         LIMIT :lim
    """), {"tid": ticker_id, "lim": remaining})
    for r in rows:
        out.append(SignalEventRow(
            event_id=int(r.event_id),
            bar_timestamp=r.bar_timestamp,
            computed_at=r.computed_at,
            timeframe=r.timeframe,
            signal=r.signal,
            price_at_signal=float(r.price_at_signal),
        ))
    return out
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


@dataclass(frozen=True, slots=True)
class SignalColorInputs:
    """Per-ticker inputs to DetermineBackgroundColor, as RAW DB values.

    Kept in raw form (str signal_type / smallint display_regime) so this
    persistence-layer DTO stays free of GUI/enum coupling. Conversion to
    ChangeSignal / CoreRegime happens at the GUI paint boundary via
    DetermineBackgroundColor's own converters.

      current_signal  -- signal_type at the current weekly bar, or None
      current_regime  -- display_regime (-1/0/1) at the current bar, or None
      previous_signal -- signal_type at the previous weekly bar, or None
    """
    current_signal: str | None
    current_regime: int | None
    previous_signal: str | None


def _signal_color_inputs_by_ticker(
    conn: Connection, *, exchange: str | None,
) -> dict[int, SignalColorInputs]:
    """Shared lookup for both the Tickers tab and the Signals dropdown.

    Returns {ticker_id: SignalColorInputs} for every ticker that has a
    signal on the current OR previous weekly bar. A ticker with neither
    is omitted -- the coloring rule returns None for it regardless of
    regime, so there is nothing to color.

    Window (Monday 00:00 UTC anchors, see `_week_anchors`):
      current_signal  : current_signals@current_ts, else
                        historical_signals@current_ts (post-rotation).
      previous_signal : historical_signals@previous_ts.
      current_regime  : stock_indicator_state.display_regime@current_ts
                        (strict -- the current weekly bar; None if that
                        bar's state is not yet computed).

    exchange=None means all exchanges (legacy contract of
    ticker_universe_with_recent_signals).
    """
    current_ts, previous_ts = _week_anchors(conn, exchange)
    if exchange is None:
        ex_clause = ""
        ex_params: dict = {}
    else:
        ex_clause = " AND t.exchange = :ex"
        ex_params = {"ex": exchange}

    # ---- current-bar signal: current_signals first, else historical ----
    cur_sig: dict[int, str] = {}
    for r in conn.execute(text(f"""
        SELECT cs.ticker_id, cs.signal_type
          FROM current_signals cs
          JOIN tickers t USING (ticker_id)
         WHERE cs.bar_timestamp = :current_ts
               {ex_clause}
    """), {"current_ts": current_ts, **ex_params}):
        cur_sig[int(r.ticker_id)] = r.signal_type
    for r in conn.execute(text(f"""
        SELECT DISTINCT ON (hs.ticker_id)
               hs.ticker_id, hs.signal_type
          FROM historical_signals hs
          JOIN tickers t USING (ticker_id)
         WHERE hs.bar_timestamp = :current_ts
               {ex_clause}
         ORDER BY hs.ticker_id, hs.computed_at DESC
    """), {"current_ts": current_ts, **ex_params}):
        cur_sig.setdefault(int(r.ticker_id), r.signal_type)

    # ---- previous-bar signal: historical only ----
    prev_sig: dict[int, str] = {}
    for r in conn.execute(text(f"""
        SELECT DISTINCT ON (hs.ticker_id)
               hs.ticker_id, hs.signal_type
          FROM historical_signals hs
          JOIN tickers t USING (ticker_id)
         WHERE hs.bar_timestamp = :previous_ts
               {ex_clause}
         ORDER BY hs.ticker_id, hs.computed_at DESC
    """), {"previous_ts": previous_ts, **ex_params}):
        prev_sig[int(r.ticker_id)] = r.signal_type

    tids = set(cur_sig) | set(prev_sig)
    if not tids:
        return {}

    # ---- current-bar regime from stock_indicator_state (weekly) ----
    regime: dict[int, int] = {}
    for r in conn.execute(text(f"""
        SELECT sis.ticker_id, sis.display_regime
          FROM stock_indicator_state sis
          JOIN tickers t USING (ticker_id)
         WHERE sis.timeframe = '1W'
           AND sis.bar_timestamp = :current_ts
               {ex_clause}
    """), {"current_ts": current_ts, **ex_params}):
        tid = int(r.ticker_id)
        if tid in tids:
            regime[tid] = int(r.display_regime)

    return {
        tid: SignalColorInputs(
            current_signal=cur_sig.get(tid),
            current_regime=regime.get(tid),
            previous_signal=prev_sig.get(tid),
        )
        for tid in tids
    }


def last_run_signals(
    conn: Connection, *, exchange: str = "NSE",
) -> dict[int, SignalColorInputs]:
    """{ticker_id: SignalColorInputs} for the Tickers tab.

    Thin wrapper over `_signal_color_inputs_by_ticker`. The three inputs
    feed the single source of truth for the row color
    (trip_ema.gui.determine_background_color.DetermineBackgroundColor).

    Sort/prominence is a SEPARATE concern: the Tickers-tab proxy keys off
    the recent flip (current_signal or previous_signal), unchanged from
    the prior behaviour. Only the COLOR now also depends on
    current_regime + previous_signal.
    """
    return _signal_color_inputs_by_ticker(conn, exchange=exchange)
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

    Coloring inputs (current_signal / current_regime / previous_signal)
    feed DetermineBackgroundColor at the view's paint boundary. The
    dropdown SORT keys off `recent_signal` (= current_signal or
    previous_signal) via signal_group_industry_rank_sort_key -- sort and
    color are deliberately separate concerns.
    """
    ticker_id: int
    exchange: str
    symbol: str
    instrument_type: str
    full_name: str | None
    has_recent_signal: bool
    current_signal: str | None = None
    current_regime: int | None = None
    previous_signal: str | None = None
    industry: str | None = None
    market_cap: Decimal | None = None

    @property
    def recent_signal(self) -> str | None:
        """The flip that drives sort/prominence: current bar wins, else
        previous bar. NOT the color (color also considers regime)."""
        return self.current_signal or self.previous_signal


def ticker_universe_with_recent_signals(
    conn: Connection,
    *,
    exchange: str | None = None,
) -> list[TickerComboRow]:
    """Active ticker universe carrying the coloring inputs + a
    `has_recent_signal` flag.

    Uses the shared `_signal_color_inputs_by_ticker` lookup (current/
    previous weekly-bar window + current-bar regime), then joins to
    tickers / industry / market_cap for the row payload.

    has_recent_signal := the ticker has a signal on the current OR
    previous bar (i.e. it is present in the shared lookup). Ordering:
    (has_recent_signal DESC, symbol ASC) -- preserved; the view layers
    industry-grouped + rank-score ordering on top.
    """
    inputs = _signal_color_inputs_by_ticker(conn, exchange=exchange)

    if exchange is None:
        ex_params: dict = {}
    else:
        ex_params = {"ex": exchange}

    sql = """
        WITH latest_mcap AS (
            SELECT DISTINCT ON (ticker_id)
                ticker_id, market_cap
            FROM daily_market_cap
            ORDER BY ticker_id, trade_date DESC
        )
        SELECT t.ticker_id, t.exchange, t.symbol, t.instrument_type,
               t.full_name,
               ii.industry  AS industry,
               lm.market_cap AS market_cap
        FROM tickers t
        LEFT JOIN instrument_industry ii ON ii.ticker_id = t.ticker_id
        LEFT JOIN latest_mcap         lm ON lm.ticker_id = t.ticker_id
        WHERE t.is_active = TRUE AND t.delisted_at IS NULL
    """
    if exchange is not None:
        sql += " AND t.exchange = :ex"
    sql += " ORDER BY t.symbol ASC"

    rows = conn.execute(text(sql), ex_params).all()

    out: list[TickerComboRow] = []
    for r in rows:
        tid = int(r.ticker_id)
        inp = inputs.get(tid)
        out.append(TickerComboRow(
            ticker_id=tid,
            exchange=r.exchange,
            symbol=r.symbol,
            instrument_type=r.instrument_type,
            full_name=r.full_name,
            has_recent_signal=inp is not None,
            current_signal=inp.current_signal if inp else None,
            current_regime=inp.current_regime if inp else None,
            previous_signal=inp.previous_signal if inp else None,
            industry=r.industry,
            market_cap=r.market_cap,
        ))
    # Python-side sort: has_recent_signal DESC, then symbol ASC.
    out.sort(key=lambda x: (not x.has_recent_signal, x.symbol))
    return out
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
               FALSE AS parked,
               m.market_cap AS market_cap
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
            market_cap=r.market_cap,
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
    industry_alpha: float | None
    industry_beta: float | None
    industry_rs_pct: float | None
    # Stock vs Industry
    stock_alpha: float | None
    stock_beta: float | None
    stock_rs_pct: float | None
    stock_r2: float | None
    # Composite for ranking
    rank_score: float | None

@dataclass(frozen=True)
class IndustryIndicators:
    """Summary indicators about the industry a ticker belongs to.

    Populates the "Overall Indicators" card on the Tickers tab. All
    fields nullable so the renderer can show "—" for tickers whose
    industry has no industry_indicator_state coverage yet.

    regime: latest display_regime for the industry (-1 / 0 / +1).
    trend:  sign of (med_ema_latest - med_ema_two_bars_back). Same
            tri-state semantics as regime so the renderer can reuse
            the bullish/bearish/neutral coloring logic.
    """
    industry_name: str | None
    regime: int | None              # -1 / 0 / +1
    trend: int | None               # -1 / 0 / +1
    latest_trade_date: date | None
    # Phase 6-3 (revised): benchmark stats indexed by window. Three
    # snapshots — SHORT (26w), MEDIUM (52w), LONG (156w) — so the
    # Tickers tab can render them in a tabbed view. Any window may be
    # None when no stats row exists yet (sub-threshold history).
    short: BenchmarkWindowSnapshot | None = None
    medium: BenchmarkWindowSnapshot | None = None
    long: BenchmarkWindowSnapshot | None = None


def industry_indicators_for_ticker(conn, ticker_id: int) -> IndustryIndicators:
    """Return regime + trend + Phase 6-3 windowed benchmark stats.

    Three SQL roundtrips (all cheap; <15ms total):
      1. industry_indicator_state — latest 3 WEEKLY rows for
         regime + trend. The table is DAILY grain, so each week is
         collapsed to its converged (last) daily row first; taking
         3 raw rows would give a 3-DAY trend, not a 3-week one.
      2. stock_benchmark_stats — latest row per window
         (SHORT/MEDIUM/LONG), INDUSTRY benchmark.
      3. industry_benchmark_stats — latest row per window for the
         industry vs NIFTY.

    The composite rank_score is computed here per-window using the
    same formula as notifications/benchmark_rerank:
        0.5 × stock_RS + 0.3 × industry_RS + 0.2 × R²
    """
    # DROPPED-TABLE-FIX: `indicator_state` was dropped in migration 0032 and
    # its `trade_date` column with it — this query raised
    # "relation indicator_state does not exist" every time the Tickers tab
    # asked for industry indicators. The replacement is
    # industry_indicator_state, which is DAILY grain (migration 0035): ~5 rows
    # per week, all sharing the week's IST-Monday bar_timestamp.
    #
    # The caller wants the latest THREE WEEKS (it takes
    # med_ema[0] - med_ema[2] as a 3-week trend). A plain
    # `ORDER BY date DESC LIMIT 3` would return three DAYS and silently turn
    # that into a 3-DAY trend. So collapse each week to its LAST daily row
    # (the converged end-of-week state) first, then take the latest 3 weeks.
    state_rows = conn.execute(text("""
        WITH weekly AS (
            SELECT DISTINCT ON (s.industry_name, s.bar_timestamp)
                   s.industry_name, s.bar_timestamp, s.date,
                   s.med_ema, s.display_regime
              FROM industry_indicator_state s
             WHERE s.timeframe = '1W'
             ORDER BY s.industry_name, s.bar_timestamp, s.date DESC
        )
        SELECT w.med_ema, w.display_regime,
               w.date AS trade_date, ii.industry
          FROM instrument_industry ii
          JOIN weekly w
            ON w.industry_name = ii.industry
         WHERE ii.ticker_id = :tid
         ORDER BY w.bar_timestamp DESC
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

    trend: int | None = None
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

    def _build_snapshot(window_label: str) -> BenchmarkWindowSnapshot | None:
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


def latest_mcap_for_ticker(
    conn: Connection, ticker_id: int,
) -> Decimal | None:
    """Return the most recent daily_market_cap value for a ticker.

    Used by the Signals tab's Bar detail readout to surface the
    ticker's current market cap alongside the regime label. Returns
    None if the ticker has no market_cap rows.
    """
    row = conn.execute(text("""
        SELECT market_cap
        FROM daily_market_cap
        WHERE ticker_id = :tid
        ORDER BY trade_date DESC
        LIMIT 1
    """), {"tid": ticker_id}).first()
    return row.market_cap if row is not None else None
