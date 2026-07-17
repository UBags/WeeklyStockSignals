"""Daily market cap computation for trade dates within our local
instrument_shares data range.

This module produces COMPUTED rows only — joining ohlcv_bars (daily
close prices) × instrument_shares (the as-of shares-outstanding for
the trade date) into daily_market_cap.

For trade dates OLDER than our earliest instrument_shares row, the
yfinance historical mcap fetcher takes over (Phase 4A still-to-build,
pending the operator's probe of yfinance's API shape — see
scripts/probe_yfinance_hist_mcap.py).

What this module does:
  - compute_market_cap_for_date(db_engine, exchange, trade_date)
    Iterates active EQ tickers on `exchange`. For each:
      - Look up the day's close from ohlcv_bars (timeframe='1D').
      - Look up shares as-of the trade_date from instrument_shares.
      - Multiply, write a COMPUTED row.
    Tickers without a close (no bar that day) are skipped.
    Tickers without shares for the date but with shares from a
    later date are written as APPROX_TODAYS_SHARES (low-trust),
    so the row exists for downstream Phase 4B even if approximate.
    Tickers with no shares at all are skipped (no value to record).

What it deliberately does NOT do:
  - Yfinance calls. The yfinance historical mcap path is the
    fetcher module's job (still pending the probe).
  - Multi-date batching (yet). compute_market_cap_for_date does ONE
    date per call. The runner module walks the date range.
  - ETF handling. ETFs are filtered out via instrument_type='EQ'
    in the selection query — Phase 4B doesn't index ETFs.

The function returns a ComputeResult summary so the caller can log
counts; it does NOT return the rows themselves (those live in the
DB after the call).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Engine, text

from .parallel import fan_out
from ..persistence import transaction
from ..persistence.fundamentals_repo import (
    DailyMarketCapRepository,
    InstrumentSharesRepository,
)
from ..domain.market_session import (
    market_tz_name,
    range_bounds,
)

logger = logging.getLogger(__name__)


# ---------- result dataclass ---------- #


@dataclass(frozen=True, slots=True)
class ComputeResult:
    """Summary of one call to compute_market_cap_for_date.

    All counts are over the per-exchange active EQ ticker universe
    that the function attempted to compute for. `skipped_no_close`
    + `skipped_no_shares` + `succeeded_computed` +
    `succeeded_approx` = total tickers iterated.
    """
    trade_date: date
    exchange: str
    succeeded_computed: int  # rows written with source=COMPUTED
    succeeded_approx: int    # rows written with source=APPROX_TODAYS_SHARES
    skipped_no_close: int    # ticker had no bar for that day
    skipped_no_shares: int   # ticker had no shares for that day at all


# ---------- public function ---------- #


def compute_market_cap_for_date(
    db_engine: Engine,
    exchange: str,
    trade_date: date,
    *,
    allow_approx_fallback: bool = True,
    price_column: str = "close",
    backup_price_column: str = "close",
    use_current_shares_always: bool = False,
    shares_override: dict[int, tuple[int | None, str]] | None = None,
    stocks: list[int] | None = None,
) -> ComputeResult:
    """Compute and store daily_market_cap rows for all active EQ
    tickers on `exchange` for `trade_date`.

    The function is idempotent — re-running it for the same date
    overwrites the previously-stored rows (via the repo's
    upsert_snapshot ON CONFLICT logic).

    Parameters:
      db_engine, exchange, trade_date — as documented.
      allow_approx_fallback — if True (the default), tickers that
        have shares from a LATER date but not the trade_date itself
        get an APPROX_TODAYS_SHARES row. If False, those tickers
        are skipped (skipped_no_shares).

    The allow_approx_fallback default is True for production
    backfill (we'd rather have an approximate row than no row, so
    Phase 4B can build indices for the backfill period). Tests
    pass False to verify the strict-mode behavior.
    """
    # Load the universe in one query so we don't do per-ticker
    # database round-trips.
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT t.ticker_id, t.symbol
                FROM tickers t
                WHERE t.exchange = :ex
                  AND t.is_active = TRUE
                  AND t.instrument_type = 'EQ'
                  AND (t.delisted_at IS NULL OR t.delisted_at > :td)
                  AND (:stocks IS NULL
                       OR t.ticker_id = ANY(CAST(:stocks AS integer[])))
                ORDER BY t.symbol
            """),
            {"ex": exchange, "td": trade_date, "stocks": stocks},
        ).fetchall()
    universe = [(int(r[0]), r[1]) for r in rows]

    if not universe:
        logger.warning(
            "compute_market_cap_for_date: no active EQ tickers on %s — "
            "nothing to do for %s", exchange, trade_date,
        )
        return ComputeResult(
            trade_date=trade_date, exchange=exchange,
            succeeded_computed=0, succeeded_approx=0,
            skipped_no_close=0, skipped_no_shares=0,
        )

    # L7-NO-NAIVE-BINDS. The comment that stood here said this window did not
    # "tie ourselves to a specific timezone". It tied us to the worst one: the
    # SESSION's. Both bounds were NAIVE, and Postgres coerces a naive datetime to
    # timestamptz using the session TZ — which this application now pins to UTC.
    #
    # The anchor of trading date D is local midnight: D-1 18:30Z for NSE. The naive
    # window [D 00:00, D 23:59:59.999999] read as UTC does not contain it. What it
    # DOES contain is D 18:30Z — the anchor of D+1. So this query returned the NEXT
    # DAY's close, and every daily market cap was computed with lookahead.
    #
    # (The old comment was also simply wrong on the facts: daily bars were never
    # "timestamped at market close 15:30 IST". They were at 00:00Z. Being
    # "permissive" about a timestamp you have not actually looked at is how a
    # window ends up a day wide in the wrong direction.)
    #
    # Half-open, market-local, and named. Same primitive the writer uses.
    day_start, day_end = range_bounds(trade_date, trade_date, exchange)
    # Whitelist the column names — they go into SQL text, so never
    # interpolate arbitrary strings.
    _ALLOWED_PRICE_COLS = {"adj_close", "close", "open", "high", "low"}
    if price_column not in _ALLOWED_PRICE_COLS:
        raise ValueError(f"price_column {price_column!r} not allowed")
    if backup_price_column not in _ALLOWED_PRICE_COLS:
        raise ValueError(
            f"backup_price_column {backup_price_column!r} not allowed"
        )
    # COALESCE(primary, backup): market cap is actual price x shares, so
    # the split-adjusted close is primary (NOT adj_close, which is dividend-
    # back-adjusted and would understate historical mcap). Per-row.
    _price_expr = f"COALESCE(b.{price_column}, b.{backup_price_column})"
    with db_engine.connect() as conn:
        close_rows = conn.execute(
            text(
                f"""
                SELECT b.ticker_id, {_price_expr}
                FROM ohlcv_bars b
                WHERE b.timeframe = '1D'
                  AND b.bar_timestamp >= :start
                  AND b.bar_timestamp <  :end
                """
            ),
            {"start": day_start, "end": day_end},
        ).fetchall()
    closes: dict[int, float] = {
        int(r[0]): float(r[1]) for r in close_rows
    }

    # Iterate the universe; track outcomes per category.
    succeeded_computed = 0
    succeeded_approx = 0
    skipped_no_close = 0
    skipped_no_shares = 0

    if shares_override is not None:
        # Range-walk fast path: the caller pre-resolved current shares for
        # the whole universe in ONE query (see populate_market_cap_range).
        # Valid only in current-shares mode, where shares are date-
        # invariant. Tickers absent from the map have no usable shares —
        # identical to get_latest(tid) returning None.
        ticker_shares_info: dict[int, tuple[int | None, str]] = {
            tid: shares_override.get(tid, (None, ""))
            for tid, _sym in universe
        }
    else:
        with db_engine.connect() as conn:
            shares_repo = InstrumentSharesRepository(conn)

            # Pre-resolve shares per ticker. For each ticker we want:
            #   1. shares as-of trade_date (or earlier) → COMPUTED row.
            #   2. otherwise, shares from a LATER date (today's known
            #      shares) → APPROX_TODAYS_SHARES row (if allowed).
            #   3. otherwise, skip.
            ticker_shares_info: dict[int, tuple[int | None, str]] = {}
            for tid, _sym in universe:
                if use_current_shares_always:
                    # Index-build mode: always current shares, always
                    # APPROX_TODAYS_SHARES. The split-adjusted close is on
                    # a current-share basis (splits divided out), so a uniform
                    # current-share basis across all history is correct here.
                    latest = shares_repo.get_latest(tid)
                    if latest is not None and latest.shares_outstanding is not None:
                        ticker_shares_info[tid] = (
                            latest.shares_outstanding, "APPROX_TODAYS_SHARES",
                        )
                    else:
                        ticker_shares_info[tid] = (None, "")
                    continue
                row = shares_repo.get_as_of(tid, trade_date)
                if row is not None and row.shares_outstanding is not None:
                    ticker_shares_info[tid] = (row.shares_outstanding, "COMPUTED")
                    continue
                # No shares as-of trade_date. Try the absolute latest.
                latest = shares_repo.get_latest(tid)
                if (
                    latest is not None
                    and latest.shares_outstanding is not None
                    and allow_approx_fallback
                ):
                    ticker_shares_info[tid] = (
                        latest.shares_outstanding, "APPROX_TODAYS_SHARES",
                    )
                    continue
                # Nothing we can use.
                ticker_shares_info[tid] = (None, "")

    # Collect this date's rows, then write them all in ONE batched
    # upsert (executemany) rather than ~2300 single-row statements.
    rows_to_write: list[dict] = []
    for tid, _sym in universe:
        close_price = closes.get(tid)
        if close_price is None:
            skipped_no_close += 1
            continue
        shares, src = ticker_shares_info[tid]
        if shares is None:
            skipped_no_shares += 1
            continue
        rows_to_write.append({
            "ticker_id": tid,
            "trade_date": trade_date,
            "close_price": close_price,
            "shares_used": shares,
            "market_cap": close_price * shares,
            "source": src,
        })
        if src == "COMPUTED":
            succeeded_computed += 1
        else:
            succeeded_approx += 1
    if rows_to_write:
        with transaction(db_engine) as conn:
            DailyMarketCapRepository(conn).upsert_snapshot_many(
                rows_to_write,
            )

    logger.info(
        "compute_market_cap_for_date %s/%s: "
        "computed=%d approx=%d skipped_no_close=%d skipped_no_shares=%d",
        exchange, trade_date,
        succeeded_computed, succeeded_approx,
        skipped_no_close, skipped_no_shares,
    )
    return ComputeResult(
        trade_date=trade_date, exchange=exchange,
        succeeded_computed=succeeded_computed,
        succeeded_approx=succeeded_approx,
        skipped_no_close=skipped_no_close,
        skipped_no_shares=skipped_no_shares,
    )


