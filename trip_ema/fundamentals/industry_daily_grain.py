"""Industry daily-grain weekly-indicator computation.

Industry indices (IIV) are artificial: one `value` per trading day, no
real OHLC and no real trading volume. We feed the state-carry engine
pseudo-daily bars (O=H=L=C=value) and pin the weekly VW-EMA volume to a
CONSTANT 1.0. Because `vw1 = ema(close*volume) / ema(volume)`, a constant
volume makes the volume-weighted triple-EMA collapse exactly to a PLAIN
triple-EMA of the index series — which is the correct semantics for an
artificial index with no volume to weight by.

ATR is true-range (`synthetic_atr_for_index=False`): even though each
pseudo-DAILY bar has O=H=L=C, the forming/real WEEKLY bar's high/low span
the week's daily value range, so the weekly true range is real.

Same O(weeks+days) engine as stocks; state and enrichment come from the
SAME walk (mirror of `daily_grain.py`).

NOTE: this path deliberately does NOT reproduce the legacy weekly rows.
The legacy path fed `daily_to_weekly`'s summed day-count as volume, which
made holiday-shortened weeks weigh less. Uniform 1.0 removes that; the
industry tables are rebuilt fresh (parity with the old rows is not a goal).
"""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from ..domain import OHLCVBar
from .indicator_primitives import INDICATOR_DEFAULTS
from ..domain.market_session import market_session_for
from .state_carry_engine import compute_daily_grain_via_engine

# Artificial index: every weekly bar weighs the same in the VW-EMA.
INDUSTRY_CONSTANT_VOLUME = 1.0
# Industry compute is NSE-anchored (IST Monday weeks), matching the
# legacy `_build_industry_weekly_bars(tz="Asia/Kolkata")`.
_INDUSTRY_SESSION_EXCHANGE = "NSE"


def _iiv_to_pseudo_daily_bars(
    history: list, *, zone: ZoneInfo,
) -> list[OHLCVBar]:
    """IIV records (oldest-first; each has `.trade_date` and `.value`)
    -> pseudo-daily OHLCVBars with O=H=L=C=value, anchored at the IST
    day start. Per-bar `volume` is a placeholder (1.0); the weekly VW-EMA
    volume is pinned to a constant downstream, so it is never summed into
    a meaningful weight."""
    bars: list[OHLCVBar] = []
    for h in history:
        v = float(h.value)
        ts = datetime.combine(h.trade_date, time.min, tzinfo=zone)
        bars.append(
            OHLCVBar(timestamp=ts, open=v, high=v, low=v, close=v,
                     volume=1.0),
        )
    return bars


def _walk(history: list, *, now: datetime,
          target_days: list[date] | None):
    session = market_session_for(_INDUSTRY_SESSION_EXCHANGE)
    bars = _iiv_to_pseudo_daily_bars(history, zone=session.zone)
    return compute_daily_grain_via_engine(
        bars,
        session=session,
        now=now,
        params=INDICATOR_DEFAULTS,
        target_days=target_days,
        synthetic_atr_for_index=False,
        constant_volume=INDUSTRY_CONSTANT_VOLUME,
    )


def compute_industry_daily_grain_states(
    history: list,
    *,
    now: datetime,
    target_days: list[date] | None = None,
) -> list[dict]:
    """One state-primitive dict per emitted trading day (trade_date,
    bar_timestamp + 12 core fields), via the state-carry engine with
    constant volume=1.0."""
    state_rows, _ = _walk(history, now=now, target_days=target_days)
    return state_rows


def compute_industry_daily_grain_enrichment(
    history: list,
    *,
    now: datetime,
    target_days: list[date] | None = None,
) -> list[dict]:
    """One enrichment-primitive dict per emitted trading day, aligned
    1:1 with `compute_industry_daily_grain_states` (same walk)."""
    _, enrich_rows = _walk(history, now=now, target_days=target_days)
    return enrich_rows
