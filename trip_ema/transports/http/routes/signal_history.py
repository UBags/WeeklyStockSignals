"""Chart-history payload builder — pure data assembly, no engine replay.

The OLD chart-history endpoint ran the live `TickerEngine` to recompute
indicators and regime bar-by-bar each time the GUI requested a chart.
That meant the chart's regime fill could disagree with the persisted
`indicator_state.display_regime` and with the persisted `signal_events`
— two computation paths over the same input is the textbook setup for
drift (Issue 2 from the bug-triage memo).

This module replaces that with a straight JOIN of the persisted tables:

  - `ohlcv_bars` (daily) → aggregated to weekly via `daily_to_weekly`.
  - `stock_indicator_state` → EMAs, ATR, bands, and `display_regime`.
    DAILY grain (migration 0034): ~5 rows per week, all sharing the week's
    IST-Monday `bar_timestamp`. The chart wants ONE row per weekly bar — the
    week's LAST daily row, i.e. the converged end-of-week state.
  - `current_signals` + `historical_signals` → BUY / SELL / EXIT_* markers.
    historical_signals is DAILY grain too: a ticker can fire several signals in
    one week, all sharing one bar_timestamp. Filter and order on `signal_date`.

`indicator_state` and `signal_events` (named in an earlier version of this
docstring) were dropped in migrations 0032 and 0030 respectively.

The chart now displays exactly what the persisted regime and signal
producers wrote. Two consumers, one source of truth.

Wire shape is unchanged: same `SignalHistoryPayload` /
`HistoricalBarPayload` / `HistoricalSignalEventPayload` schemas the
GUI already consumes. The `display_bg` field continues to drive the
regime fill in `chart_widget._render_regime_fill`. The legacy
`core_bg` field is populated with the same value as `display_bg` —
under the new unified regime model there's no separate core state, so
the two are identical at the wire boundary. The GUI's chart_widget
doesn't read `core_bg`, but other potential consumers (and the
HistoricalBarPayload schema itself) still expect the field to be
present.

This function is pure data assembly (`Connection` in, payload out).
The FastAPI route handler should resolve the ticker from the
(exchange, symbol, instrument) path params via the tickers repo,
then call this function with the resolved `ticker_id` + `ticker_ref`.
See the README for the integration sketch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, UTC
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

from ....domain import (
    CoreRegime,
    OHLCVBar,
    TickerRef,
    Timeframe,
)
from ....indicators.aggregation import daily_to_weekly
from ....transports.http.schemas import (
    HistoricalBarPayload,
    HistoricalSignalEventPayload,
    SignalHistoryPayload,
)
from ....domain.market_session import (
    exchange_of_conn,
    market_tz,
    market_tz_name,
)


# ============================================================
# Public entry point
# ============================================================


def build_signal_history_payload(
    conn: Connection,
    *,
    ticker_id: int,
    ticker_ref: TickerRef,
    timeframe: Timeframe,
    years_back: int = 3,
    now: datetime | None = None,
    week_anchor_tz: str | None = None,
) -> SignalHistoryPayload:
    """Assemble the chart-history payload for one (ticker, timeframe).

    Args:
        conn: SQLAlchemy Connection inside the caller's transaction.
            We don't open or close; we just read.
        ticker_id, ticker_ref: Resolved ticker identity. The caller
            (the route handler) maps (exchange, symbol, instrument)
            → these via `TickersRepository.get_by_ref` before calling.
        timeframe: Currently only Timeframe.WEEKLY is supported.
        years_back: How far back to look. Defaults to 3 — matches
            the http_client's default and the GUI's chart range.
        now: Upper bound for the lookup window. Defaults to
            `datetime.now(timezone.utc)`. Tests pass a fixed datetime.
        week_anchor_tz: Timezone the weekly buckets anchor on. NSE
            convention; pass differently for NYSE/LSE/etc.

    Returns:
        A `SignalHistoryPayload` with bars (oldest → newest),
        signal_events for the same window, `warmup_bars_count`, and
        the small bookkeeping fields the GUI expects.

    The wire shape:
      - Each `HistoricalBarPayload` has OHLC + volume from
        `ohlcv_bars` (aggregated to weekly), and EMAs/ATR/bands +
        `display_bg` from `indicator_state` if a matching row exists.
        Warmup bars (no indicator_state row yet) have all indicator
        fields None and `display_bg = "NEUTRAL"`.
      - `core_bg` equals `display_bg` at the wire boundary — see the
        module docstring.
      - `is_warmed_up` is `True` iff the bar has a matching
        indicator_state row.
      - Signal events are filtered to the same window.
    """
    # A DEFAULT of 'Asia/Kolkata' is a silent-wrong-answer generator:
    # every caller that omits it gets India. The ticker knows its own
    # exchange, so ask it.
    if week_anchor_tz is None:
        week_anchor_tz = market_tz(ticker_ref.exchange).key
    if timeframe != Timeframe.WEEKLY:
        raise NotImplementedError(
            f"Only WEEKLY is supported in this step; got {timeframe}"
        )

    if now is None:
        now = datetime.now(UTC)

    tz = ZoneInfo(week_anchor_tz)
    end_date = now.astimezone(tz).date()
    start_date = end_date - timedelta(days=years_back * 365 + 30)
    # +30 days slack so the first weekly bar at the window edge has
    # the daily bars it needs to aggregate cleanly.

    # ---- 1. Daily bars → weekly aggregation ----
    daily_bars = _load_daily_bars_for_window(
        conn, ticker_id=ticker_id, start=start_date, end=end_date,
    )
    weekly_bars = daily_to_weekly(
        daily_bars,
        include_partial_last_week=True,
        tz=week_anchor_tz,
    )

    # ---- 2. Indicator state rows for the same window ----
    indicator_rows_by_date = _load_indicator_rows_by_date(
        conn,
        ticker_id=ticker_id,
        timeframe=timeframe,
        start=start_date,
        end=end_date,
        tz=tz,
    )

    # ---- 3. Build per-bar payloads ----
    bars_out: list[HistoricalBarPayload] = []
    warmup_count = 0
    for wb in weekly_bars:
        # The weekly bar's IST-Monday date is the indicator_state key.
        wb_date_ist = wb.timestamp.astimezone(tz).date()
        ind = indicator_rows_by_date.get(wb_date_ist)

        if ind is None:
            warmup_count += 1
            bars_out.append(HistoricalBarPayload(
                bar_timestamp=wb.timestamp,
                open=wb.open, high=wb.high, low=wb.low,
                close=wb.close, volume=wb.volume,
                med_ema=None, long_ema=None, longest_ema=None,
                atr=None,
                upper_inner=None, lower_inner=None,
                upper_mid=None, lower_mid=None,
                upper_outer=None, lower_outer=None,
                core_bg="NEUTRAL",
                display_bg="NEUTRAL",
                is_warmed_up=False,
            ))
        else:
            display_bg = CoreRegime(ind.display_regime).name
            bars_out.append(HistoricalBarPayload(
                bar_timestamp=wb.timestamp,
                open=wb.open, high=wb.high, low=wb.low,
                close=wb.close, volume=wb.volume,
                med_ema=float(ind.med_ema),
                long_ema=float(ind.long_ema),
                longest_ema=float(ind.longest_ema),
                atr=float(ind.atr),
                upper_inner=float(ind.upper_inner_band),
                lower_inner=float(ind.lower_inner_band),
                upper_mid=float(ind.upper_mid_band),
                lower_mid=float(ind.lower_mid_band),
                upper_outer=float(ind.upper_outer_band),
                lower_outer=float(ind.lower_outer_band),
                # Under the unified regime model, core and display are
                # the same value at the wire boundary. The schema field
                # is kept for backward compat with any consumer that
                # still reads it; chart_widget reads display_bg only.
                core_bg=display_bg,
                display_bg=display_bg,
                is_warmed_up=True,
            ))

    # ---- 4. Signal events for the same window ----
    sig_events = _load_signal_events_for_window(
        conn,
        ticker_id=ticker_id,
        timeframe=timeframe,
        start=start_date,
        end=end_date,
    )

    return SignalHistoryPayload(
        exchange=ticker_ref.exchange,
        symbol=ticker_ref.symbol,
        instrument=ticker_ref.instrument,
        timeframe=timeframe.value,
        bars=bars_out,
        signal_events=sig_events,
        warmup_bars_count=warmup_count,
        notes="",
    )


# ============================================================
# Read-side helpers
# ============================================================


@dataclass(frozen=True)
class _IndicatorRow:
    """Subset of indicator_state we read for chart history.

    Frozen + slots to keep memory footprint small — there are
    ~150 of these per call for a 3-year weekly window.
    """
    trade_date: date
    med_ema: float
    long_ema: float
    longest_ema: float
    atr: float
    upper_inner_band: float
    lower_inner_band: float
    upper_mid_band: float
    lower_mid_band: float
    upper_outer_band: float
    lower_outer_band: float
    display_regime: int


def _load_daily_bars_for_window(
    conn: Connection,
    *,
    ticker_id: int,
    start: date,
    end: date,
) -> list[OHLCVBar]:
    """Fetch daily bars for ticker_id in [start, end], chronological.

    Mirrors `signals.generate_and_backfill_signals._load_daily_bars`.
    Duplicated rather than imported because routes/ shouldn't reach
    into signals/'s private API; both are tiny and the SQL is the
    same shape.
    """
    sql = text("""
        SELECT bar_timestamp, open, high, low, close, volume
          FROM ohlcv_bars
         WHERE ticker_id = :tid
           AND timeframe = '1D'
           AND (bar_timestamp AT TIME ZONE :tz)::date BETWEEN :start AND :end
      ORDER BY bar_timestamp
    """)
    rows = conn.execute(sql, {
        "tid": ticker_id, "start": start, "end": end,
        "tz": market_tz_name(exchange_of_conn(conn)),
    }).fetchall()
    return [
        OHLCVBar(
            timestamp=r.bar_timestamp,
            open=r.open, high=r.high, low=r.low,
            close=r.close, volume=r.volume,
        )
        for r in rows
    ]


def _load_indicator_rows_by_date(
    conn: Connection,
    *,
    ticker_id: int,
    timeframe: Timeframe,
    start: date,
    end: date,
    tz: ZoneInfo,
) -> dict[date, _IndicatorRow]:
    """The CONVERGED indicator state for each weekly bar, keyed by the bar's
    IST-Monday date.

    WEEK-GRAIN-FIX: stock_indicator_state is DAILY grain (migration 0034) —
    ~5 rows per week, every one carrying the same IST-Monday `bar_timestamp`.
    This function must return ONE row per weekly bar, and it must be the week's
    LAST daily row: that is the converged end-of-week state, which is what the
    weekly bar on the chart represents.

    The previous version selected every daily row and built the dict keyed on
    bar_timestamp, so the ~5 rows of a week collapsed to one on a last-writer-
    wins basis — with NO ORDER BY, meaning the surviving row (and therefore the
    EMAs, bands and regime fill drawn for that week) was whatever Postgres
    happened to return last. Two identical requests could render different
    charts, and a completed week could be drawn with a mid-week FORMING state.

    DISTINCT ON (bar_timestamp) + ORDER BY bar_timestamp, date DESC picks the
    latest daily row per week, deterministically. For the current, still-forming
    week that is the most recent day — exactly what the partial weekly bar shows.

    The window is bounded on `date` (the daily grain) rather than on
    bar_timestamp::date (the week anchor), so a window that starts or ends
    mid-week is not silently widened to whole weeks.
    """
    sql = text("""
        SELECT DISTINCT ON (bar_timestamp)
               bar_timestamp, date,
               med_ema, long_ema, longest_ema, atr,
               upper_inner_band, lower_inner_band,
               upper_mid_band,   lower_mid_band,
               upper_outer_band, lower_outer_band,
               display_regime
          FROM stock_indicator_state
         WHERE ticker_id = :tid
           AND timeframe = :tf
           AND date BETWEEN :start AND :end
      ORDER BY bar_timestamp, date DESC
    """)
    rows = conn.execute(sql, {
        "tid": ticker_id, "tf": timeframe.value,
        "start": start, "end": end,
    }).fetchall()
    return {
        # Key = the weekly bar's IST-Monday (matches wb.timestamp in the
        # caller). trade_date = the actual trading day the state converged on,
        # which is the week's last session, NOT the Monday.
        r.bar_timestamp.astimezone(tz).date(): _IndicatorRow(
            trade_date=r.date,
            med_ema=r.med_ema,
            long_ema=r.long_ema,
            longest_ema=r.longest_ema,
            atr=r.atr,
            upper_inner_band=r.upper_inner_band,
            lower_inner_band=r.lower_inner_band,
            upper_mid_band=r.upper_mid_band,
            lower_mid_band=r.lower_mid_band,
            upper_outer_band=r.upper_outer_band,
            lower_outer_band=r.lower_outer_band,
            display_regime=r.display_regime,
        )
        for r in rows
    }


# Step 12a: V1 read path — signal_history reads current_signals + historical_signals.


def _load_signal_events_for_window(
    conn: Connection,
    *,
    ticker_id: int,
    timeframe: Timeframe,
    start: date,
    end: date,
) -> list[HistoricalSignalEventPayload]:
    """Fetch signal events for the chart-history window.

    Step 12a: V1 read path. Two separate queries — current_signals
    (at most 1 row) + historical_signals (the bulk for a years-back
    window) — composed in Python. No JOIN. No UNION.

    `event_id` on the payload is now `int | None`: current_signals
    has no surrogate event_id (its PK is ticker_id alone), so its
    contribution carries None. historical_signals rows carry their
    actual event_id. The GUI's chart_widget marker layer doesn't
    read event_id; it iterates the list left-to-right purely for
    placement.

    Result is ordered by bar_timestamp ascending — the chart's
    marker layer walks left-to-right.

    Edge case (concurrent rotation): if the rotation primitive
    fires between these two queries, the same (ticker, bar) pair
    could appear in BOTH tables briefly. The merge prefers
    current_signals (no event_id) and skips the historical
    duplicate via (bar_timestamp, computed_at) keying. This costs
    one O(n) scan; the dedup set is small (1 element).
    """
    rows: list[HistoricalSignalEventPayload] = []
    seen_keys: set[tuple[datetime, datetime]] = set()

    # ---- 1. current_signals (at most 1 row for this ticker) ----
    cur = conn.execute(text("""
        SELECT bar_timestamp, computed_at, signal_type,
               price_at_signal
          FROM current_signals
         WHERE ticker_id = :tid
           AND timeframe = :tf
           AND (bar_timestamp AT TIME ZONE :tz)::date BETWEEN :start AND :end
    """), {
        "tid": ticker_id, "tf": timeframe.value,
        "start": start, "end": end,
        "tz": market_tz_name(exchange_of_conn(conn)),
    }).first()
    if cur is not None:
        rows.append(HistoricalSignalEventPayload(
            event_id=None,
            bar_timestamp=cur.bar_timestamp,
            computed_at=cur.computed_at,
            signal=cur.signal_type,
            price_at_signal=cur.price_at_signal,
        ))
        seen_keys.add((cur.bar_timestamp, cur.computed_at))

    # ---- 2. historical_signals ----
    # WEEK-GRAIN-FIX: filter and order on signal_date (the DAILY grain, and the
    # column carrying the UNIQUE constraint), NOT bar_timestamp — which is the
    # IST-Monday week anchor shared by every day of the week.
    #
    #   * filtering on bar_timestamp::date matched by WEEK: a window starting
    #     mid-week dropped every signal in that week (its Monday precedes
    #     :start), and a window ending mid-week pulled in signals from later in
    #     that same week.
    #   * ordering on bar_timestamp alone was total only while signals could
    #     fire on one day per week. Now a ticker can fire several in one week
    #     (BUY Mon -> EXIT_FROM_BUY Wed -> BUY Thu), all sharing one anchor, so
    #     the sequence came back in arbitrary order — and the chart's marker
    #     layer walks the list left-to-right for placement.
    hist = conn.execute(text("""
        SELECT signal_id AS event_id, bar_timestamp, computed_at,
               signal_type, price_at_signal
          FROM historical_signals
         WHERE ticker_id = :tid
           AND timeframe = :tf
           AND signal_date BETWEEN :start AND :end
      ORDER BY signal_date ASC
    """), {
        "tid": ticker_id, "tf": timeframe.value,
        "start": start, "end": end,
    }).fetchall()
    for r in hist:
        key = (r.bar_timestamp, r.computed_at)
        if key in seen_keys:
            continue
        rows.append(HistoricalSignalEventPayload(
            event_id=int(r.event_id),
            bar_timestamp=r.bar_timestamp,
            computed_at=r.computed_at,
            signal=r.signal_type,
            price_at_signal=r.price_at_signal,
        ))

    # Final order across the merged set. WEEK-GRAIN-FIX: bar_timestamp alone is
    # NOT a total order any more — several signals in one week share it — and
    # Python's sort is stable, so it would preserve whatever arbitrary order the
    # rows arrived in. computed_at is the signal day at 16:30 IST, so it breaks
    # the tie in true chronological order.
    rows.sort(key=lambda x: (x.bar_timestamp, x.computed_at))
    return rows