def _trading_days_from_bars(
    db_engine: Engine, exchange: str, date_from: date, date_to: date,
) -> list[date]:
    """Distinct 1d-bar trade dates for the exchange in [date_from, date_to],
    ascending. Trading days come from ohlcv_bars (daily_market_cap is what we
    are populating, so it can't be the source)."""
    # Two faults in four lines, and both were invisible on this box:
    #   * `_dt.combine(...)` with no tzinfo produced a NAIVE datetime, which
    #     Postgres coerced to timestamptz using the session TZ;
    #   * `bar_timestamp::date` cast the anchor back to a date in that same
    #     session TZ.
    # Two implicit reads of an unchosen clock, cancelling each other out under
    # Asia/Kolkata. Both are named now, and the window is half-open so no bar can
    # be lost to a `23:59:59.999999` that is a fact about microsecond precision
    # rather than about the day.
    start, end = range_bounds(date_from, date_to, exchange)
    with db_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT (b.bar_timestamp AT TIME ZONE :tz)::date AS d
                FROM ohlcv_bars b
                JOIN tickers t ON t.ticker_id = b.ticker_id
                WHERE b.timeframe = '1D'
                  AND t.exchange = :ex
                  AND b.bar_timestamp >= :start
                  AND b.bar_timestamp <  :end
                ORDER BY d ASC
            """),
            {
                "ex": exchange, "start": start, "end": end,
                "tz": market_tz_name(exchange),
            },
        ).fetchall()
    return [r[0] for r in rows]


def populate_market_cap_range(
    db_engine: Engine,
    exchange: str,
    *,
    date_from: date,
    date_to: date,
    price_column: str = "close",
    backup_price_column: str = "close",
    use_current_shares_always: bool = True,
    progress_cb=None,
    stocks: list[int] | None = None,
) -> dict:
    """Walk every trading day in [date_from, date_to] and populate
    daily_market_cap via compute_market_cap_for_date.

    Defaults are the industry-index-build settings: adjusted price
    (fallback to raw close) × CURRENT shares, source APPROX_TODAYS_SHARES.
    Idempotent (per-date upserts). Returns aggregate counts + per-day
    summaries.
    """
    days = _trading_days_from_bars(db_engine, exchange, date_from, date_to)
    # Prefetch current shares for the whole universe ONCE. In
    # use_current_shares_always mode the shares are date-invariant, so
    # per-day per-ticker resolution was O(days x tickers) redundant
    # queries; one DISTINCT-ON query replaces them all.
    shares_override = None
    if use_current_shares_always:
        with db_engine.connect() as conn:
            _latest = InstrumentSharesRepository(
                conn,
            ).list_latest_for_exchange(exchange)
        shares_override = {
            s.ticker_id: (s.shares_outstanding, "APPROX_TODAYS_SHARES")
            for s in _latest
            if s.shares_outstanding is not None
        }
    # Compute each trading day in parallel — days are independent
    # (disjoint trade_date rows, no write contention), and each worker
    # opens its own pooled connections inside compute_market_cap_for_date.
    _results: list = []
    _lock = threading.Lock()

    def _compute_one_day(d):
        res = compute_market_cap_for_date(
            db_engine, exchange, d,
            price_column=price_column,
            backup_price_column=backup_price_column,
            use_current_shares_always=use_current_shares_always,
            shares_override=shares_override,
            stocks=stocks,
        )
        with _lock:
            _results.append(res)

    outcome = fan_out(
        days, _compute_one_day,
        label="stage_3b_market_cap", progress_cb=progress_cb,
    )
    # The mcap phase is not per-day resumable, so any failed day must
    # fail the phase (not silently leave a gap) — a resume retries it.
    if outcome.failed:
        _names = ", ".join(str(item) for item, _ in outcome.failed[:5])
        raise RuntimeError(
            f"market_cap: {len(outcome.failed)} of {len(days)} days "
            f"failed (e.g. {_names}); phase not complete"
        )
    total = {
        "days": len(days),
        "computed": sum(r.succeeded_computed for r in _results),
        "approx": sum(r.succeeded_approx for r in _results),
        "skipped_no_close": sum(r.skipped_no_close for r in _results),
        "skipped_no_shares": sum(r.skipped_no_shares for r in _results),
    }
    logger.info(
        "populate_market_cap_range %s %s..%s: %d days, "
        "approx=%d computed=%d no_close=%d no_shares=%d",
        exchange, date_from, date_to, total["days"],
        total["approx"], total["computed"],
        total["skipped_no_close"], total["skipped_no_shares"],
    )
    return total
