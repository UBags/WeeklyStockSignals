"""Runner integration tests for Phase 1 industry refresh.

These use the real Postgres fixture (db_engine) and a mocked fetch
function. The runner is the integration point — these tests catch
bugs in the resumability, override-handling, and checkpoint logic
that the unit tests can't.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.fundamentals.industry_fetch import (
    IndustryFetchPermanent,
    IndustryFetchRateLimited,
    IndustryFetchTransient,
    IndustryInfo,
)
from trip_ema.fundamentals.runners import run_industry_refresh
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    IndustryOverrideRepository,
    InstrumentIndustryRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ---------- fixtures ---------- #


@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def nse_tickers(db_engine, now):
    """Five NSE tickers we'll fetch industry for."""
    refs = [
        TickerRef("NSE", "RELIANCE", "EQ"),
        TickerRef("NSE", "TCS", "EQ"),
        TickerRef("NSE", "HDFCBANK", "EQ"),
        TickerRef("NSE", "INFY", "EQ"),
        TickerRef("NSE", "WIPRO", "EQ"),
    ]
    ids = {}
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        for ref in refs:
            ids[ref.symbol] = repo.insert(ref, created_at=now)
    return ids


def _stub_fetcher(mapping: dict[str, IndustryInfo]):
    """Build a fetcher fn that returns mapped IndustryInfo per symbol.
    Symbols not in `mapping` raise IndustryFetchPermanent (treated as
    a failure by the runner). Tests use this rather than mock.patch
    because the runner takes fetch_fn as an explicit dependency."""
    def _fn(symbol: str, exchange: str) -> IndustryInfo:
        if symbol in mapping:
            return mapping[symbol]
        raise IndustryFetchPermanent(f"{symbol}: not in stub")
    return _fn


# ---------- happy path ---------- #


class TestHappyPath:

    def test_all_tickers_get_industry_assigned(self, db_engine, nse_tickers):
        fetcher = _stub_fetcher({
            "RELIANCE": IndustryInfo("Energy", "Oil & Gas"),
            "TCS": IndustryInfo("Technology", "IT Services"),
            "HDFCBANK": IndustryInfo("Financial Services", "Banking"),
            "INFY": IndustryInfo("Technology", "IT Services"),
            "WIPRO": IndustryInfo("Technology", "IT Services"),
        })
        run_id = run_industry_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
            ind = InstrumentIndustryRepository(conn)
            tcs_row = ind.get(nse_tickers["TCS"])
        assert r.status == "SUCCESS"
        assert r.succeeded == 5
        assert r.failed == 0
        assert r.skipped == 0
        assert tcs_row.industry == "IT Services"

    def test_returns_partial_when_some_fail(self, db_engine, nse_tickers):
        fetcher = _stub_fetcher({
            "RELIANCE": IndustryInfo("Energy", "Oil & Gas"),
            "TCS": IndustryInfo("Technology", "IT Services"),
            # 3 missing → 3 failures
        })
        run_id = run_industry_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "PARTIAL"
        assert r.succeeded == 2
        assert r.failed == 3
        assert r.skipped == 0

    def test_returns_failed_when_all_fail(self, db_engine, nse_tickers):
        fetcher = _stub_fetcher({})  # no symbols match
        run_id = run_industry_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "FAILED"
        assert r.failed == 5

    def test_null_sector_and_industry_still_counts_as_success(
        self, db_engine, nse_tickers,
    ):
        """yfinance returning a valid response with no classification
        is a success — we want a row to exist so the next refresh
        can find it. Failure means the FETCH failed, not that
        classification is missing."""
        fetcher = _stub_fetcher({
            "RELIANCE": IndustryInfo(None, None),
            "TCS": IndustryInfo(None, None),
            "HDFCBANK": IndustryInfo(None, None),
            "INFY": IndustryInfo(None, None),
            "WIPRO": IndustryInfo(None, None),
        })
        run_id = run_industry_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
            row = InstrumentIndustryRepository(conn).get(nse_tickers["TCS"])
        assert r.status == "SUCCESS"
        assert r.succeeded == 5
        # Row exists but with NULL classification — that's the contract.
        assert row is not None
        assert row.sector is None
        assert row.industry is None


# ---------- override handling ---------- #


