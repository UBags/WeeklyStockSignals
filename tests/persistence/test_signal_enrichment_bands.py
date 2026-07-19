"""C4b — the §20 columns, against a real Postgres.

Three jobs.

1. DRIFT. signal_bands.band_keys() is the code's truth; migration 0052 is
   the schema's. 0052's own test compares code to MIGRATION. This one
   compares code to the DATABASE, which is the copy that actually matters
   and the only one that can be wrong in a way nothing else catches.

2. NaN. A warm-up bar has no ATR-40, so band_flags emits NaN. Postgres will
   store that NaN in a double precision column without a murmur, and from
   then on AVG() over the column is NaN and `WHERE dist > 0` quietly drops
   the row. It must land as NULL. This is the test that says so.

3. NULL != FALSE. A pre-v0.5 row has no bands. An all-False row means the
   price was on the WRONG SIDE of the EMA when the signal fired — a real,
   informative, and rare state. If those two ever collapse into each other,
   every model trained on this table learns from rows that never happened.
"""
from __future__ import annotations

import math
from datetime import date

import pytest
from sqlalchemy import text

from trip_ema.domain.signal_enrichment import SignalEnrichment
from trip_ema.fundamentals.signal_bands import (
    ATR_PERIODS,
    POLARITY_DOWN,
    POLARITY_UP,
    band_flags,
    band_key,
    band_keys,
    dist_key,
    polarity_for,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.signal_enrichment_repo import (
    SignalEnrichmentRepository,
)

ATRS = {10: 5.0, 15: 8.0, 20: 10.0, 30: 20.0, 40: 40.0}


def _bands(price=105.0, polarity=POLARITY_UP, atrs=None):
    return band_flags(
        price=price, med_ema=100.0, long_ema=98.0, longest_ema=95.0,
        atr_by_period=ATRS if atrs is None else atrs, polarity=polarity,
    )


def _row(**over):
    base = dict(
        ticker_id=9101, timeframe="1W", signal_date=date(2026, 7, 14),
        signal_type="BUY",
        features={"srsi14": 55.0},
        feature_spec_version="v0.5",
        industry_id=7,
        bands=_bands(),
        atrs=dict(ATRS),
        seq_in_week=2, seq_in_consec_weeks=5,
        signal_price=105.0,
        band_polarity=POLARITY_UP,
    )
    base.update(over)
    return SignalEnrichment(**base)


def _get(db_engine, row):
    with db_engine.connect() as conn:
        return SignalEnrichmentRepository(conn).get_for_signal(
            row.ticker_id, row.timeframe, row.signal_date, row.signal_type,
        )


def _put(db_engine, row):
    with transaction(db_engine) as conn:
        SignalEnrichmentRepository(conn).upsert_one(row)
    return _get(db_engine, row)


class TestSchemaDrift:
    """Code vs the database. The copy that matters."""

    def test_every_band_key_is_a_real_column(self, db_engine):
        with db_engine.connect() as conn:
            cols = {
                r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'signal_enrichment'"
                ))
            }
        expected = set(band_keys()) | {f"atr{n}" for n in ATR_PERIODS} | {
            "seq_in_week", "seq_in_consec_weeks", "signal_price", "band_polarity",
        }
        missing = sorted(expected - cols)
        assert not missing, (
            "signal_bands names columns the schema does not have: "
            f"{missing[:10]}. Code and schema have drifted — the fix is a "
            "NEW migration, not an edit to 0052."
        )

    def test_the_table_has_111_columns(self, db_engine):
        with db_engine.connect() as conn:
            n = conn.execute(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'signal_enrichment'"
            )).scalar()
        assert n == 111        # 9 pre-existing + 99 (0052) + 3 (0053)

    def test_the_band_columns_are_nullable(self, db_engine):
        """Until the v0.5 recompute lands, they must be. 0053 tightens
        them — and if 0053 has landed, this test is the thing that tells
        you to update it."""
        with db_engine.connect() as conn:
            nn = {
                r[0] for r in conn.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'signal_enrichment' "
                    "AND is_nullable = 'NO'"
                ))
            }
        assert not (set(band_keys()) & nn)


class TestRoundTrip:

    def test_bands_survive_the_round_trip(self, db_engine):
        got = _put(db_engine, _row())
        assert got is not None
        assert set(got.bands) == set(band_keys())
        # +5 over med with atr20=10 -> +0.50 ATR -> slab 3
        assert got.bands[band_key("med", 20, 3)] is True
        assert got.bands[band_key("med", 20, 2)] is False
        assert got.bands[dist_key("med", 20)] == pytest.approx(0.5)

    def test_the_scalars_survive(self, db_engine):
        got = _put(db_engine, _row())
        assert got.seq_in_week == 2
        assert got.seq_in_consec_weeks == 5
        assert got.signal_price == pytest.approx(105.0)
        assert got.band_polarity == POLARITY_UP
        assert got.atrs == {n: pytest.approx(ATRS[n]) for n in ATR_PERIODS}

    def test_an_exit_from_buy_carries_sell_geometry(self, db_engine):
        """The (k)/(l) rule, all the way to disk and back."""
        pol = polarity_for("EXIT_FROM_BUY")
        assert pol == POLARITY_DOWN
        row = _row(
            ticker_id=9102, signal_type="EXIT_FROM_BUY",
            bands=_bands(price=95.0, polarity=pol),
            band_polarity=pol,
            signal_price=95.0,
        )
        got = _put(db_engine, row)
        assert got.band_polarity == POLARITY_DOWN
        # 5 BELOW med with atr20=10 -> 0.50 ATR in the SELL direction
        assert got.bands[dist_key("med", 20)] == pytest.approx(0.5)
        assert got.bands[band_key("med", 20, 3)] is True

    def test_upsert_overwrites_the_band_columns(self, db_engine):
        row = _row(ticker_id=9103)
        _put(db_engine, row)
        got = _put(db_engine, _row(
            ticker_id=9103, bands=_bands(price=120.0), signal_price=120.0,
            seq_in_week=1,
        ))
        assert got.signal_price == pytest.approx(120.0)
        assert got.seq_in_week == 1
        assert got.bands[band_key("med", 20, 5)] is True    # +2.0 ATR
        assert got.bands[band_key("med", 20, 3)] is False   # the old one


