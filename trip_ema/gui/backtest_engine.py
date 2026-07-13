"""Long-only backtest engine for the Triple-EMA Pine-port strategy.

A pure function over a chronologically-ordered list of bar dicts
(same shape the SignalsView gets from /v1/signals/.../history).
Returns a `BacktestResult` containing:
  - completed trades with entry/exit, P/L, return %, and max
    adverse excursion per trade
  - the currently-open trade (if any) at the end of the input
  - summary stats: win rate, total return (compounded), worst MAE,
    CAGR

Locked-in rules (from the Phase B Q&A round):
  Direction:   Long-only. No mirror-short leg.
  Entry trigger: the bar at which display_regime transitions to
                 BULLISH (+1). Pre-warmup bars (is_warmed_up=False)
                 can't trigger; the transition tracker resets across
                 a warmup boundary.
  Entry price:  max(med_ema, long_ema, longest_ema) of the entry bar.
  Exit trigger: first subsequent bar whose display_regime != BULLISH.
  Exit price:   that bar's close.
  Position size: capital_per_trade (default ₹100,000) deployed
                 fresh on each trade entry — fractional shares,
                 no rounding.
  MAE:           per-trade max adverse excursion. Tracked as min(low)
                 during the open trade, expressed as a pct of
                 entry_price. The user spec calls this 'max drawdown'
                 — by 'only during open trades' they mean MAE, not
                 the full-equity-curve drawdown. Worst across trades
                 is reported as 'worst MAE' in the summary.
  CAGR:         compounded over the first-entry → last-exit span,
                starting from `capital_per_trade`. Each closed trade
                multiplies it by (1 + return_pct/100).

Open trade at end of input:
  Returned as a SEPARATE field (`open_trade`), NOT included in
  summary stats. Mark-to-market against the last bar's close. The
  GUI shows it as a footer panel below the closed-trades table.

This module is Qt-free and DB-free so it tests under `gui_pure/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, UTC


# Approximate days per year — used for CAGR's "years" denominator
# and for the rolling-range slicer below. 365.25 averages over the
# 4-year leap cycle and is the conventional choice for financial
# annualization.
_DAYS_PER_YEAR = 365.25


@dataclass(frozen=True, slots=True)
class Trade:
    """A completed long trade."""
    entry_bar_index: int      # zero-based index into the input bars list
    entry_date: datetime
    entry_price: float        # max(EMAs) of the entry bar
    exit_bar_index: int
    exit_date: datetime
    exit_price: float         # close of the exit bar
    n_shares: float           # fractional, capital_per_trade / entry_price
    pnl_inr: float            # n_shares * (exit_price - entry_price)
    return_pct: float         # (exit_price - entry_price) / entry_price * 100
    max_adverse_excursion_pct: float
    """Largest (most negative) drawdown observed during the trade,
    in pct of entry_price. Computed as
    (min_low_during_trade - entry_price) / entry_price * 100.
    Always ≤ 0; equal to 0 only if no bar's low was below entry_price
    (rare — usually the entry bar itself dipped below entry intrabar)."""


@dataclass(frozen=True, slots=True)
class OpenTrade:
    """A trade still in progress at the end of the input. Returned
    separately from the closed `trades` list and NOT counted in
    summary stats. Marked-to-market against the last bar's close."""
    entry_bar_index: int
    entry_date: datetime
    entry_price: float
    n_shares: float
    bars_held: int
    last_bar_close: float
    unrealized_pnl_inr: float
    unrealized_return_pct: float
    current_mae_pct: float


