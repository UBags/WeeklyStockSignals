"""C6c — the two EOD scope anti-joins.

These are the whole risk surface of the EOD wiring: if a scope selects the
wrong rows, the cycle either misses today's signals or re-walks all history.
The tests pin the two anti-joins against a seeded schema, using the repos to
satisfy the tickers FK (the lesson from the ensemble-scoping fixture).
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.persistence import transaction
from trip_ema.persistence.tickers_repo import TickersRepository
from trip_ema.orchestrator.eod_signal_scopes import (
    tickers_needing_enrichment,
    tickers_needing_labels,
)

DAY = date(2026, 7, 14)


def _ticker(conn, symbol, *, exchange="NSE"):
    return TickersRepository(conn).insert(
        TickerRef(exchange, symbol, "EQ"), created_at=datetime.now(UTC),
    )


def _hist_signal(conn, tid, *, day=DAY, stype="BUY"):
    # historical_signals has several NOT NULL columns and CHECK constraints:
    # bar_timestamp, regime_before/after in {-1,0,1} and unequal,
    # price_at_signal > 0, computed_at. signal_id is sequence-defaulted.
    conn.execute(text(
        "INSERT INTO historical_signals (ticker_id, timeframe, "
        "bar_timestamp, signal_type, regime_before, regime_after, "
        "price_at_signal, computed_at, signal_date) VALUES "
        "(:t, '1W', :ts, :st, 0, 1, 100.0, :ts, :d)"
    ), {"t": tid, "st": stype, "d": day,
        "ts": datetime(2026, 7, 14, tzinfo=UTC)})


def _enrichment(conn, se_id, tid, *, day=DAY, stype="BUY", ver="v0.5"):
    conn.execute(text(
        "INSERT INTO signal_enrichment (signal_enrichment_id, ticker_id, "
        "timeframe, signal_date, signal_type, features, feature_spec_version, "
        "day_of_week, data_is_valid) VALUES "
        "(:id, :t, '1W', :d, :st, '{}', :v, 1, TRUE)"
    ), {"id": se_id, "t": tid, "d": day, "st": stype, "v": ver})


def _label(conn, se_id, *, complete):
    conn.execute(text(
        "INSERT INTO signal_labels (signal_enrichment_id, horizon_complete) "
        "VALUES (:id, :c)"
    ), {"id": se_id, "c": complete})


class TestEnrichmentScope:

    def test_a_flipped_ticker_with_no_enrichment_is_selected(self, db_engine):
        with transaction(db_engine) as conn:
            t = _ticker(conn, "FLIP1")
            _hist_signal(conn, t)
        got = tickers_needing_enrichment(
            db_engine, "NSE", DAY, spec_version="v0.5")
        assert got == [t]

    def test_an_already_enriched_ticker_is_not_selected(self, db_engine):
        with transaction(db_engine) as conn:
            t = _ticker(conn, "DONE1")
            _hist_signal(conn, t)
            _enrichment(conn, 5001, t)          # same key, v0.5
        got = tickers_needing_enrichment(
            db_engine, "NSE", DAY, spec_version="v0.5")
        assert t not in got

    def test_an_old_spec_row_still_needs_enrichment(self, db_engine):
        """A v0.4 enrichment row does NOT satisfy a v0.5 ask — the anti-join
        keys on spec_version, so a bump re-selects the ticker."""
        with transaction(db_engine) as conn:
            t = _ticker(conn, "STALE1")
            _hist_signal(conn, t)
            _enrichment(conn, 5002, t, ver="v0.4")
        got = tickers_needing_enrichment(
            db_engine, "NSE", DAY, spec_version="v0.5")
        assert t in got

    def test_a_different_day_is_not_selected(self, db_engine):
        with transaction(db_engine) as conn:
            t = _ticker(conn, "YDAY1")
            _hist_signal(conn, t, day=date(2026, 7, 13))
        got = tickers_needing_enrichment(
            db_engine, "NSE", DAY, spec_version="v0.5")
        assert t not in got

    def test_another_exchange_is_not_selected(self, db_engine):
        with transaction(db_engine) as conn:
            t = _ticker(conn, "NYSE1", exchange="NYSE")
            _hist_signal(conn, t)
        got = tickers_needing_enrichment(
            db_engine, "NSE", DAY, spec_version="v0.5")
        assert t not in got

    def test_self_correcting_after_a_failed_prior_eod(self, db_engine):
        """historical_signals is UNIQUE (ticker_id, timeframe, signal_date) —
        at most ONE net signal per ticker per day. So the real
        self-correction case is not two signal types on one day (impossible);
        it is a single flip whose enrichment a prior EOD never wrote (the run
        died before stage 9). The anti-join must still select it next time.

        Two tickers flip today; only one got enriched. The other is still
        selected."""
        with transaction(db_engine) as conn:
            done = _ticker(conn, "OK1")
            _hist_signal(conn, done)
            _enrichment(conn, 5003, done)        # enriched last EOD

            missed = _ticker(conn, "MISSED1")
            _hist_signal(conn, missed)           # flipped, never enriched
        got = tickers_needing_enrichment(
            db_engine, "NSE", DAY, spec_version="v0.5")
        assert missed in got
        assert done not in got


class TestLabelScope:

    def test_an_enrichment_with_no_label_is_selected(self, db_engine):
        with transaction(db_engine) as conn:
            t = _ticker(conn, "NOLBL")
            _enrichment(conn, 6001, t)
        got = tickers_needing_labels(db_engine, "NSE")
        assert t in got

    def test_an_incomplete_label_is_selected(self, db_engine):
        with transaction(db_engine) as conn:
            t = _ticker(conn, "MATURING")
            _enrichment(conn, 6002, t)
            _label(conn, 6002, complete=False)
        got = tickers_needing_labels(db_engine, "NSE")
        assert t in got

    def test_a_complete_label_is_not_selected(self, db_engine):
        with transaction(db_engine) as conn:
            t = _ticker(conn, "FINAL")
            _enrichment(conn, 6003, t)
            _label(conn, 6003, complete=True)
        got = tickers_needing_labels(db_engine, "NSE")
        assert t not in got

    def test_maturity_set_is_not_todays_flips(self, db_engine):
        """The crux: a ticker whose ONLY signal is a completed label is NOT
        in the label scope, even though it might be a today's-flip candidate
        elsewhere. The two scopes are genuinely different sets."""
        with transaction(db_engine) as conn:
            done = _ticker(conn, "DONELBL")
            _enrichment(conn, 6004, done)
            _label(conn, 6004, complete=True)

            maturing = _ticker(conn, "OLD8W")
            _enrichment(conn, 6005, maturing)
            _label(conn, 6005, complete=False)
        got = set(tickers_needing_labels(db_engine, "NSE"))
        assert maturing in got
        assert done not in got

    def test_another_exchange_is_not_selected(self, db_engine):
        with transaction(db_engine) as conn:
            t = _ticker(conn, "NYSELBL", exchange="NYSE")
            _enrichment(conn, 6006, t)
        got = tickers_needing_labels(db_engine, "NSE")
        assert t not in got
