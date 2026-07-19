"""C4c — the walk attaches bands, sequences and ATRs to every row.

The tests that matter here are not "did it set the field". They are:

  * did it cut the bands against the FORMING-day EMAs, or did it quietly
    reach for the week's converged ones (a lookahead that produces complete,
    plausible, systematically flattering rows and nothing downstream can
    see it) — test_bands_are_cut_against_the_forming_day_emas;

  * does an EXIT_FROM_BUY on the same bar as a BUY carry the MIRROR flags,
    rather than the BUY's — test_exit_rows_carry_mirrored_geometry;

  * do the four rows of one day share a mutable dict — test_rows_do_not_alias.
"""
from datetime import datetime, timedelta, timezone

import pytest

from trip_ema.domain import OHLCVBar
from trip_ema.domain.market_session import market_session_for
from trip_ema.fundamentals.signal_bands import (
    ATR_PERIODS,
    POLARITY_DOWN,
    POLARITY_UP,
    band_flags,
    band_keys,
    dist_key,
)
from trip_ema.fundamentals.signal_enrichment_walk import (
    compute_ticker_signal_enrichments,
)

UTC = timezone.utc
NOW = datetime(2024, 6, 1, tzinfo=UTC)


def _week(monday, base):
    return [OHLCVBar(monday + timedelta(days=i), base + i, base + 2 + i,
                     base - 1 + i, base + 1 + i, 100 + i) for i in range(5)]


def _scenario(n_weeks=4):
    weeks = [datetime(2024, 1, 1, tzinfo=UTC) + timedelta(weeks=w)
             for w in range(n_weeks)]
    session = market_session_for("NSE")
    daily, state_by_date, conv_by_monday = [], {}, {}
    for w, m in enumerate(weeks):
        bars = _week(m, 10 + 5 * w)
        daily += bars
        for b in bars:
            d = b.timestamp.astimezone(session.zone).date()
            state_by_date[d] = (b.close - 0.5, b.close - 1.0,
                                b.close - 1.5, 1.2,
                                b.close + 2.0, b.close - 2.0, 1)
        last = bars[-1]
        conv_by_monday[m] = (last.close - 0.5, last.close - 1.0,
                             last.close - 1.5, 1.2,
                             last.close + 2.0, last.close - 2.0, 1)
    return daily, session, state_by_date, conv_by_monday, weeks


def _day(week_dt, offset, session):
    return (week_dt + timedelta(days=offset)).astimezone(session.zone).date()


def _run(signals, n_weeks=4, **over):
    daily, session, sbd, cbm, weeks = _scenario(n_weeks)
    kw = dict(
        ticker_id=42, timeframe="1W", daily_bars=daily,
        state_by_date=sbd, conv_by_monday=cbm,
        signals_by_date=signals(session, weeks),
        session=session, now=NOW, feature_spec_version="v0.5",
        industry_id=7,
    )
    kw.update(over)
    rows = compute_ticker_signal_enrichments(**kw)
    return rows, session, sbd, weeks, daily


def _close_on(daily, session, d):
    for b in daily:
        if b.timestamp.astimezone(session.zone).date() == d:
            return b.close
    raise AssertionError(f"no bar on {d}")


class TestShape:

    def test_every_row_carries_the_full_band_set(self):
        rows, session, _sbd, weeks, _daily = _run(
            lambda s, w: {_day(w[2], 2, s): ["BUY"]}
        )
        assert len(rows) == 1
        r = rows[0]
        assert set(r.bands) == set(band_keys())
        assert set(r.atrs) == set(ATR_PERIODS)
        assert r.band_polarity == POLARITY_UP
        assert r.feature_spec_version == "v0.5"

    def test_signal_price_is_the_signal_days_close(self):
        rows, session, _sbd, weeks, daily = _run(
            lambda s, w: {_day(w[2], 2, s): ["BUY"]}
        )
        d = rows[0].signal_date
        assert rows[0].signal_price == pytest.approx(
            _close_on(daily, session, d)
        )

    def test_the_atrs_are_not_in_the_features_jsonb(self):
        rows, *_ = _run(lambda s, w: {_day(w[2], 2, s): ["BUY"]})
        for n in ATR_PERIODS:
            assert f"atr{n}" not in rows[0].features
        assert "atr28" in rows[0].features      # the normaliser stays