@dataclass(frozen=True, slots=True)
class BacktestSummary:
    """Headline metrics over completed trades only.

    `total_return_pct` is the compounded return assuming starting
    capital == `capital_per_trade`, with each trade re-deploying
    that capital and growing it by (1 + return_pct/100). This
    matches 'if I had run this strategy with ₹100k starting capital'
    intuition; it does NOT match 'fresh ₹100k on every trade,
    sum the ₹ profits' — that interpretation is also available via
    `total_pnl_inr` if the consumer wants it.

    `worst_mae_pct` is the most-negative MAE across all closed
    trades — the answer to 'how deep did I ever go underwater?'.

    `cagr_pct` is annualized over first_entry → last_exit. Returns
    0.0 when there are 0 or 1 trades, or when duration < 1 day
    (insufficient time span to annualize meaningfully).
    """
    n_trades: int
    n_wins: int
    n_losses: int
    win_rate_pct: float
    starting_capital_inr: float
    final_capital_inr: float
    total_pnl_inr: float
    total_return_pct: float
    worst_mae_pct: float
    cagr_pct: float
    first_entry_date: datetime | None
    last_exit_date: datetime | None
    duration_days: int


@dataclass(frozen=True, slots=True)
class BacktestResult:
    trades: list[Trade]
    open_trade: OpenTrade | None
    summary: BacktestSummary
    bars_evaluated: int        # bars iterated past warmup
    bars_skipped_warmup: int
    parameters: dict[str, str]


