"""C6a — ensemble runner scoping, with the emphasis on "changed nothing else".

The risk in adding a scope parameter to a load-all-sweep-all runner is that
the sweep quietly changes for the unscoped case too. These tests pin that it
does not: None must behave exactly as before, and the compute/skip logic must
be identical whether scoped or not.
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from trip_ema.fundamentals.signal_ensemble_runner import (
    backfill_signal_ensemble_features,
)
from trip_ema.persistence import transaction


from trip_ema.domain import TickerRef
from trip_ema.persistence.tickers_repo import TickersRepository
from trip_ema.persistence.fundamentals_repo import InstrumentIndustryRepository


def _seed(conn):
    """Minimal fixture: two tickers in one industry that has an ensemble, a
    breadth row for the members at the signals' week, and a signal each.

    instrument_industry.ticker_id is an FK to tickers(ticker_id), so the
    tickers MUST exist first — created through TickersRepository so the id is
    real, not invented. Returns the three assigned ticker_ids so the tests
    scope against them without hard-coding.
    """
    from datetime import UTC, datetime
    now = datetime.now(UTC)

    tids = [
        TickersRepository(conn).insert(
            TickerRef("NSE", sym, "EQ"), created_at=now,
        )
        for sym in ("ALPHACO", "ALPHACO2", "BETACO")
    ]
    t_alpha1, t_alpha2, t_beta = tids

    InstrumentIndustryRepository(conn).upsert(
        t_alpha1, sector="X", industry="ALPHA")
    InstrumentIndustryRepository(conn).upsert(
        t_alpha2, sector="X", industry="ALPHA")
    InstrumentIndustryRepository(conn).upsert(
        t_beta, sector="X", industry="BETA")

    # ALPHA's ensemble: one variant 'v1', one leader member 'BETA'.
    # best_lag, sign, asof_date are all NOT NULL in the schema.
    conn.execute(text(
        "INSERT INTO influential_industry_ensemble "
        "(industry_i, member_j, variant, corr, best_lag, is_leader, sign, "
        "asof_date) "
        "VALUES ('ALPHA', 'BETA', 'v1', 0.8, 0, TRUE, 1, :wk)"
    ), {"wk": date(2026, 7, 13)})
    wk = date(2026, 7, 13)   # a Monday
    conn.execute(text(
        "INSERT INTO industry_regime_breadth (industry_name, asof_date, "
        "breadth) VALUES ('BETA', :wk, 0.6)"
    ), {"wk": wk})
    # three signal_enrichment rows (alpha1, alpha2 in ALPHA; beta in BETA
    # which has no ensemble, so it is skipped either way). signal_enrichment_id
    # is not an FK target for these tickers, and ticker_id has no FK, but we
    # use the real ids so the scoped tests filter on something meaningful.
    se_ids = {}
    for i, tid in enumerate(tids):
        se_id = 8001 + i
        se_ids[tid] = se_id
        conn.execute(text(
            "INSERT INTO signal_enrichment (signal_enrichment_id, ticker_id, "
            "timeframe, signal_date, signal_type, features, "
            "feature_spec_version, day_of_week, data_is_valid) VALUES "
            "(:id, :tid, '1W', :sd, 'BUY', '{}', 'v0.5', 1, TRUE)"
        ), {"id": se_id, "tid": tid, "sd": date(2026, 7, 14)})
    return tids, se_ids


def _rows(conn):
    return {
        (r.signal_enrichment_id, r.variant): r
        for r in conn.execute(text(
            "SELECT signal_enrichment_id, variant, ensemble_score, "
            "ensemble_confirms FROM signal_ensemble_features"
        )).all()
    }


class TestUnscopedIsUnchanged:

    def test_none_sweeps_every_eligible_signal(self, db_engine):
        with transaction(db_engine) as conn:
            _seed(conn)
        n = backfill_signal_ensemble_features(db_engine, "NSE")
        with db_engine.connect() as conn:
            rows = _rows(conn)
        # alpha1 (se 8001) and alpha2 (se 8002) are in ALPHA (has ensemble)
        # -> one 'v1' row each. beta (se 8003) is in BETA (no ensemble as
        # industry_i) -> skipped.
        assert (8001, "v1") in rows
        assert (8002, "v1") in rows
        assert (8003, "v1") not in rows
        assert n == 2

    def test_the_beta_skip_still_happens(self, db_engine):
        """`ind not in ens` is the skip that keeps the BETA ticker out. It
        must fire the same whether scoped or not."""
        with transaction(db_engine) as conn:
            _seed(conn)
        backfill_signal_ensemble_features(db_engine, "NSE")
        with db_engine.connect() as conn:
            assert (8003, "v1") not in _rows(conn)


class TestScoped:

    def test_a_ticker_list_restricts_the_sweep(self, db_engine):
        with transaction(db_engine) as conn:
            tids, _se = _seed(conn)
        n = backfill_signal_ensemble_features(
            db_engine, "NSE", scoped_tickers=[tids[0]],   # alpha1 only
        )
        with db_engine.connect() as conn:
            rows = _rows(conn)
        assert (8001, "v1") in rows
        assert (8002, "v1") not in rows      # alpha2 was out of scope
        assert n == 1

    def test_scoped_and_unscoped_agree_on_the_shared_ticker(self, db_engine):
        """The computed VALUES for alpha1 must be identical whether it was
        reached via a full sweep or a scoped one — same core calc."""
        with transaction(db_engine) as conn:
            tids, _se = _seed(conn)
        backfill_signal_ensemble_features(
            db_engine, "NSE", scoped_tickers=[tids[0]])
        with db_engine.connect() as conn:
            scoped = _rows(conn)[(8001, "v1")]
        # wipe and recompute unscoped
        with transaction(db_engine) as conn:
            conn.execute(text("DELETE FROM signal_ensemble_features"))
        backfill_signal_ensemble_features(db_engine, "NSE")
        with db_engine.connect() as conn:
            full = _rows(conn)[(8001, "v1")]
        assert scoped.ensemble_score == full.ensemble_score
        assert scoped.ensemble_confirms == full.ensemble_confirms

    def test_empty_list_means_no_tickers_not_all(self, db_engine):
        """The trap. An empty scope is a cycle where nothing flipped: zero
        rows, NOT a full sweep."""
        with transaction(db_engine) as conn:
            _seed(conn)
        n = backfill_signal_ensemble_features(
            db_engine, "NSE", scoped_tickers=[],
        )
        assert n == 0
        with db_engine.connect() as conn:
            assert _rows(conn) == {}

    def test_scoping_to_an_ineligible_ticker_yields_nothing(self, db_engine):
        with transaction(db_engine) as conn:
            tids, _se = _seed(conn)
        n = backfill_signal_ensemble_features(
            db_engine, "NSE", scoped_tickers=[tids[2]],   # beta, no ensemble
        )
        assert n == 0
