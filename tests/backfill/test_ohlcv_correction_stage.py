"""Correction-stage tests: which bars get rewritten, and the stage over
injected fakes (no test-side patching — collaborators are injected)."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trip_ema.backfill.ohlcv_correction_stage import (
    correct_ohlcv_stage,
    corrected_bar_writes,
)
from trip_ema.domain.ohlcv import OHLCVBar
from trip_ema.domain.ohlcv_corrector import PriceBar
from trip_ema.etl.nse_corporate_actions_repo import FactorEvent
from trip_ema.etl.split_reconcile import correct_ticker

IST = ZoneInfo("Asia/Kolkata")
D0 = datetime(2015, 1, 1, 0, 0, tzinfo=IST)


def _bar(i, level):
    return OHLCVBar(
        timestamp=D0 + timedelta(days=i),
        open=level * .999, high=level * 1.004, low=level * .996,
        close=level, volume=1000.0,
    )


def _two_leg_bars(pre, post, split_at, w=6):
    bars = [_bar(i, pre) for i in range(split_at)]
    bars += [_bar(split_at + k, post) for k in range(w)]
    return bars


def _evt(day_index, factor):
    return FactorEvent(
        ex_date=(D0 + timedelta(days=day_index)).date(),
        action_type="split", price_factor=factor, censor=False,
        ratio_numerator=None, ratio_denominator=None, subject="x",
    )


# --------------------------------------------------------------------------- #
# corrected_bar_writes — only changed bars, mapped back to original timestamps
# --------------------------------------------------------------------------- #

def test_only_changed_bars_are_written():
    f = 2 / 3
    orig = _two_leg_bars(300.0, 300.0 * f, split_at=10)
    events = [_evt(10, f)]
    pricebars = [
        PriceBar(b.timestamp.astimezone(IST).date(), b.open, b.high, b.low, b.close, b.volume)
        for b in orig
    ]
    result = correct_ticker(pricebars, events)
    writes = corrected_bar_writes(orig, result, IST)
    assert len(writes) == 10                       # 10 pre-boundary changed
    written_ts = {w.timestamp for w in writes}
    assert written_ts == {b.timestamp for b in orig[:10]}   # original timestamps


def test_adjusted_ok_writes_nothing():
    orig = _two_leg_bars(100.0, 100.0, split_at=10)
    pricebars = [
        PriceBar(b.timestamp.astimezone(IST).date(), b.open, b.high, b.low, b.close, b.volume)
        for b in orig
    ]
    result = correct_ticker(pricebars, [_evt(10, 0.2)])
    assert corrected_bar_writes(orig, result, IST) == []


# --------------------------------------------------------------------------- #
# injected fakes for the stage (collaborators injected, not patched)
# --------------------------------------------------------------------------- #

class _FakeActionsRepo:
    def __init__(self, events_by_ticker):
        self._e = events_by_ticker

    def factor_events_for_ticker(self, ticker_id):
        return self._e.get(ticker_id, [])


class _FakeBarsRepo:
    def __init__(self, bars_by_ticker):
        self._b = bars_by_ticker
        self.upserts = []

    def fetch_history(self, ticker_id, timeframe, limit=100000):
        return list(self._b.get(ticker_id, []))

    def upsert_bar(self, ticker_id, timeframe, bar, *, adj_close=None):
        self.upserts.append((ticker_id, bar.timestamp, bar.close))
        cur = self._b[ticker_id]
        self._b[ticker_id] = [bar if b.timestamp == bar.timestamp else b for b in cur]


def _fake_transaction_factory():
    @contextmanager
    def _tx(engine):
        yield object()   # the fake repos ignore the conn
    return _tx


def _run(scoped, events, bars):
    actions = _FakeActionsRepo(events)
    brepo = _FakeBarsRepo(bars)
    summary = correct_ohlcv_stage(
        object(), exchange="NSE", scoped_tickers=scoped,
        transaction=_fake_transaction_factory(),
        make_actions_repo=lambda conn: actions,
        make_bars_repo=lambda conn: brepo,
    )
    return summary, brepo


def test_stage_corrects_seam_ticker_and_skips_clean():
    f = 2 / 3
    seam = _two_leg_bars(300.0, 300.0 * f, split_at=10)
    clean = _two_leg_bars(100.0, 100.0, split_at=10)
    summary, brepo = _run(
        [1, 2, 3],
        {1: [_evt(10, f)], 2: [_evt(10, 0.2)], 3: []},
        {1: seam, 2: clean},
    )
    assert summary.tickers_scanned == 2          # ticker 3 skipped (no events)
    assert summary.tickers_corrected == 1        # only the seam ticker
    assert summary.bars_written == 10
    assert all(tid == 1 for tid, _, _ in brepo.upserts)


def test_stage_is_idempotent():
    f = 2 / 3
    bars = {1: _two_leg_bars(300.0, 300.0 * f, split_at=10)}
    events = {1: [_evt(10, f)]}
    first, brepo = _run([1], events, bars)
    assert first.bars_written == 10
    # second pass over the now-corrected series -> ADJUSTED_OK -> no writes
    second = correct_ohlcv_stage(
        object(), exchange="NSE", scoped_tickers=[1],
        transaction=_fake_transaction_factory(),
        make_actions_repo=lambda conn: _FakeActionsRepo(events),
        make_bars_repo=lambda conn: brepo,
    )
    assert second.tickers_corrected == 0
    assert second.bars_written == 0


def test_stage_counts_censor_windows():
    bars = {1: _two_leg_bars(100.0, 100.0, split_at=10)}
    dem = FactorEvent(
        ex_date=(D0 + timedelta(days=30)).date(), action_type="demerger",
        price_factor=None, censor=True, ratio_numerator=None,
        ratio_denominator=None, subject="Scheme Of Arrangement",
    )
    summary, _ = _run([1], {1: [_evt(10, 0.2), dem]}, bars)
    assert summary.censored_windows == 1
