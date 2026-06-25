"""Reconciler.

Given a list of new RawBars from yfinance and the existing bars in the
DB, produces a write plan plus a list of divergences that need
human review.

The contract (ARCHITECTURE.md invariant 3.1 says the ETL owns truth;
invariant 3.6 says the engine accepts revisions):

  - Bars within the trailing N days (default 7) are OVERWRITTEN
    freely. yfinance corrects intraday-and-recent data routinely;
    the engine's revision-detection path picks up the change on its
    next pass.
  - Bars OLDER than N days are INSERTED-ONLY if new. If a new value
    differs from the stored value, do NOT overwrite. Instead, flag
    a 'candidate_revision' corporate_action row with status
    'pending_review'. A human runs `trip-ema-etl approve-action <id>`
    to accept the overwrite.

Pure function: no DB writes, no logging side effects, no time.now()
inside the algorithm — caller passes `now` and `existing_bars`. This
is so tests can pin both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .types import RawBar


# Tolerance for "is this old bar actually different?" comparisons.
# yfinance gives us double-precision floats; we treat values within
# 1 cent (absolute) AND 0.01% (relative) as equivalent. The two are
# combined so penny stocks don't trigger on every micro-shift and
# index-priced names don't get false-equivalence on big moves.
ABS_TOL = 0.01
REL_TOL = 0.0001


@dataclass(frozen=True, slots=True)
class BarWrite:
    """One bar to upsert into ohlcv_bars."""
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float
    volume: float
    is_overwrite: bool
    """True when overwriting an existing bar (within the rolling window)."""


@dataclass(frozen=True, slots=True)
class DivergenceFlag:
    """An old bar (outside rolling window) whose new yfinance value
    differs from the stored value. The runner creates a
    corporate_actions row with status='pending_review' for each one;
    `trip-ema-etl approve-action <id>` promotes it."""
    trading_date: date
    stored_close: float
    new_close: float
    stored_adj_close: float | None
    new_adj_close: float
    delta_pct: float
    """(new_close - stored_close) / stored_close, signed."""


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    writes: tuple[BarWrite, ...] = field(default_factory=tuple)
    divergences: tuple[DivergenceFlag, ...] = field(default_factory=tuple)
    skipped_count: int = 0
    """Bars that matched stored values exactly — no-ops."""


@dataclass(frozen=True, slots=True)
class _StoredBar:
    """Compact representation of what's already in ohlcv_bars.
    Repo layer materializes these for the reconciler."""
    trading_date: date
    open: float
    high: float
    low: float
    close: float
    adj_close: float | None
    volume: float


def reconcile(
    new_bars: tuple[RawBar, ...] | list[RawBar],
    stored_bars: tuple[_StoredBar, ...] | list[_StoredBar],
    *,
    today: date,
    rolling_window_days: int = 7,
) -> ReconcilePlan:
    """Build the write plan and the divergence list.

    Args:
      new_bars: From yfinance, sorted-or-unsorted by date. We sort.
      stored_bars: From bars_repo, the bars currently in DB for this
        (ticker, timeframe). The reconciler doesn't care about which
        ticker — that's enforced at the caller.
      today: Used to compute the rolling-window cutoff. The cutoff
        date is `today - rolling_window_days`. Bars on the cutoff
        date and AFTER are inside the window.
      rolling_window_days: How many trailing days are eligible for
        free overwrite. Default 7. Smaller = more candidate-revisions
        flagged; larger = more silent overwrites.

    Returns: a ReconcilePlan. The caller commits the writes and
    persists the divergences.
    """
    cutoff = today - timedelta(days=rolling_window_days)
    stored_by_date = {b.trading_date: b for b in stored_bars}

    writes: list[BarWrite] = []
    divergences: list[DivergenceFlag] = []
    skipped = 0

    for new in new_bars:
        existing = stored_by_date.get(new.trading_date)
        if existing is None:
            # Brand-new bar — always insert.
            writes.append(BarWrite(
                trading_date=new.trading_date,
                open=new.open, high=new.high, low=new.low, close=new.close,
                adj_close=new.adj_close, volume=new.volume,
                is_overwrite=False,
            ))
            continue

        if _bars_equivalent(new, existing):
            # Exact match — no-op. Don't bother re-writing.
            skipped += 1
            continue

        # Different.
        if new.trading_date >= cutoff:
            # Within rolling window: overwrite freely. yfinance
            # routinely revises recent bars; trust the latest value.
            writes.append(BarWrite(
                trading_date=new.trading_date,
                open=new.open, high=new.high, low=new.low, close=new.close,
                adj_close=new.adj_close, volume=new.volume,
                is_overwrite=True,
            ))
        else:
            # Old bar diverged. DO NOT overwrite silently. Flag it.
            delta = 0.0
            if existing.close != 0:
                delta = (new.close - existing.close) / existing.close
            divergences.append(DivergenceFlag(
                trading_date=new.trading_date,
                stored_close=existing.close,
                new_close=new.close,
                stored_adj_close=existing.adj_close,
                new_adj_close=new.adj_close,
                delta_pct=delta,
            ))

    return ReconcilePlan(
        writes=tuple(writes),
        divergences=tuple(divergences),
        skipped_count=skipped,
    )


# ---------- helpers ---------- #


def _bars_equivalent(new: RawBar, stored: _StoredBar) -> bool:
    """Are the new and stored bars equal within tolerance?

    Compares OHLC + volume. adj_close is checked separately: if the
    stored adj_close is None (pre-migration bars), we don't penalize
    on that field. Volume tolerance is wider because yfinance
    sometimes rounds.
    """
    if not _approx_eq(new.open, stored.open):
        return False
    if not _approx_eq(new.high, stored.high):
        return False
    if not _approx_eq(new.low, stored.low):
        return False
    if not _approx_eq(new.close, stored.close):
        return False
    # Volume — tolerance widened
    if not _approx_eq(new.volume, stored.volume, abs_tol=1.0, rel_tol=0.001):
        return False
    # adj_close: only compare if stored has one
    if stored.adj_close is not None:
        if not _approx_eq(new.adj_close, stored.adj_close):
            return False
    return True


def _approx_eq(
    a: float, b: float,
    *, abs_tol: float = ABS_TOL, rel_tol: float = REL_TOL,
) -> bool:
    """Combined absolute + relative tolerance. Two values match if
    they're within ABS_TOL OR within REL_TOL of each other (relative
    to max(|a|, |b|))."""
    if a == b:
        return True
    diff = abs(a - b)
    if diff <= abs_tol:
        return True
    biggest = max(abs(a), abs(b))
    if biggest > 0 and diff / biggest <= rel_tol:
        return True
    return False
