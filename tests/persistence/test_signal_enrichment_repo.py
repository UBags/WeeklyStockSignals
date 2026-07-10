"""Tests for SignalEnrichmentRepository (per-signal feature store, §19).

Requires migration 0040_signal_enrichment applied to the test DB.
ticker_id has no FK, so no ticker fixture is needed.
"""
from datetime import date

from trip_ema.domain.signal_enrichment import SignalEnrichment
from trip_ema.persistence import transaction
from trip_ema.persistence.signal_enrichment_repo import (
    SignalEnrichmentRepository,
)


def _row(**over):
    base = dict(
        ticker_id=9001, timeframe="1W", signal_date=date(2024, 1, 8),
        signal_type="BUY",
        features={"srsi14": 55.0, "med_ema_structure": "UPTREND",
                  "bars_since_med_ema_turn": 3, "pdisp_med_ema": 1.2},
        feature_spec_version="v0.3", industry_id=7,
    )
    base.update(over)
    return SignalEnrichment(**base)


class TestSignalEnrichmentRepo:
    def test_upsert_and_readback_jsonb(self, db_engine):
        with transaction(db_engine) as conn:
            SignalEnrichmentRepository(conn).upsert_one(_row())
        with db_engine.connect() as conn:
            got = SignalEnrichmentRepository(conn).get_for_signal(
                9001, "1W", date(2024, 1, 8), "BUY",
            )
        assert got is not None
        assert got.features["srsi14"] == 55.0
        assert got.features["med_ema_structure"] == "UPTREND"
        assert got.industry_id == 7
        assert got.feature_spec_version == "v0.3"

    def test_upsert_updates_in_place(self, db_engine):
        with transaction(db_engine) as conn:
            r = SignalEnrichmentRepository(conn)
            r.upsert_one(_row(ticker_id=9002))
            r.upsert_one(_row(ticker_id=9002, features={"srsi14": 99.0}))
        with db_engine.connect() as conn:
            got = SignalEnrichmentRepository(conn).get_for_signal(
                9002, "1W", date(2024, 1, 8), "BUY",
            )
        assert got.features == {"srsi14": 99.0}   # unique key -> replaced

    def test_upsert_many_and_get_missing(self, db_engine):
        rows = [_row(ticker_id=9010 + i, signal_date=date(2024, 2, 1))
                for i in range(3)]
        with transaction(db_engine) as conn:
            n = SignalEnrichmentRepository(conn).upsert_many(rows)
        assert n == 3
        with db_engine.connect() as conn:
            r = SignalEnrichmentRepository(conn)
            assert r.get_for_signal(9010, "1W", date(2024, 2, 1), "BUY")
            assert r.get_for_signal(99999, "1W", date(2024, 2, 1), "BUY") is None

    def test_empty_upsert_many_is_noop(self, db_engine):
        with transaction(db_engine) as conn:
            assert SignalEnrichmentRepository(conn).upsert_many([]) == 0