class TestTheEmasTheBandsAreCutAgainst:
    """The lookahead this whole file exists to prevent."""

    def test_bands_are_cut_against_the_forming_day_emas(self):
        rows, session, sbd, weeks, daily = _run(
            lambda s, w: {_day(w[2], 2, s): ["BUY"]}
        )
        r = rows[0]
        med, lng, lngst = sbd[r.signal_date][0:3]

        expected = band_flags(
            price=_close_on(daily, session, r.signal_date),
            med_ema=med, long_ema=lng, longest_ema=lngst,
            atr_by_period=r.atrs, polarity=POLARITY_UP,
        )
        assert r.bands == expected, (
            "the walk did not cut the bands against state_by_date. If it "
            "reached for conv_by_monday, it used Friday's EMAs to score "
            "Tuesday's signal."
        )

    def test_the_converged_emas_would_have_given_a_different_answer(self):
        """Guards the test above from being vacuous: the two EMA sets must
        actually differ on this fixture, or the assertion proves nothing."""
        rows, session, sbd, weeks, daily = _run(
            lambda s, w: {_day(w[2], 2, s): ["BUY"]}
        )
        r = rows[0]
        friday = _day(weeks[2], 4, session)
        assert sbd[r.signal_date][0] != sbd[friday][0]

        wrong = band_flags(
            price=_close_on(daily, session, r.signal_date),
            med_ema=sbd[friday][0], long_ema=sbd[friday][1],
            longest_ema=sbd[friday][2],
            atr_by_period=r.atrs, polarity=POLARITY_UP,
        )
        assert r.bands != wrong


class TestPolarity:

    def test_exit_rows_carry_mirrored_geometry(self):
        rows, session, _sbd, weeks, _daily = _run(
            lambda s, w: {_day(w[2], 2, s): ["BUY", "EXIT_FROM_BUY"]}
        )
        by_type = {r.signal_type: r for r in rows}
        buy, exit_buy = by_type["BUY"], by_type["EXIT_FROM_BUY"]

        assert buy.band_polarity == POLARITY_UP
        assert exit_buy.band_polarity == POLARITY_DOWN

        for ema in ("med", "long", "longest"):
            for n in ATR_PERIODS:
                k = dist_key(ema, n)
                assert exit_buy.bands[k] == pytest.approx(-buy.bands[k]), k

    def test_exit_from_sell_scores_as_a_buy(self):
        rows, *_ = _run(
            lambda s, w: {_day(w[2], 2, s): ["BUY", "EXIT_FROM_SELL"]}
        )
        by_type = {r.signal_type: r for r in rows}
        assert by_type["EXIT_FROM_SELL"].band_polarity == POLARITY_UP
        assert by_type["EXIT_FROM_SELL"].bands == by_type["BUY"].bands

    def test_an_unknown_signal_type_raises(self):
        with pytest.raises(ValueError, match="unknown signal_type"):
            _run(lambda s, w: {_day(w[2], 2, s): ["HOLD"]})


class TestRowsDoNotAlias:

    def test_rows_do_not_alias(self):
        """One dict per row. Four rows sharing one `features` object is a
        bug that lies dormant until the first caller mutates one of them."""
        rows, *_ = _run(
            lambda s, w: {_day(w[2], 2, s):
                          ["BUY", "SELL", "EXIT_FROM_BUY", "EXIT_FROM_SELL"]}
        )
        assert len(rows) == 4
        feats = [r.features for r in rows]
        assert all(f == feats[0] for f in feats)          # same content
        assert len({id(f) for f in feats}) == 4           # different objects
        assert len({id(r.bands) for r in rows}) == 4
        assert len({id(r.atrs) for r in rows}) == 4


class TestSequenceNumbers:

    def test_two_buys_in_one_week_are_numbered_in_order(self):
        rows, session, _sbd, weeks, _daily = _run(
            lambda s, w: {_day(w[2], 1, s): ["BUY"],
                          _day(w[2], 3, s): ["BUY"]}
        )
        by_date = {r.signal_date: r for r in rows}
        first, second = sorted(by_date)
        assert by_date[first].seq_in_week == 1
        assert by_date[second].seq_in_week == 2
        # Both signals are in ONE week, so the run length is 1 for both. Under
        # the old fixed two-week window this asserted 2 -- which is exactly
        # the confusion the run-length counter removes: two signals in a
        # fortnight is not two consecutive weeks of signals.
        assert by_date[first].seq_in_consec_weeks == 1
        assert by_date[second].seq_in_consec_weeks == 1

    def test_the_week_counter_resets_and_the_run_length_grows(self):
        rows, session, _sbd, weeks, _daily = _run(
            lambda s, w: {_day(w[1], 2, s): ["BUY"],
                          _day(w[2], 2, s): ["BUY"]}
        )
        by_date = {r.signal_date: r for r in rows}
        wk1, wk2 = sorted(by_date)
        assert by_date[wk1].seq_in_week == 1
        assert by_date[wk2].seq_in_week == 1        # new week
        # Adjacent weeks, one signal each -> a run of 2.
        assert by_date[wk2].seq_in_consec_weeks == 2

    def test_the_churn_week_is_counted_per_type(self):
        rows, session, _sbd, weeks, _daily = _run(
            lambda s, w: {_day(w[2], 0, s): ["BUY"],
                          _day(w[2], 1, s): ["EXIT_FROM_BUY"],
                          _day(w[2], 3, s): ["BUY"]}
        )
        got = {(r.signal_date, r.signal_type): r.seq_in_week for r in rows}
        dates = sorted({d for d, _t in got})
        assert got[(dates[0], "BUY")] == 1
        assert got[(dates[1], "EXIT_FROM_BUY")] == 1
        assert got[(dates[2], "BUY")] == 2          # the second attempt
