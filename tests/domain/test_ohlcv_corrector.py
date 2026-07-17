"""Corrector unit tests — the §6.1 seam-correction pipeline.

Cases are the real dirty-data signatures from the design doc:
HTMEDIA (reverse BOTCHED), DABUR (SEAM_PRESENT), SUZLON (real crash →
censor), plus ADJUSTED_OK, multi-action composition, small-f ambiguity,
and NO_BARS. Every corrected seam must land at ratio ≈ 1.
"""

from __future__ import annotations

from datetime import date, timedelta
from statistics import median

import pytest

from trip_ema.domain.ohlcv_corrector import (
    CorrectionConfig,
    PriceBar,
    SplitAction,
    Verdict,
    reconcile_and_correct,
)


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #

D0 = date(2007, 1, 1)


def _day(i: int) -> date:
    return D0 + timedelta(days=i)


def _flat_series(level: float, start: int, count: int) -> list[PriceBar]:
    """`count` quiet bars at ~`level`, one per day starting at index `start`."""
    out = []
    for k in range(count):
        i = start + k
        close = level * (1.0 + 0.002 * ((k % 3) - 1))  # tiny wobble, well under tol
        out.append(
            PriceBar(
                day=_day(i),
                open=close * 0.999,
                high=close * 1.004,
                low=close * 0.996,
                close=close,
                volume=1000.0 + k,
            )
        )
    return out


def _two_leg(pre_level: float, post_level: float, split_at: int, w: int = 6):
    """A pre-leg at `pre_level` then a post-leg at `post_level`, the scale
    changing at bar index `split_at`. Returns (bars, boundary_day)."""
    pre = _flat_series(pre_level, 0, split_at)
    post = _flat_series(post_level, split_at, w)
    return pre + post, _day(split_at)


def _seam_ratio(bars, b_day, w=3):
    days = [b.day for b in bars]
    pos = next(i for i, d in enumerate(days) if d >= b_day)
    before = [b.close for b in bars[pos - w:pos]]
    after = [b.close for b in bars[pos + 1:pos + 1 + w]]
    return median(after) / median(before)


# --------------------------------------------------------------------------- #
# ADJUSTED_OK
# --------------------------------------------------------------------------- #

def test_adjusted_ok_is_noop():
    # yfinance already scaled the pre-leg: continuous series, R ≈ 1.
    bars, b = _two_leg(100.0, 100.0, split_at=10)
    act = SplitAction(effective_date=b, factor=0.2, kind="split", marker_date=b)
    res = reconcile_and_correct(bars, [act])
    (rep,) = res.reports
    assert rep.verdict is Verdict.ADJUSTED_OK
    assert res.cumulative_multiplier == 1.0
    # bars untouched
    assert [round(x.close, 6) for x in res.bars] == [round(x.close, 6) for x in bars]
    assert res.censored == ()


# --------------------------------------------------------------------------- #
# SEAM_PRESENT — DABUR 2007 3:2, f=0.667, R≈f → pre×f
# --------------------------------------------------------------------------- #

def test_seam_present_dabur_splices_by_f():
    f = 2.0 / 3.0  # 0.667
    # yfinance skipped it: pre-leg still at OLD (high) scale, post at NEW.
    # R = post/pre = f.
    pre_level = 300.0
    post_level = pre_level * f            # 200.0
    bars, b = _two_leg(pre_level, post_level, split_at=10)
    r_before = _seam_ratio(bars, b)
    assert abs(r_before - f) < 0.02       # seam sits at f

    act = SplitAction(effective_date=b, factor=f, kind="split", marker_date=b)
    res = reconcile_and_correct(bars, [act])
    (rep,) = res.reports
    assert rep.verdict is Verdict.SEAM_PRESENT
    assert rep.snapped_s == pytest.approx(f)
    # seam now continuous
    assert _seam_ratio(res.bars, b) == pytest.approx(1.0, abs=0.02)
    assert res.cumulative_multiplier == pytest.approx(f)


# --------------------------------------------------------------------------- #
# BOTCHED reverse seam — HTMEDIA 5:1, f=0.2, R≈1/f=5 → pre×5
# --------------------------------------------------------------------------- #