def run_long_only_backtest(
    bars: list[dict],
    *,
    capital_per_trade: float = 100_000.0,
) -> BacktestResult:
    """Run the long-only Triple-EMA-regime backtest on a list of bars.

    Args:
        bars: chronologically-ordered list of bar dicts. Each must
            carry at minimum: `bar_timestamp` (datetime or ISO str),
            `low`, `close`, `display_bg` (str: BULLISH/BEARISH/NEUTRAL),
            `is_warmed_up` (bool), `med_ema`, `long_ema`, `longest_ema`.
            Bars missing optional fields are handled defensively (no
            trade is opened on a bar with a None EMA).
        capital_per_trade: ₹ amount deployed per trade. Default
            ₹100,000 per the user spec.

    Returns:
        BacktestResult with closed trades, open trade (if any), and
        summary stats. Empty bars list returns a zeroed summary.

    Raises:
        ValueError on non-positive capital_per_trade.
    """
    if capital_per_trade <= 0:
        raise ValueError("capital_per_trade must be > 0")

    in_trade = False
    entry_bar_idx = -1
    entry_date: datetime | None = None
    entry_price = 0.0
    n_shares = 0.0
    min_low_in_trade = float("inf")
    prev_regime: str | None = None

    trades: list[Trade] = []
    bars_evaluated = 0
    bars_skipped_warmup = 0

    for i, bar in enumerate(bars):
        if not bar.get("is_warmed_up", False):
            bars_skipped_warmup += 1
            # Reset transition tracker — a warmup-boundary bar should
            # NOT count as a 'transition from non-BULLISH' that triggers
            # an entry on the first post-warmup BULLISH bar. The first
            # post-warmup bar starts with prev_regime=None, so it can
            # only enter if it's a fresh BULLISH after a non-BULLISH
            # post-warmup bar — which by construction requires at
            # least one post-warmup non-BULLISH bar first.
            prev_regime = None
            continue

        bars_evaluated += 1
        regime = bar.get("display_bg") or "NEUTRAL"
        bar_dt = _parse_bar_dt(bar)

        if not in_trade:
            if regime == "BULLISH" and prev_regime is not None and prev_regime != "BULLISH":
                # Transition into BULLISH from a known non-BULLISH bar.
                med = bar.get("med_ema")
                long_ = bar.get("long_ema")
                longest = bar.get("longest_ema")
                if None in (med, long_, longest):
                    # Defensive: post-warmup bars should have EMAs, but
                    # don't crash if a payload is malformed — just skip
                    # entry on this bar and keep iterating.
                    prev_regime = regime
                    continue
                price = max(med, long_, longest)
                if price <= 0:
                    prev_regime = regime
                    continue
                in_trade = True
                entry_bar_idx = i
                entry_date = bar_dt
                entry_price = price
                n_shares = capital_per_trade / entry_price
                # Initialize MAE tracking with the entry bar's own low —
                # the regime change happens at the bar's close (or its
                # implied state), so we count this bar's drawdown.
                min_low_in_trade = bar.get("low", entry_price)
        else:
            # Update MAE every in-trade bar
            low = bar.get("low", float("inf"))
            if low < min_low_in_trade:
                min_low_in_trade = low

            if regime != "BULLISH":
                # Exit at this bar's close
                exit_price = bar.get("close", 0.0)
                pnl = n_shares * (exit_price - entry_price)
                ret_pct = (exit_price - entry_price) / entry_price * 100
                mae_pct = (min_low_in_trade - entry_price) / entry_price * 100
                # Defensive: MAE must be ≤ 0 by construction; clamp
                # tiny positive values that might come from float dust
                # to 0.0 so the summary doesn't show '+0.0%' as worst-MAE.
                if mae_pct > 0:
                    mae_pct = 0.0
                trades.append(Trade(
                    entry_bar_index=entry_bar_idx,
                    entry_date=entry_date,  # type: ignore[arg-type]
                    entry_price=entry_price,
                    exit_bar_index=i,
                    exit_date=bar_dt,
                    exit_price=exit_price,
                    n_shares=n_shares,
                    pnl_inr=pnl,
                    return_pct=ret_pct,
                    max_adverse_excursion_pct=mae_pct,
                ))
                in_trade = False

        prev_regime = regime

    open_trade: OpenTrade | None = None
    if in_trade and bars:
        last_bar = bars[-1]
        last_close = last_bar.get("close", entry_price)
        unrealized_pnl = n_shares * (last_close - entry_price)
        unrealized_ret_pct = (last_close - entry_price) / entry_price * 100
        current_mae_pct = (min_low_in_trade - entry_price) / entry_price * 100
        if current_mae_pct > 0:
            current_mae_pct = 0.0
        open_trade = OpenTrade(
            entry_bar_index=entry_bar_idx,
            entry_date=entry_date,  # type: ignore[arg-type]
            entry_price=entry_price,
            n_shares=n_shares,
            bars_held=len(bars) - 1 - entry_bar_idx,
            last_bar_close=last_close,
            unrealized_pnl_inr=unrealized_pnl,
            unrealized_return_pct=unrealized_ret_pct,
            current_mae_pct=current_mae_pct,
        )

    summary = _compute_summary(trades, capital_per_trade)

    return BacktestResult(
        trades=trades,
        open_trade=open_trade,
        summary=summary,
        bars_evaluated=bars_evaluated,
        bars_skipped_warmup=bars_skipped_warmup,
        parameters={
            "capital_per_trade":
                f"₹{capital_per_trade:,.0f}",
            "strategy":
                "Long-only, Triple-EMA regime",
            "entry":
                "max(med_ema, long_ema, longest_ema) when bg → BULLISH",
            "exit":
                "close when bg → not BULLISH",
        },
    )


