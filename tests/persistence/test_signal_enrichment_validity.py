"""C5b — data_is_valid (strict literal), bars_available, day_of_week.

data_is_valid is the ML gate. Strict literal, by request: every band column
non-NULL or the row is out. The tests that matter are the two edges of that
rule — a full row is valid, a row with a single NULL band column is not,
whatever the reason for the NULL.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from trip_ema.domain.signal_enrichment import SignalEnrichment
from trip_ema.fundamentals.signal_bands import (
    ATR_PERIODS, POLARITY_UP, band_flags, band_keys,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.signal_enrichment_repo import (
    SignalEnrichmentRepository,
    _VALIDITY_COLS,
)

ATRS = {10: 5.0, 15: 8.0, 20: 10.0, 30: 20.0, 40: 40.0}


def _bands(price=105.0, atrs=None):
    return band_flags(
        price=price, med_ema=100.0, long_ema=98.0, longest_ema=95.0,
        atr_by_period=ATRS if atrs is None else atrs, polarity=POLARITY_UP,
    )


def _row(**over):
    base = dict(
        ticker_id=9200, timeframe="1W", signal_date=date(2026, 7, 14),
        signal_type="BUY", features={"x": 1.0}, feature_spec_version="v0.5",
        industry_id=7, bands=_bands(), atrs=dict(ATRS),
        seq_in_week=1, seq_in_consec_weeks=1, signal_price=105.0, band_polarity="UP",
        bars_available=60, day_of_week=2,
    )
    base.update(over)
    return SignalEnrichment(**base)


def _put(db_engine, row):
    with transaction(db_engine) as conn:
        SignalEnrichmentRepository(conn).upsert_one(row)
    with db_engine.connect() as conn:
        return SignalEnrichmentRepository(conn).get_for_signal(
            row.ticker_id, row.timeframe, row.signal_date, row.signal_type,
        )


class TestSchema:

    def test_the_three_columns_exist_and_table_is_111(self, db_engine):
        with db_engine.connect() as conn:
            cols = {
                r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'signal_enrichment'"
                ))
            }
        assert {"data_is_valid", "bars_available", "day_of_week"} <= cols
        assert len(cols) == 111        # 9 + 99 + 3

    def test_data_is_valid_is_not_null_defaulting_false(self, db_engine):
        with db_engine.connect() as conn:
            r = conn.execute(text(
                "SELECT is_nullable, column_default FROM "
                "information_schema.columns WHERE table_name = "
                "'signal_enrichment' AND column_name = 'data_is_valid'"
            )).fetchone()
        assert r.is_nullable == "NO"
        assert "false" in r.column_default.lower()


class TestValidRow:

    def test_a_complete_row_is_valid(self, db_engine):
        got = _put(db_engine, _row())
        assert got.data_is_valid is True
        assert got.bars_available == 60
        assert got.day_of_week == 2

    def test_a_below_ema_row_is_still_valid(self, db_engine):
        """Price below the EMA -> real False flags, NOT NULL. Strict-literal
        validity is about NULL, not about False, so this row is VALID."""
        got = _put(db_engine, _row(ticker_id=9201, bands=_bands(price=90.0),
                                   signal_price=90.0))
        assert got.data_is_valid is True
        # and it really is an all-False row, not a null one
        with db_engine.connect() as conn:
            v = conn.execute(text(
                "SELECT band_med_atr20_1, dist_med_atr20 FROM "
                "signal_enrichment WHERE ticker_id = 9201"
            )).fetchone()
        assert v.band_med_atr20_1 is False
        assert v.dist_med_atr20 is not None


class TestInvalidRow:
    """The strict rule: one NULL band column anywhere -> not valid."""

    def test_a_warm_up_null_atr_makes_the_row_invalid(self, db_engine):
        """No atr40 yet (uncomputable pair -> None flags + None atr).
        Strict literal: the row is not valid, period."""
        atrs = {**ATRS, 40: float("nan")}
        got = _put(db_engine, _row(
            ticker_id=9202, bands=_bands(atrs=atrs), atrs=atrs,
            bars_available=20,
        ))
        assert got.data_is_valid is False

    def test_none_bands_is_invalid(self, db_engine):
        got = _put(db_engine, _row(
            ticker_id=9203, bands=None, atrs=None,
            feature_spec_version="v0.4",
        ))
        assert got.data_is_valid is False

    def test_a_single_null_distance_is_enough(self, db_engine):
        """Every ATR present, but one EMA missing -> that EMA's distances are
        NaN -> NULL. One NULL and the strict flag is False."""
        bands = band_flags(
            price=105.0, med_ema=100.0, long_ema=None, longest_ema=95.0,
            atr_by_period=ATRS, polarity=POLARITY_UP,
        )
        got = _put(db_engine, _row(ticker_id=9204, bands=bands))
        assert got.data_is_valid is False

    def test_validity_cols_are_the_95_we_expect(self):
        assert set(_VALIDITY_COLS) == set(band_keys()) | {
            f"atr{n}" for n in ATR_PERIODS
        }
        assert len(_VALIDITY_COLS) == 95


class TestDayOfWeek:

    def test_iso_weekday_round_trips(self, db_engine):
        for dow, d in [(1, date(2026, 7, 13)), (5, date(2026, 7, 17))]:
            got = _put(db_engine, _row(
                ticker_id=9210 + dow, signal_date=d, day_of_week=dow,
            ))
            assert got.day_of_week == dow

    def test_day_of_week_is_derived_from_signal_date(self, db_engine):
        """A caller that does not pass day_of_week still gets the right ISO
        weekday: the domain object derives it. 2026-07-15 is a Wednesday.

        Built directly, NOT through _row(), because _row() bakes in a
        day_of_week default — going through it would test the default, not
        the derivation."""
        row = SignalEnrichment(
            ticker_id=9222, timeframe="1W", signal_date=date(2026, 7, 15),
            signal_type="BUY", features={"x": 1.0},
            feature_spec_version="v0.5", industry_id=7,
            bands=_bands(), atrs=dict(ATRS),
            seq_in_week=1, seq_in_consec_weeks=1, signal_price=105.0,
            band_polarity="UP", bars_available=60,
            # day_of_week deliberately omitted -> __post_init__ derives it
        )
        assert row.day_of_week == 3
        got = _put(db_engine, row)
        assert got.day_of_week == 3

    def test_the_check_constraint_rejects_out_of_range(self, db_engine):
        with pytest.raises(Exception):
            with transaction(db_engine) as conn:
                SignalEnrichmentRepository(conn).upsert_one(
                    _row(ticker_id=9221, day_of_week=8)
                )


class TestUpsertRecomputesValidity:

    def test_upsert_flips_validity_when_the_data_changes(self, db_engine):
        atrs = {**ATRS, 40: float("nan")}
        _put(db_engine, _row(ticker_id=9230, bands=_bands(atrs=atrs),
                             atrs=atrs))
        got = _put(db_engine, _row(ticker_id=9230))   # now complete
        assert got.data_is_valid is True
