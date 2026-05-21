"""Tests for fundamentals repos (Phase 1).

Uses the same Postgres fixture pattern as the other persistence tests:
db_engine from conftest gives a fresh DB per test with Alembic applied
through head.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import text

from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndustryOverrideRepository,
    InstrumentIndustryRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository
from trip_ema.domain import TickerRef


# ---------- fixtures ---------- #


@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def ticker_ids(db_engine, now):
    """A small ticker universe for the tests: 3 NSE + 1 BSE."""
    refs = [
        TickerRef("NSE", "RELIANCE", "EQ"),
        TickerRef("NSE", "TCS", "EQ"),
        TickerRef("NSE", "HDFCBANK", "EQ"),
        TickerRef("BSE", "RELIANCE", "EQ"),
    ]
    ids = {}
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        for ref in refs:
            ids[(ref.exchange, ref.symbol)] = repo.insert(ref, created_at=now)
    return ids


# ---------- InstrumentIndustryRepository ---------- #


class TestInstrumentIndustryUpsert:

    def test_insert_then_read(self, db_engine, ticker_ids):
        tid = ticker_ids[("NSE", "RELIANCE")]
        with transaction(db_engine) as conn:
            r = InstrumentIndustryRepository(conn)
            r.upsert(tid, sector="Energy", industry="Oil & Gas Refining & Marketing")
        with db_engine.connect() as conn:
            row = InstrumentIndustryRepository(conn).get(tid)
        assert row is not None
        assert row.ticker_id == tid
        assert row.sector == "Energy"
        assert row.industry == "Oil & Gas Refining & Marketing"
        assert row.classification_source == "YFINANCE"

    def test_upsert_updates_existing(self, db_engine, ticker_ids):
        tid = ticker_ids[("NSE", "RELIANCE")]
        with transaction(db_engine) as conn:
            r = InstrumentIndustryRepository(conn)
            r.upsert(tid, sector="Energy", industry="Old Name")
            r.upsert(tid, sector="Energy", industry="New Name")
        with db_engine.connect() as conn:
            assert InstrumentIndustryRepository(conn).get(tid).industry == "New Name"

    def test_upsert_rejects_bad_source(self, db_engine, ticker_ids):
        tid = ticker_ids[("NSE", "RELIANCE")]
        with transaction(db_engine) as conn:
            r = InstrumentIndustryRepository(conn)
            with pytest.raises(ValueError, match="classification_source"):
                r.upsert(tid, sector="x", industry="y", classification_source="FOO")

    def test_get_returns_none_for_unknown(self, db_engine):
        with db_engine.connect() as conn:
            assert InstrumentIndustryRepository(conn).get(999_999) is None


class TestUpsertUnlessOverridden:
    """The runner's hot path. Must skip writes when an override exists."""

    def test_writes_when_no_override(self, db_engine, ticker_ids):
        tid = ticker_ids[("NSE", "TCS")]
        with transaction(db_engine) as conn:
            r = InstrumentIndustryRepository(conn)
            wrote = r.upsert_unless_overridden(
                tid, sector="Technology", industry="IT Services",
            )
        assert wrote is True
        with db_engine.connect() as conn:
            assert InstrumentIndustryRepository(conn).get(tid).industry == "IT Services"

    def test_skips_when_override_exists(self, db_engine, ticker_ids):
        tid = ticker_ids[("NSE", "TCS")]
        # Operator manually sets an override.
        with transaction(db_engine) as conn:
            IndustryOverrideRepository(conn).upsert(
                tid, sector="Manual", industry="Operator Set",
                note="test", set_by="operator",
            )
        # Runner tries to refresh; must be a no-op for THIS ticker.
        with transaction(db_engine) as conn:
            r = InstrumentIndustryRepository(conn)
            wrote = r.upsert_unless_overridden(
                tid, sector="Technology", industry="IT Services",
            )
        assert wrote is False
        with db_engine.connect() as conn:
            # Still the operator's values.
            row = InstrumentIndustryRepository(conn).get(tid)
            assert row.industry == "Operator Set"
            assert row.classification_source == "MANUAL_OVERRIDE"


