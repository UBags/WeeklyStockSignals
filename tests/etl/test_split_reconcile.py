"""split_reconcile tests: nselib factor events -> corrector, and the survey.

Pure core is exercised with the real corrector + synthetic bars; the survey
runner uses injected loaders (no DB).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from trip_ema.domain.ohlcv_corrector import PriceBar, Verdict
from trip_ema.etl.nse_corporate_actions_repo import FactorEvent
from trip_ema.etl.split_reconcile import (
    build_split_actions_and_censors,
    correct_ticker,
    reconcile_all,
    reconcile_ticker,
)

D0 = date(2015, 1, 1)


def _day(i):
    return D0 + timedelta(days=i)


def _flat(level, start, count):
    out = []
    for k in range(count):
        c = level * (1 + 0.002 * ((k % 3) - 1))
        out.append(PriceBar(_day(start + k), c * .999, c * 1.004, c * .996, c, 1000.0))
    return out


def _two_leg(pre, post, split_at, w=6):
    return _flat(pre, 0, split_at) + _flat(post, split_at, w), _day(split_at)


def _evt(ex_date, *, factor=None, action_type="split", censor=False, num=None, den=None):
    return FactorEvent(
        ex_date=ex_date, action_type=action_type, price_factor=factor,
        censor=censor, ratio_numerator=num, ratio_denominator=den, subject="x",
    )


# --------------------------------------------------------------------------- #
# build_split_actions_and_censors
# --------------------------------------------------------------------------- #

def test_same_date_scale_events_compose_multiplicatively():
    d = _day(10)
    events = [
        _evt(d, factor=0.2, action_type="split"),
        _evt(d, factor=0.5, action_type="bonus"),
    ]
    actions, censors = build_split_actions_and_censors(events)
    assert len(actions) == 1
    assert actions[0].effective_date == d
    assert actions[0].factor == pytest.approx(0.1)   # 0.2 * 0.5
    assert censors == []


def test_censor_events_become_windows():
    d = _day(20)
    actions, censors = build_split_actions_and_censors(
        [_evt(d, action_type="demerger", censor=True)], censor_pad_days=3
    )
    assert actions == []
    assert len(censors) == 1
    assert censors[0].start == d - timedelta(days=3)
    assert censors[0].end == d + timedelta(days=3)
    assert "demerger" in censors[0].reason


def test_distinct_dates_stay_separate():
    actions, _ = build_split_actions_and_censors([
        _evt(_day(5), factor=0.5), _evt(_day(20), factor=0.2),
    ])
    assert [a.effective_date for a in actions] == [_day(5), _day(20)]
    assert [round(a.factor, 3) for a in actions] == [0.5, 0.2]


# --------------------------------------------------------------------------- #
# correct_ticker — verdicts flow through the real corrector
# --------------------------------------------------------------------------- #

def test_seam_present_detected_and_corrected():
    f = 2 / 3
    bars, b = _two_leg(300.0, 300.0 * f, split_at=10)
    res = correct_ticker(bars, [_evt(b, factor=f)])
    (rep,) = res.reports
    assert rep.verdict is Verdict.SEAM_PRESENT


def test_botched_reverse_detected():
    bars, b = _two_leg(27.52, 138.71, split_at=10)   # R≈5.04, f=0.2 -> 1/f
    res = correct_ticker(bars, [_evt(b, factor=0.2)])
    (rep,) = res.reports
    assert rep.verdict is Verdict.BOTCHED


def test_adjusted_ok_when_already_continuous():
    bars, b = _two_leg(100.0, 100.0, split_at=10)
    res = correct_ticker(bars, [_evt(b, factor=0.2)])
    assert res.reports[0].verdict is Verdict.ADJUSTED_OK


def test_demerger_censor_window_merged_into_result():
    bars, b = _two_leg(100.0, 100.0, split_at=10)
    dem = _day(30)
    res = correct_ticker(
        bars, [_evt(b, factor=0.2), _evt(dem, action_type="demerger", censor=True)]
    )
    assert any("demerger" in w.reason for w in res.censored)


# --------------------------------------------------------------------------- #
# reconcile_all — the survey
# --------------------------------------------------------------------------- #

def test_reconcile_all_tallies_and_backlogs():
    # ticker 1: SEAM_PRESENT; ticker 2: clean (ADJUSTED_OK); ticker 3: no events
    seam_bars, sb = _two_leg(300.0, 200.0, split_at=10)
    ok_bars, ob = _two_leg(100.0, 100.0, split_at=10)

    tickers = [(1, "SEAMY"), (2, "CLEAN"), (3, "NOACT")]
    events = {
        1: [_evt(sb, factor=2 / 3)],
        2: [_evt(ob, factor=0.2)],
        3: [],
    }
    bars = {1: seam_bars, 2: ok_bars, 3: []}

    summary = reconcile_all(
        lambda: tickers,
        lambda tid: events[tid],
        lambda tid: bars[tid],
    )
    assert summary.tickers_scanned == 3
    assert summary.tickers_with_actions == 2      # ticker 3 skipped
    assert summary.tally.get("SEAM_PRESENT") == 1
    assert summary.tally.get("ADJUSTED_OK") == 1
    # backlog holds the seam ticker, not the clean one
    assert [v.symbol for v in summary.backlog] == ["SEAMY"]
    assert summary.backlog[0].verdict == "SEAM_PRESENT"
    assert summary.backlog[0].ticker_id == 1


def test_reconcile_ticker_is_report_only():
    bars, b = _two_leg(300.0, 200.0, split_at=10)
    reports = reconcile_ticker(bars, [_evt(b, factor=2 / 3)])
    assert all(hasattr(r, "verdict") for r in reports)
    assert len(reports) == 1


# --- regression: TickerRecord exposes exchange/symbol via .ref (not top-level) --- #

class _FakeRef:
    def __init__(self, exchange, symbol):
        self.exchange, self.symbol = exchange, symbol


class _FakeTickerRecord:
    def __init__(self, ticker_id, exchange, symbol):
        self.ticker_id = ticker_id
        self.ref = _FakeRef(exchange, symbol)
        self.is_active = True


class _FakeTickersRepo:
    def __init__(self, records):
        self._records = records

    def find_active(self):
        return self._records


def test_active_tickers_for_exchange_reads_ref():
    from trip_ema.etl.split_reconcile import _active_tickers_for_exchange
    repo = _FakeTickersRepo([
        _FakeTickerRecord(1, "NSE", "ACME"),
        _FakeTickerRecord(2, "BSE", "BETA"),
        _FakeTickerRecord(3, "NSE", "GAMMA"),
    ])
    got = _active_tickers_for_exchange(repo, "NSE")
    assert got == [(1, "ACME"), (3, "GAMMA")]   # BSE filtered out, symbol from ref