class TestNaNNeverReachesTheColumn:

    def test_a_nan_distance_lands_as_null(self, db_engine):
        """Warm-up: no atr40 yet. band_flags emits NaN; the column must be
        NULL, not NaN."""
        atrs = {**ATRS, 40: float("nan")}
        row = _row(ticker_id=9104, bands=_bands(atrs=atrs), atrs=atrs)
        with transaction(db_engine) as conn:
            SignalEnrichmentRepository(conn).upsert_one(row)
        with db_engine.connect() as conn:
            v = conn.execute(text(
                "SELECT dist_med_atr40, atr40 FROM signal_enrichment "
                "WHERE ticker_id = 9104"
            )).fetchone()
        assert v.dist_med_atr40 is None
        assert v.atr40 is None

    def test_the_column_does_not_go_nan_on_aggregate(self, db_engine):
        """The actual harm. One stored NaN and AVG() over the column is NaN
        forever, for every row, in every query anyone writes."""
        with transaction(db_engine) as conn:
            r = SignalEnrichmentRepository(conn)
            r.upsert_one(_row(ticker_id=9105))
            r.upsert_one(_row(
                ticker_id=9106,
                bands=_bands(atrs={**ATRS, 20: float("nan")}),
                atrs={**ATRS, 20: float("nan")},
            ))
        with db_engine.connect() as conn:
            avg = conn.execute(text(
                "SELECT avg(dist_med_atr20) FROM signal_enrichment "
                "WHERE ticker_id IN (9105, 9106)"
            )).scalar()
        assert avg is not None and not math.isnan(float(avg))
        assert float(avg) == pytest.approx(0.5)      # the NaN row is NULL

    def test_a_nan_signal_price_lands_as_null(self, db_engine):
        row = _row(ticker_id=9107, signal_price=float("nan"))
        got = _put(db_engine, row)
        assert got.signal_price is None


class TestNullIsNotFalse:

    def test_a_row_with_no_bands_reads_back_as_none(self, db_engine):
        """A pre-v0.5 row. bands=None, NOT a dict of Falses."""
        row = _row(
            ticker_id=9108, feature_spec_version="v0.4",
            bands=None, atrs=None, seq_in_week=None, seq_in_consec_weeks=None,
            signal_price=None, band_polarity=None,
        )
        got = _put(db_engine, row)
        assert got.bands is None
        assert got.atrs is None
        assert got.band_polarity is None

    def test_an_all_false_row_is_not_none(self, db_engine):
        """Price BELOW the med EMA on a BUY: every flag is False and that is
        a real, computed, informative row. It must not come back as None."""
        bands = _bands(price=90.0)          # below every EMA
        assert not any(v for v in bands.values() if isinstance(v, bool))
        row = _row(ticker_id=9109, bands=bands, signal_price=90.0)
        got = _put(db_engine, row)
        assert got.bands is not None
        assert got.bands[band_key("med", 20, 1)] is False
        assert got.bands[dist_key("med", 20)] == pytest.approx(-1.0)


class TestGuards:

    def test_a_wrong_key_set_is_rejected_before_it_reaches_sql(self, db_engine):
        """A bands dict that does not match band_keys() is schema drift. It
        must die at the boundary with a message, not as an SQL error 400
        rows into a fan-out worker."""
        bad = dict(_bands())
        bad.pop(band_key("med", 20, 1))
        with pytest.raises(ValueError, match="band_keys"):
            with transaction(db_engine) as conn:
                SignalEnrichmentRepository(conn).upsert_one(
                    _row(ticker_id=9110, bands=bad)
                )

    def test_the_polarity_check_constraint_bites(self, db_engine):
        with pytest.raises(Exception):
            with transaction(db_engine) as conn:
                SignalEnrichmentRepository(conn).upsert_one(
                    _row(ticker_id=9111, band_polarity="SIDEWAYS")
                )

    def test_the_old_construction_signature_still_works(self, db_engine):
        """Every existing call site passes no band fields at all. They must
        keep working, and they must write NULLs."""
        row = SignalEnrichment(
            ticker_id=9112, timeframe="1W", signal_date=date(2026, 7, 14),
            signal_type="SELL", features={"srsi14": 1.0},
            feature_spec_version="v0.4",
        )
        got = _put(db_engine, row)
        assert got is not None
        assert got.bands is None
        assert got.seq_in_week is None