def test_botched_reverse_htmedia_splices_by_inv_f():
    f = 0.2                               # 5:1 split
    # Reverse-adjusted: pre-leg pushed DOWN to 27.52, post-leg at 138.71.
    # R = post/pre ≈ 5.04 ≈ 1/f.
    pre_level = 27.52
    post_level = 138.71
    bars, b = _two_leg(pre_level, post_level, split_at=10)
    r_before = _seam_ratio(bars, b)
    assert abs(r_before - (1.0 / f)) / (1.0 / f) < 0.02

    act = SplitAction(effective_date=b, factor=f, kind="split", marker_date=b)
    res = reconcile_and_correct(bars, [act])
    (rep,) = res.reports
    assert rep.verdict is Verdict.BOTCHED
    assert rep.snapped_s == pytest.approx(1.0 / f)   # exactly 5.0, not R=5.04
    # pre-leg 27.52 × 5.0 = 137.6 ≈ 138.71 post → continuous
    assert _seam_ratio(res.bars, b) == pytest.approx(1.0, abs=0.03)


def test_snapped_magnitude_is_exact_not_measured():
    # The applied factor must be the exact 1/f, NOT the noisy measured R.
    f = 0.2
    bars, b = _two_leg(27.52, 138.71, split_at=10)
    act = SplitAction(effective_date=b, factor=f, marker_date=b)
    res = reconcile_and_correct(bars, [act])
    (rep,) = res.reports
    assert rep.snapped_s == 5.0            # exactly, independent of R≈5.04
    assert rep.measured_r != rep.snapped_s


# --------------------------------------------------------------------------- #
# UNEXPLAINED real move — SUZLON 2008 crash → censor, NOT spliced
# --------------------------------------------------------------------------- #

def test_suzlon_real_crash_is_censored_not_spliced():
    # A ~55% drop that matches no split signature. There is a nselib split
    # on record near the date, but the step is a real crash, not a scale error.
    f = 0.5                                # e.g. an alleged 1:1 bonus nearby
    pre_level = 100.0
    post_level = 40.0                      # R=0.40 — clear of 1, f(0.5), 1/f(2), f²(.25), 1/f²(4)
    bars, b = _two_leg(pre_level, post_level, split_at=10)
    act = SplitAction(effective_date=b, factor=f, kind="bonus", marker_date=b)
    res = reconcile_and_correct(bars, [act])
    (rep,) = res.reports
    assert rep.verdict is Verdict.UNEXPLAINED
    # bars completely unchanged — the crash is real, we do not invent a price
    assert [round(x.close, 6) for x in res.bars] == [round(x.close, 6) for x in bars]
    # and the crossing window is flagged for downstream walk invalidation
    assert len(res.censored) == 1
    assert res.censored[0].start <= b <= res.censored[0].end


# --------------------------------------------------------------------------- #
# Multi-action composition — newest→oldest, multipliers compound
# --------------------------------------------------------------------------- #

def test_multi_action_composition():
    # Two SEAM_PRESENT splits. Oldest leg at the oldest (highest) scale.
    # Timeline: [leg A]  --split1(f1)-->  [leg B]  --split2(f2)-->  [leg C]
    f1 = 0.5     # 1:1
    f2 = 0.2     # 5:1
    leg_c = 50.0
    leg_b = leg_c / f2      # 250.0  (pre-split2 at its old scale)
    leg_a = leg_b / f1      # 500.0  (pre-split1 at its old scale)

    a = _flat_series(leg_a, 0, 8)
    b_leg = _flat_series(leg_b, 8, 8)
    c_leg = _flat_series(leg_c, 16, 8)
    bars = a + b_leg + c_leg
    b1 = _day(8)            # split1 boundary (A→B)
    b2 = _day(16)           # split2 boundary (B→C)

    acts = [
        SplitAction(effective_date=b1, factor=f1, marker_date=b1),
        SplitAction(effective_date=b2, factor=f2, marker_date=b2),
    ]
    res = reconcile_and_correct(bars, acts)

    verdicts = {r.effective_date: r.verdict for r in res.reports}
    assert verdicts[b1] is Verdict.SEAM_PRESENT
    assert verdicts[b2] is Verdict.SEAM_PRESENT
    # both seams continuous after correction
    assert _seam_ratio(res.bars, b1) == pytest.approx(1.0, abs=0.03)
    assert _seam_ratio(res.bars, b2) == pytest.approx(1.0, abs=0.03)
    # earliest leg moved by f1*f2 relative to input
    assert res.cumulative_multiplier == pytest.approx(f1 * f2)
    first_in = bars[0].close
    first_out = res.bars[0].close
    assert first_out / first_in == pytest.approx(f1 * f2, rel=0.01)


# --------------------------------------------------------------------------- #
# Small-f ambiguity — a real ~18% move impersonating a small bonus
# --------------------------------------------------------------------------- #