class TestCoverageCounts:

    def test_empty_db_returns_zeros_per_exchange(self, db_engine, ticker_ids):
        """All tickers exist but none have an industry row."""
        with db_engine.connect() as conn:
            counts = InstrumentIndustryRepository(conn).coverage_counts()
        by_ex = {ex: (total, classified) for ex, total, classified in counts}
        assert by_ex["NSE"] == (3, 0)
        assert by_ex["BSE"] == (1, 0)

    def test_counts_classified(self, db_engine, ticker_ids):
        with transaction(db_engine) as conn:
            r = InstrumentIndustryRepository(conn)
            r.upsert(ticker_ids[("NSE", "RELIANCE")], sector="Energy", industry="Oil")
            r.upsert(ticker_ids[("NSE", "TCS")], sector="Tech", industry="IT")
            # NSE HDFCBANK has a row but null industry — should NOT count.
            r.upsert(ticker_ids[("NSE", "HDFCBANK")], sector=None, industry=None)
        with db_engine.connect() as conn:
            counts = InstrumentIndustryRepository(conn).coverage_counts()
        by_ex = {ex: (total, classified) for ex, total, classified in counts}
        assert by_ex["NSE"] == (3, 2)


# ---------- IndustryOverrideRepository ---------- #


class TestIndustryOverride:

    def test_upsert_writes_both_tables(self, db_engine, ticker_ids):
        tid = ticker_ids[("NSE", "RELIANCE")]
        with transaction(db_engine) as conn:
            IndustryOverrideRepository(conn).upsert(
                tid, sector="Energy", industry="Hand-set",
                note="ops decision", set_by="uddi",
            )
        with db_engine.connect() as conn:
            ov = IndustryOverrideRepository(conn).get(tid)
            ind = InstrumentIndustryRepository(conn).get(tid)
        assert ov.industry == "Hand-set"
        assert ov.note == "ops decision"
        assert ind.industry == "Hand-set"
        assert ind.classification_source == "MANUAL_OVERRIDE"

    def test_delete_removes_override_only(self, db_engine, ticker_ids):
        """Deleting the override row leaves the instrument_industry row.
        Operators may want to "unfreeze" a ticker without losing the
        last good classification."""
        tid = ticker_ids[("NSE", "RELIANCE")]
        with transaction(db_engine) as conn:
            IndustryOverrideRepository(conn).upsert(
                tid, sector="Energy", industry="Hand-set",
            )
        with transaction(db_engine) as conn:
            removed = IndustryOverrideRepository(conn).delete(tid)
        assert removed is True
        with db_engine.connect() as conn:
            assert IndustryOverrideRepository(conn).get(tid) is None
            # instrument_industry row still there
            assert InstrumentIndustryRepository(conn).get(tid) is not None

    def test_list_all(self, db_engine, ticker_ids):
        tids = [ticker_ids[("NSE", "RELIANCE")], ticker_ids[("NSE", "TCS")]]
        with transaction(db_engine) as conn:
            r = IndustryOverrideRepository(conn)
            for tid in tids:
                r.upsert(tid, sector="X", industry="Y")
        with db_engine.connect() as conn:
            all_ovs = IndustryOverrideRepository(conn).list_all()
        assert {ov.ticker_id for ov in all_ovs} == set(tids)


# ---------- FundamentalsRunRepository ---------- #