class TestOverrideHandling:

    def test_overridden_tickers_are_skipped_not_overwritten(
        self, db_engine, nse_tickers,
    ):
        """The whole point of overrides: operator's classification
        beats yfinance's."""
        # Operator manually classifies TCS.
        with transaction(db_engine) as conn:
            IndustryOverrideRepository(conn).upsert(
                nse_tickers["TCS"],
                sector="Manual Sector",
                industry="Manual Industry",
                set_by="operator",
            )
        # Runner tries to refresh; should leave TCS alone.
        fetcher = _stub_fetcher({
            "RELIANCE": IndustryInfo("Energy", "Oil"),
            "TCS": IndustryInfo("WrongSector", "WrongIndustry"),
            "HDFCBANK": IndustryInfo("Financial", "Banking"),
            "INFY": IndustryInfo("Tech", "IT"),
            "WIPRO": IndustryInfo("Tech", "IT"),
        })
        run_id = run_industry_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
            tcs = InstrumentIndustryRepository(conn).get(nse_tickers["TCS"])
        assert r.status == "SUCCESS"
        # 4 fetched (RELIANCE/HDFCBANK/INFY/WIPRO), 1 skipped (TCS).
        assert r.succeeded == 4
        assert r.skipped == 1
        # TCS still has the operator's values.
        assert tcs.industry == "Manual Industry"
        assert tcs.classification_source == "MANUAL_OVERRIDE"


# ---------- skip-if-recent guard ---------- #


class TestSkipIfRecent:

    def test_skips_when_recent_success_exists(self, db_engine, nse_tickers):
        """A SUCCESS run from yesterday means we don't re-run."""
        # Fake a recent successful run by writing one directly.
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            prior_run_id = r.open(phase="industry", exchange="NSE")
            r.close(prior_run_id, status="SUCCESS", succeeded=5, failed=0, skipped=0)
        fetcher = MagicMock()  # should never be called
        returned_id = run_industry_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=fetcher,
        )
        # Should return the OLD run id, not a new one.
        assert returned_id == prior_run_id
        assert fetcher.call_count == 0

    def test_force_overrides_skip(self, db_engine, nse_tickers):
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            prior_run_id = r.open(phase="industry", exchange="NSE")
            r.close(prior_run_id, status="SUCCESS", succeeded=5, failed=0, skipped=0)
        fetcher = _stub_fetcher({
            s: IndustryInfo("x", "y") for s in nse_tickers
        })
        new_run_id = run_industry_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            force=True,
            fetch_fn=fetcher,
        )
        assert new_run_id != prior_run_id


# ---------- resumability ---------- #