def _compute_summary(
    trades: list[Trade], starting_capital: float,
) -> BacktestSummary:
    if not trades:
        return BacktestSummary(
            n_trades=0, n_wins=0, n_losses=0, win_rate_pct=0.0,
            starting_capital_inr=starting_capital,
            final_capital_inr=starting_capital,
            total_pnl_inr=0.0,
            total_return_pct=0.0,
            worst_mae_pct=0.0,
            cagr_pct=0.0,
            first_entry_date=None,
            last_exit_date=None,
            duration_days=0,
        )

    n_wins = sum(1 for t in trades if t.return_pct > 0)
    n_losses = sum(1 for t in trades if t.return_pct < 0)
    # Trades with return_pct == 0 (extremely unlikely on float prices)
    # count as neither — win_rate is fraction of decisive trades.
    n_decisive = n_wins + n_losses
    win_rate = (n_wins / n_decisive * 100) if n_decisive > 0 else 0.0

    # Compound: each trade multiplies capital by (1 + return_pct/100).
    # This matches the 'if I had run this strategy with ₹100k starting
    # capital' interpretation. A non-compounded interpretation
    # ('fresh ₹100k on every trade') would sum trade.pnl_inr instead;
    # we expose that as `total_pnl_inr` for completeness.
    capital = starting_capital
    for t in trades:
        capital *= (1 + t.return_pct / 100)

    total_pnl = sum(t.pnl_inr for t in trades)
    total_return_pct = (capital - starting_capital) / starting_capital * 100
    worst_mae = min(t.max_adverse_excursion_pct for t in trades)

    first_entry = trades[0].entry_date
    last_exit = trades[-1].exit_date
    duration_days = max(0, (last_exit - first_entry).days)

    if duration_days >= 1 and capital > 0 and starting_capital > 0:
        years = duration_days / _DAYS_PER_YEAR
        # capital/starting_capital may be < 0 in catastrophic loss
        # cases (shouldn't on long-only without leverage, but defensive
        # against negative arithmetic). Annualize only when positive.
        ratio = capital / starting_capital
        if ratio > 0:
            cagr = (ratio ** (1 / years) - 1) * 100
        else:
            cagr = -100.0
    else:
        # < 1 day span — annualization isn't meaningful. Report 0.
        cagr = 0.0

    return BacktestSummary(
        n_trades=len(trades),
        n_wins=n_wins,
        n_losses=n_losses,
        win_rate_pct=win_rate,
        starting_capital_inr=starting_capital,
        final_capital_inr=capital,
        total_pnl_inr=total_pnl,
        total_return_pct=total_return_pct,
        worst_mae_pct=worst_mae,
        cagr_pct=cagr,
        first_entry_date=first_entry,
        last_exit_date=last_exit,
        duration_days=duration_days,
    )


def filter_bars_to_range(
    bars: list[dict],
    *,
    years: int | None,
    reference_dt: datetime | None = None,
) -> list[dict]:
    """Slice bars to those with `bar_timestamp >= reference - years`.

    Used by the GUI's backtest handler to apply a rolling range
    filter without re-fetching. `years=None` returns the input
    unchanged (the 'All time' choice from the dropdown). The
    reference date defaults to `now()` UTC.

    Uses 365.25 days/year, so 3y = 1095.75 days back. Slightly
    different from a calendar-3-years subtraction but immune to
    leap-day / Feb-29 edge cases.

    Timezone handling: if a bar's timestamp is naive and the
    reference is tz-aware (or vice versa), we coerce to match.
    The bar_timestamps in this project are tz-aware UTC, so
    coercion is normally a no-op.
    """
    if years is None:
        return list(bars)
    if reference_dt is None:
        reference_dt = datetime.now(UTC)
    cutoff = reference_dt - timedelta(days=_DAYS_PER_YEAR * years)
    out: list[dict] = []
    for bar in bars:
        bar_dt = _parse_bar_dt(bar)
        if bar_dt >= cutoff:
            out.append(bar)
    return out


def _parse_bar_dt(bar: dict) -> datetime:
    """Parse a bar's `bar_timestamp` field. Accepts either a datetime
    (when called from in-process code with cached payloads) or an ISO
    string (when called after a JSON deserialization)."""
    ts = bar["bar_timestamp"]
    if not isinstance(ts, datetime):
        ts = datetime.fromisoformat(ts)
    if ts.tzinfo is None:
        # The old code grafted the cutoff's tzinfo onto a naive bar and vice
        # versa — "prefer the more-specific one", which invents an instant out of
        # a value that names none. Whatever zone happened to be on the other
        # operand became the answer. Every producer of this payload (the HTTP
        # routes) emits an aware timestamp; a naive one means something upstream
        # dropped the zone, and silently backfilling a guess is how a backtest
        # ends up a week out without anyone noticing.
        raise ValueError(
            f"bar_timestamp is naive ({ts!r}). A naive datetime names no instant; "
            f"the producer must supply the zone."
        )
    return ts