def test_small_f_correction_routes_to_review_not_applied():
    # 1:5 bonus ⇒ f=0.833, inside the small-f band (0.7, 1.4). Even a seam
    # that matches f cleanly is NOT auto-spliced — a genuine ~17% move can
    # impersonate the factor, so it goes to manual review, censored meanwhile.
    f = 5.0 / 6.0                          # 0.8333
    pre_level = 100.0
    post_level = pre_level * f             # clean f-match, R ≈ 0.833
    bars, b = _two_leg(pre_level, post_level, split_at=10)
    act = SplitAction(effective_date=b, factor=f, kind="bonus", marker_date=b)
    res = reconcile_and_correct(bars, [act])
    (rep,) = res.reports
    assert rep.verdict is Verdict.AMBIGUOUS_REVIEW
    # not corrected while under review — prices untouched
    assert [round(x.close, 6) for x in res.bars] == [round(x.close, 6) for x in bars]
    assert res.cumulative_multiplier == 1.0
    assert len(res.censored) == 1


def test_small_f_adjusted_ok_is_still_accepted():
    # In the small-f band, a seam that matches "1" is safe to pass — accepting
    # it modifies no price. Only price-MODIFYING small-f matches go to review.
    f = 5.0 / 6.0
    bars, b = _two_leg(100.0, 100.0, split_at=10)   # already continuous, R≈1
    act = SplitAction(effective_date=b, factor=f, kind="bonus", marker_date=b)
    res = reconcile_and_correct(bars, [act])
    assert res.reports[0].verdict is Verdict.ADJUSTED_OK


def test_large_split_botch_auto_applies_without_review():
    # Contrast to small-f: a LARGE split (5:1) has widely separated
    # signatures, so a clean 1/f seam is trustworthy and auto-corrects —
    # no manual review. (This is the HTMEDIA regime.)
    f = 0.2
    bars, b = _two_leg(27.52, 138.71, split_at=10)
    act = SplitAction(effective_date=b, factor=f, marker_date=b)
    res = reconcile_and_correct(bars, [act])
    assert res.reports[0].verdict is Verdict.BOTCHED       # not AMBIGUOUS_REVIEW


# --------------------------------------------------------------------------- #
# NO_BARS — action predates stored history / too few bars to measure
# --------------------------------------------------------------------------- #

def test_no_bars_before_boundary():
    bars = _flat_series(100.0, 0, 8)       # boundary sits at the very start
    b = _day(0)
    act = SplitAction(effective_date=b, factor=0.2, marker_date=b)
    res = reconcile_and_correct(bars, [act])
    (rep,) = res.reports
    assert rep.verdict is Verdict.NO_BARS
    assert res.cumulative_multiplier == 1.0


def test_action_after_history_end():
    bars = _flat_series(100.0, 0, 8)
    b = _day(500)                          # far past the last stored bar
    act = SplitAction(effective_date=b, factor=0.2, marker_date=b)
    res = reconcile_and_correct(bars, [act])
    (rep,) = res.reports
    assert rep.verdict is Verdict.NO_BARS


# --------------------------------------------------------------------------- #
# purity / idempotency
# --------------------------------------------------------------------------- #

def test_input_not_mutated():
    bars, b = _two_leg(27.52, 138.71, split_at=10)
    snapshot = [(x.day, x.close) for x in bars]
    act = SplitAction(effective_date=b, factor=0.2, marker_date=b)
    reconcile_and_correct(bars, [act])
    assert [(x.day, x.close) for x in bars] == snapshot


def test_second_pass_on_corrected_series_is_noop():
    # Re-running the corrector on an already-corrected series must not
    # double-apply: the seam is now ≈1 → ADJUSTED_OK.
    bars, b = _two_leg(27.52, 138.71, split_at=10)
    act = SplitAction(effective_date=b, factor=0.2, marker_date=b)
    once = reconcile_and_correct(bars, [act])
    twice = reconcile_and_correct(list(once.bars), [act])
    assert twice.reports[0].verdict is Verdict.ADJUSTED_OK
    assert twice.cumulative_multiplier == 1.0
    assert [round(x.close, 6) for x in twice.bars] == [
        round(x.close, 6) for x in once.bars
    ]


def test_marker_date_falls_back_to_effective_when_absent():
    f = 2.0 / 3.0
    bars, b = _two_leg(300.0, 300.0 * f, split_at=10)
    # no marker_date → uses effective_date
    act = SplitAction(effective_date=b, factor=f)
    res = reconcile_and_correct(bars, [act])
    assert res.reports[0].verdict is Verdict.SEAM_PRESENT
    assert res.reports[0].boundary == b