class TestResumability:

    def test_resume_from_partial_picks_up_at_cursor(self, db_engine, nse_tickers):
        """Simulate: a run died after processing 3 of 5 tickers.
        Resume should process only the remaining 2."""
        # Manually set up a PARTIAL run with checkpoint at index 3.
        tids_in_order = sorted(nse_tickers.items())  # sorted by symbol
        ticker_list = [{"id": tid, "symbol": sym} for sym, tid in tids_in_order]

        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            run_id = r.open(phase="industry", exchange="NSE")
            r.update_progress(
                run_id,
                succeeded=3, failed=0, skipped=0,
                progress_json={
                    "ticker_list": ticker_list,
                    "cursor_index": 3,
                },
            )
            r.close(run_id, status="PARTIAL",
                    succeeded=3, failed=0, skipped=0)

        # Track which symbols the fetcher was actually called for.
        called_symbols = []

        def tracking_fetcher(symbol, exchange):
            called_symbols.append(symbol)
            return IndustryInfo("x", "y")

        resumed_id = run_industry_refresh(
            db_engine, "NSE",
            resume_run_id=run_id,
            request_delay_s=0,
            fetch_fn=tracking_fetcher,
        )
        assert resumed_id == run_id
        # Only the last 2 of the 5 should have been fetched.
        assert len(called_symbols) == 2
        # Those 2 are the ones at indexes 3 and 4 in the sorted list.
        expected_remaining = [ticker_list[3]["symbol"], ticker_list[4]["symbol"]]
        assert called_symbols == expected_remaining

    def test_resume_uses_original_ticker_list_not_current(
        self, db_engine, nse_tickers, now,
    ):
        """If new tickers are added between original run and resume,
        the resume should NOT pick them up — it processes the SAME
        list."""
        tids_in_order = sorted(nse_tickers.items())
        ticker_list = [{"id": tid, "symbol": sym} for sym, tid in tids_in_order]

        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            run_id = r.open(phase="industry", exchange="NSE")
            r.update_progress(
                run_id, succeeded=3, failed=0, skipped=0,
                progress_json={"ticker_list": ticker_list, "cursor_index": 3},
            )
            r.close(run_id, status="PARTIAL",
                    succeeded=3, failed=0, skipped=0)
            # Add a new ticker mid-resume.
            TickersRepository(conn).insert(
                TickerRef("NSE", "NEWLISTING", "EQ"), created_at=now,
            )

        called = []
        def tracking(symbol, exchange):
            called.append(symbol)
            return IndustryInfo("x", "y")

        run_industry_refresh(
            db_engine, "NSE",
            resume_run_id=run_id,
            request_delay_s=0,
            fetch_fn=tracking,
        )
        # Resume should NOT have included NEWLISTING.
        assert "NEWLISTING" not in called

    def test_resume_rejects_terminal_status(self, db_engine, nse_tickers):
        """Can't resume a SUCCESS or FAILED run — start a new one
        with --force instead."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            run_id = r.open(phase="industry", exchange="NSE")
            r.update_progress(
                run_id, succeeded=5, failed=0, skipped=0,
                progress_json={"ticker_list": [], "cursor_index": 0},
            )
            r.close(run_id, status="SUCCESS",
                    succeeded=5, failed=0, skipped=0)
        with pytest.raises(ValueError, match="terminal status SUCCESS"):
            run_industry_refresh(
                db_engine, "NSE",
                resume_run_id=run_id,
                request_delay_s=0,
                fetch_fn=lambda s, e: IndustryInfo("x", "y"),
            )

    def test_resume_rejects_unknown_run_id(self, db_engine):
        with pytest.raises(ValueError, match="does not exist"):
            run_industry_refresh(
                db_engine, "NSE",
                resume_run_id=999_999,
                request_delay_s=0,
                fetch_fn=lambda s, e: IndustryInfo("x", "y"),
            )


# ---------- edge cases ---------- #


class TestEdgeCases:

    def test_empty_ticker_universe_writes_success_run(self, db_engine):
        """No active tickers for this exchange — write a SUCCESS run
        with zero counts. (BSE has no tickers in this fixture.)"""
        run_id = run_industry_refresh(
            db_engine, "BSE",
            request_delay_s=0,
            fetch_fn=lambda s, e: IndustryInfo("x", "y"),
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "SUCCESS"
        assert r.succeeded == 0
        assert r.failed == 0
        assert r.skipped == 0

    def test_unexpected_exception_counts_as_failed(self, db_engine, nse_tickers):
        """Defense in depth: if the fetcher raises a totally unrelated
        exception (programming error, anything), the runner counts it
        and keeps going rather than crashing the whole run."""
        def explodey_fetcher(symbol, exchange):
            if symbol == "TCS":
                raise RuntimeError("simulated programming error")
            return IndustryInfo("x", "y")

        run_id = run_industry_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=explodey_fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        # 4 succeeded, 1 failed (TCS).
        assert r.succeeded == 4
        assert r.failed == 1
        assert r.status == "PARTIAL"


# ---------- coverage_counts after a real run ---------- #


def test_coverage_counts_reflects_actual_rows(db_engine, nse_tickers):
    """End-to-end: after a successful run, the verification query
    from the design doc should show classified == total."""
    fetcher = _stub_fetcher({
        s: IndustryInfo("x", "y") for s in nse_tickers
    })
    run_industry_refresh(
        db_engine, "NSE", request_delay_s=0, fetch_fn=fetcher,
    )
    with db_engine.connect() as conn:
        counts = InstrumentIndustryRepository(conn).coverage_counts()
    by_ex = {ex: (total, classified) for ex, total, classified in counts}
    assert by_ex["NSE"] == (5, 5)


# ---------- ETF branching (migration 0007 + runner update) ---------- #


class TestEtfBranching:
    """The runner has two paths: EQ → yfinance for sector/industry,
    ETF → yfinance for longName only, write 'ETF' bucket. These tests
    verify the branch happens correctly based on instrument_type."""

    @pytest.fixture
    def mixed_universe(self, db_engine, now):
        """3 EQ + 2 ETF tickers. ETF instrument_type set explicitly
        because TickersRepository.insert defaults it to 'EQ'."""
        from sqlalchemy import text as _text
        refs = [
            ("EQ", TickerRef("NSE", "RELIANCE", "EQ")),
            ("EQ", TickerRef("NSE", "TCS", "EQ")),
            ("EQ", TickerRef("NSE", "HDFCBANK", "EQ")),
            ("ETF", TickerRef("NSE", "NIFTYBEES", "ETF")),
            ("ETF", TickerRef("NSE", "GOLDBEES", "ETF")),
        ]
        ids = {}
        with transaction(db_engine) as conn:
            repo = TickersRepository(conn)
            for itype, ref in refs:
                tid = repo.insert(ref, created_at=now)
                if itype == "ETF":
                    conn.execute(
                        _text("UPDATE tickers SET instrument_type='ETF' "
                              "WHERE ticker_id=:tid"),
                        {"tid": tid},
                    )
                ids[ref.symbol] = (itype, tid)
        return ids

    def test_etfs_get_etf_bucket_not_yfinance_sector(
        self, db_engine, mixed_universe,
    ):
        """The headline test: ETFs end up with industry='ETF' regardless
        of whatever yfinance returned for sector/industry."""
        eq_calls = []
        def fetcher(symbol, exchange):
            eq_calls.append(symbol)
            # Imagine yfinance returns some garbage for ETFs.
            return IndustryInfo("WrongSector", "WrongIndustry")

        # Also patch the ETF longName fetcher so we don't hit network.
        with patch(
            "trip_ema.fundamentals.runners._try_fetch_etf_long_name",
            return_value="Some ETF long name",
        ):
            run_industry_refresh(
                db_engine, "NSE",
                request_delay_s=0,
                fetch_fn=fetcher,
            )

        # The equity fetcher should have been called ONLY for equities.
        assert sorted(eq_calls) == ["HDFCBANK", "RELIANCE", "TCS"]

        # ETFs got the 'ETF' bucket.
        with db_engine.connect() as conn:
            ind = InstrumentIndustryRepository(conn)
            niftybees = ind.get(mixed_universe["NIFTYBEES"][1])
            goldbees = ind.get(mixed_universe["GOLDBEES"][1])
        assert niftybees.industry == "ETF"
        assert niftybees.sector == "ETF"
        assert niftybees.classification_source == "INSTRUMENT_TYPE"
        assert niftybees.etf_long_name == "Some ETF long name"
        assert goldbees.industry == "ETF"

    def test_etf_longname_fetch_failure_still_classifies_as_etf(
        self, db_engine, mixed_universe,
    ):
        """If yfinance fails when we try to fetch the longName for an
        ETF, we still classify it. The longName is best-effort."""
        def fetcher(symbol, exchange):
            return IndustryInfo("Some", "Sector")

        with patch(
            "trip_ema.fundamentals.runners._try_fetch_etf_long_name",
            return_value=None,  # simulate failure / no longName
        ):
            run_industry_refresh(
                db_engine, "NSE",
                request_delay_s=0,
                fetch_fn=fetcher,
            )

        with db_engine.connect() as conn:
            niftybees = InstrumentIndustryRepository(conn).get(
                mixed_universe["NIFTYBEES"][1],
            )
        assert niftybees.industry == "ETF"
        assert niftybees.etf_long_name is None

    def test_etf_counts_as_succeeded(self, db_engine, mixed_universe):
        def fetcher(symbol, exchange):
            return IndustryInfo("Tech", "IT")

        with patch(
            "trip_ema.fundamentals.runners._try_fetch_etf_long_name",
            return_value="X",
        ):
            run_id = run_industry_refresh(
                db_engine, "NSE",
                request_delay_s=0,
                fetch_fn=fetcher,
            )

        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        # 3 EQ + 2 ETF all succeeded; 0 failed.
        assert r.status == "SUCCESS"
        assert r.succeeded == 5
        assert r.failed == 0

    def test_etf_with_override_is_skipped(self, db_engine, mixed_universe):
        """Even ETFs respect operator overrides."""
        from trip_ema.persistence.fundamentals_repo import (
            IndustryOverrideRepository,
        )
        niftybees_tid = mixed_universe["NIFTYBEES"][1]
        with transaction(db_engine) as conn:
            IndustryOverrideRepository(conn).upsert(
                niftybees_tid, sector="Custom", industry="Special",
                set_by="operator",
            )

        with patch(
            "trip_ema.fundamentals.runners._try_fetch_etf_long_name",
            return_value="X",
        ):
            run_id = run_industry_refresh(
                db_engine, "NSE",
                request_delay_s=0,
                fetch_fn=lambda s, e: IndustryInfo("X", "Y"),
            )

        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
            niftybees = InstrumentIndustryRepository(conn).get(niftybees_tid)
        # The ETF was counted in skipped, not succeeded.
        assert r.skipped == 1
        # And its row in instrument_industry still has the override.
        assert niftybees.industry == "Special"
        assert niftybees.classification_source == "MANUAL_OVERRIDE"