class TestFundamentalsRun:

    def test_open_close_roundtrip(self, db_engine):
        with transaction(db_engine) as conn:
            run_id = FundamentalsRunRepository(conn).open(
                phase="industry", exchange="NSE", notes="test run",
            )
        assert run_id > 0
        with transaction(db_engine) as conn:
            FundamentalsRunRepository(conn).close(
                run_id, status="SUCCESS", succeeded=100, failed=2, skipped=5,
            )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.phase == "industry"
        assert r.exchange == "NSE"
        assert r.status == "SUCCESS"
        assert r.succeeded == 100
        assert r.failed == 2
        assert r.skipped == 5
        assert r.completed_at is not None

    def test_rejects_invalid_phase(self, db_engine):
        with transaction(db_engine) as conn:
            with pytest.raises(ValueError, match="phase"):
                FundamentalsRunRepository(conn).open(phase="bogus")

    def test_rejects_invalid_status_on_close(self, db_engine):
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(phase="industry")
        with transaction(db_engine) as conn:
            with pytest.raises(ValueError, match="status"):
                FundamentalsRunRepository(conn).close(
                    rid, status="bogus", succeeded=0, failed=0, skipped=0,
                )

    def test_cannot_close_to_RUNNING(self, db_engine):
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(phase="industry")
        with transaction(db_engine) as conn:
            with pytest.raises(ValueError, match="terminal"):
                FundamentalsRunRepository(conn).close(
                    rid, status="RUNNING", succeeded=0, failed=0, skipped=0,
                )

    def test_update_progress_with_json(self, db_engine):
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(phase="industry", exchange="NSE")
        with transaction(db_engine) as conn:
            FundamentalsRunRepository(conn).update_progress(
                rid,
                succeeded=42,
                progress_json={"cursor": "RELIANCE", "remaining": 1800},
            )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(rid)
        assert r.succeeded == 42
        assert r.progress_json == {"cursor": "RELIANCE", "remaining": 1800}

    def test_update_progress_partial(self, db_engine):
        """Passing None for fields means don't touch."""
        with transaction(db_engine) as conn:
            rid = FundamentalsRunRepository(conn).open(phase="industry")
            FundamentalsRunRepository(conn).update_progress(rid, succeeded=10, failed=2)
        with transaction(db_engine) as conn:
            FundamentalsRunRepository(conn).update_progress(rid, skipped=3)
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(rid)
        assert r.succeeded == 10
        assert r.failed == 2
        assert r.skipped == 3

    def test_list_recent_filters_by_phase_and_exchange(self, db_engine):
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            r.open(phase="industry", exchange="NSE")
            r.open(phase="industry", exchange="BSE")
            r.open(phase="shares", exchange="NSE")
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn)
            assert len(r.list_recent(phase="industry")) == 2
            assert len(r.list_recent(phase="industry", exchange="NSE")) == 1
            assert len(r.list_recent(exchange="NSE")) == 2

    def test_last_success_scoped_to_exchange(self, db_engine):
        """The quarterly scheduler asks 'was THIS exchange refreshed
        in the last quarter?' — must not be confused by other exchanges."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            nse_rid = r.open(phase="industry", exchange="NSE")
            bse_rid = r.open(phase="industry", exchange="BSE")
            r.close(nse_rid, status="SUCCESS", succeeded=1, failed=0, skipped=0)
            r.close(bse_rid, status="FAILED", succeeded=0, failed=1, skipped=0)
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn)
            assert r.last_success(phase="industry", exchange="NSE").run_id == nse_rid
            # BSE only has a FAILED run — last_success returns None.
            assert r.last_success(phase="industry", exchange="BSE") is None


# ---------- ETF classification (migration 0007) ---------- #


class TestUpsertEtf:
    """Tests for InstrumentIndustryRepository.upsert_etf — the ETF
    classification path added in migration 0007."""

    def test_writes_etf_row(self, db_engine, ticker_ids):
        tid = ticker_ids[("NSE", "RELIANCE")]
        with transaction(db_engine) as conn:
            wrote = InstrumentIndustryRepository(conn).upsert_etf(
                tid, etf_long_name="Nippon India ETF Nifty IT",
            )
        assert wrote is True
        with db_engine.connect() as conn:
            row = InstrumentIndustryRepository(conn).get(tid)
        assert row.sector == "ETF"
        assert row.industry == "ETF"
        assert row.classification_source == "INSTRUMENT_TYPE"
        assert row.etf_long_name == "Nippon India ETF Nifty IT"

    def test_writes_etf_row_with_null_long_name(self, db_engine, ticker_ids):
        """yfinance returned no longName at all — still bucket it."""
        tid = ticker_ids[("NSE", "RELIANCE")]
        with transaction(db_engine) as conn:
            wrote = InstrumentIndustryRepository(conn).upsert_etf(
                tid, etf_long_name=None,
            )
        assert wrote is True
        with db_engine.connect() as conn:
            row = InstrumentIndustryRepository(conn).get(tid)
        assert row.industry == "ETF"
        assert row.etf_long_name is None

    def test_etf_upsert_honors_override(self, db_engine, ticker_ids):
        """Even ETFs can have an operator override (e.g. operator
        wants to mark a fund-of-funds as something specific)."""
        tid = ticker_ids[("NSE", "RELIANCE")]
        with transaction(db_engine) as conn:
            IndustryOverrideRepository(conn).upsert(
                tid, sector="Custom", industry="My Special ETF Bucket",
                set_by="operator",
            )
        with transaction(db_engine) as conn:
            wrote = InstrumentIndustryRepository(conn).upsert_etf(
                tid, etf_long_name="some etf",
            )
        assert wrote is False  # override blocked the write
        with db_engine.connect() as conn:
            row = InstrumentIndustryRepository(conn).get(tid)
        # Override survived intact.
        assert row.industry == "My Special ETF Bucket"
        assert row.classification_source == "MANUAL_OVERRIDE"

    def test_etf_upsert_overwrites_previous_etf(self, db_engine, ticker_ids):
        """Re-running the runner shouldn't churn — but if the long
        name changes (e.g. AMC renamed the ETF), we capture the new
        value."""
        tid = ticker_ids[("NSE", "RELIANCE")]
        with transaction(db_engine) as conn:
            r = InstrumentIndustryRepository(conn)
            r.upsert_etf(tid, etf_long_name="Old Name")
            r.upsert_etf(tid, etf_long_name="New Name")
        with db_engine.connect() as conn:
            row = InstrumentIndustryRepository(conn).get(tid)
        assert row.etf_long_name == "New Name"


class TestCoverageCountsByType:
    """Tests for the new instrument-type-aware coverage query."""

    def test_separates_eq_and_etf(self, db_engine, now):
        # Manually insert two tickers: one EQ, one ETF. TickersRepository.insert
        # sets `instrument` but the tickers schema also has a separate
        # `instrument_type` column (defaults to 'EQ') which is what
        # coverage_counts_by_type keys off. Update it explicitly for the
        # ETF row.
        from sqlalchemy import text as _text
        with transaction(db_engine) as conn:
            repo = TickersRepository(conn)
            eq_tid = repo.insert(TickerRef("NSE", "RELIANCE", "EQ"), created_at=now)
            etf_tid = repo.insert(TickerRef("NSE", "NIFTYBEES", "ETF"), created_at=now)
            conn.execute(
                _text("UPDATE tickers SET instrument_type='ETF' WHERE ticker_id=:tid"),
                {"tid": etf_tid},
            )
            ind = InstrumentIndustryRepository(conn)
            ind.upsert(eq_tid, sector="Energy", industry="Oil & Gas")
            ind.upsert_etf(etf_tid, etf_long_name="Nippon Nifty BeES")
        with db_engine.connect() as conn:
            counts = InstrumentIndustryRepository(conn).coverage_counts_by_type()
        # Find the two rows.
        by_key = {(ex, typ): (total, classified) for ex, typ, total, classified in counts}
        assert by_key[("NSE", "EQ")] == (1, 1)
        assert by_key[("NSE", "ETF")] == (1, 1)

    def test_unclassified_etf_shows_as_unclassified(self, db_engine, now):
        from sqlalchemy import text as _text
        with transaction(db_engine) as conn:
            repo = TickersRepository(conn)
            etf_tid = repo.insert(TickerRef("NSE", "RAW", "ETF"), created_at=now)
            conn.execute(
                _text("UPDATE tickers SET instrument_type='ETF' WHERE ticker_id=:tid"),
                {"tid": etf_tid},
            )
            # No upsert_etf call → row not in instrument_industry.
        with db_engine.connect() as conn:
            counts = InstrumentIndustryRepository(conn).coverage_counts_by_type()
        by_key = {(ex, typ): (total, classified) for ex, typ, total, classified in counts}
        assert by_key[("NSE", "ETF")] == (1, 0)
