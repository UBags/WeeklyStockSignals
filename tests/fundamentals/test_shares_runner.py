"""Integration tests for run_shares_refresh (Phase 2).

Uses the same Postgres fixture and the same stub-fetcher pattern as
test_runners.py. The runner is the integration point — these tests
catch bugs in resumability, time-series insertion, and rate-limit
retry that the unit tests for the fetcher can't see.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.fundamentals.runners import run_shares_refresh
from trip_ema.fundamentals.shares_fetch import (
    SharesFetchPermanent,
    SharesFetchRateLimited,
    SharesFetchTransient,
    SharesInfo,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    InstrumentSharesRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ---------- fixtures ---------- #


@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


@pytest.fixture
def nse_tickers(db_engine, now):
    """Five NSE tickers for the shares runner tests."""
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


def _stub_shares_fetcher(mapping: dict[str, SharesInfo]):
    """Build a fetcher fn that returns mapped SharesInfo per symbol.
    Symbols not in `mapping` raise SharesFetchPermanent."""
    def _fn(symbol: str, exchange: str) -> SharesInfo:
        if symbol in mapping:
            return mapping[symbol]
        raise SharesFetchPermanent(f"{symbol}: not in stub")
    return _fn


# ---------- happy path ---------- #


class TestSharesHappyPath:

    def test_all_tickers_get_shares_written(self, db_engine, nse_tickers):
        fetcher = _stub_shares_fetcher({
            "RELIANCE":  SharesInfo(6_700_000_000, "YFINANCE"),
            "TCS":       SharesInfo(3_600_000_000, "YFINANCE"),
            "HDFCBANK":  SharesInfo(7_600_000_000, "YFINANCE"),
            "INFY":      SharesInfo(4_100_000_000, "YFINANCE"),
            "WIPRO":     SharesInfo(5_400_000_000, "YFINANCE"),
        })
        run_id = run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
            row = InstrumentSharesRepository(conn).get_latest(
                nse_tickers["TCS"],
            )
        assert r.status == "SUCCESS"
        assert r.phase == "shares"
        assert r.succeeded == 5
        assert r.failed == 0
        assert row.shares_outstanding == 3_600_000_000
        assert row.shares_source == "YFINANCE"
        assert row.as_of_date == datetime.now(timezone.utc).date()

    def test_implied_source_recorded_distinctly(self, db_engine, nse_tickers):
        """When the fetcher returns YFINANCE_IMPLIED, that provenance
        must persist to the row."""
        fetcher = _stub_shares_fetcher({
            "RELIANCE": SharesInfo(6_700_000_000, "YFINANCE"),
            "TCS":      SharesInfo(3_600_000_000, "YFINANCE_IMPLIED"),
            "HDFCBANK": SharesInfo(7_600_000_000, "YFINANCE"),
            "INFY":     SharesInfo(4_100_000_000, "YFINANCE"),
            "WIPRO":    SharesInfo(5_400_000_000, "YFINANCE"),
        })
        run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = InstrumentSharesRepository(conn)
            assert r.get_latest(nse_tickers["RELIANCE"]).shares_source == "YFINANCE"
            assert r.get_latest(nse_tickers["TCS"]).shares_source == "YFINANCE_IMPLIED"

    def test_partial_status_when_some_fail(self, db_engine, nse_tickers):
        fetcher = _stub_shares_fetcher({
            "RELIANCE": SharesInfo(1, "YFINANCE"),
            "TCS":      SharesInfo(1, "YFINANCE"),
            # 3 missing
        })
        run_id = run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "PARTIAL"
        assert r.succeeded == 2
        assert r.failed == 3

    def test_failed_status_when_all_fail(self, db_engine, nse_tickers):
        fetcher = _stub_shares_fetcher({})
        run_id = run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "FAILED"
        assert r.failed == 5


# ---------- time-series shape ---------- #


class TestTimeSeriesShape:
    """The runner inserts ONE snapshot per refresh per ticker. Multiple
    refreshes on different days produce a history; same-day refreshes
    UPDATE the existing row (idempotency)."""

    def test_same_day_refresh_does_not_duplicate(self, db_engine, nse_tickers):
        """Two refreshes the same day → one row per ticker, not two.
        The second refresh's values win (UPSERT semantics)."""
        fetcher1 = _stub_shares_fetcher({
            "RELIANCE": SharesInfo(1_000, "YFINANCE"),
            "TCS":      SharesInfo(2_000, "YFINANCE"),
            "HDFCBANK": SharesInfo(3_000, "YFINANCE"),
            "INFY":     SharesInfo(4_000, "YFINANCE"),
            "WIPRO":    SharesInfo(5_000, "YFINANCE"),
        })
        run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher1,
        )
        # Run again, same day, with different (corrected) numbers.
        fetcher2 = _stub_shares_fetcher({
            "RELIANCE": SharesInfo(1_500, "YFINANCE"),
            "TCS":      SharesInfo(2_500, "YFINANCE"),
            "HDFCBANK": SharesInfo(3_500, "YFINANCE"),
            "INFY":     SharesInfo(4_500, "YFINANCE"),
            "WIPRO":    SharesInfo(5_500, "YFINANCE"),
        })
        run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher2, force=True,
        )
        with db_engine.connect() as conn:
            r = InstrumentSharesRepository(conn)
            history = r.list_history(nse_tickers["RELIANCE"])
        # One row, with the second refresh's value.
        assert len(history) == 1
        assert history[0].shares_outstanding == 1_500  # second refresh value

    def test_run_writes_today_date(self, db_engine, nse_tickers):
        """as_of_date on the new rows is today's UTC date."""
        fetcher = _stub_shares_fetcher({
            s: SharesInfo(1, "YFINANCE") for s in nse_tickers
        })
        run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher,
        )
        today = datetime.now(timezone.utc).date()
        with db_engine.connect() as conn:
            row = InstrumentSharesRepository(conn).get_latest(
                nse_tickers["RELIANCE"],
            )
        assert row.as_of_date == today


# ---------- skip-if-recent + force ---------- #


class TestSkipIfRecent:

    def test_skips_when_recent_success_exists(self, db_engine, nse_tickers):
        """A SUCCESS shares run from earlier today should prevent a
        re-run; the function returns the prior run_id."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            prior_rid = r.open(phase="shares", exchange="NSE")
            r.close(prior_rid, status="SUCCESS",
                    succeeded=5, failed=0, skipped=0)
        fetcher = MagicMock()  # should NOT be called
        returned = run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher,
        )
        assert returned == prior_rid
        assert fetcher.call_count == 0

    def test_force_overrides_skip(self, db_engine, nse_tickers):
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            prior_rid = r.open(phase="shares", exchange="NSE")
            r.close(prior_rid, status="SUCCESS",
                    succeeded=5, failed=0, skipped=0)
        fetcher = _stub_shares_fetcher({
            s: SharesInfo(1, "YFINANCE") for s in nse_tickers
        })
        new_rid = run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0, force=True, fetch_fn=fetcher,
        )
        assert new_rid != prior_rid

    def test_industry_recent_run_does_not_block_shares(
        self, db_engine, nse_tickers,
    ):
        """The skip-guard is per-phase. An industry SUCCESS run from
        today shouldn't prevent today's first shares run."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            ind_rid = r.open(phase="industry", exchange="NSE")
            r.close(ind_rid, status="SUCCESS",
                    succeeded=5, failed=0, skipped=0)
        fetcher = _stub_shares_fetcher({
            s: SharesInfo(1, "YFINANCE") for s in nse_tickers
        })
        new_rid = run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher,
        )
        # A new run was created, not the industry one returned.
        assert new_rid != ind_rid


# ---------- resumability ---------- #


class TestResumability:

    def test_resume_picks_up_at_cursor(self, db_engine, nse_tickers):
        """Simulate a shares run that processed 3 of 5 then died."""
        tids_in_order = sorted(nse_tickers.items())
        ticker_list = [
            {"id": tid, "symbol": sym, "instrument_type": "EQ"}
            for sym, tid in tids_in_order
        ]

        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="shares", exchange="NSE")
            r.update_progress(
                rid, succeeded=3, failed=0, skipped=0,
                progress_json={
                    "ticker_list": ticker_list,
                    "cursor_index": 3,
                },
            )
            r.close(rid, status="PARTIAL",
                    succeeded=3, failed=0, skipped=0)

        called = []
        def tracking(symbol, exchange):
            called.append(symbol)
            return SharesInfo(1, "YFINANCE")

        resumed = run_shares_refresh(
            db_engine, "NSE",
            resume_run_id=rid,
            request_delay_s=0,
            fetch_fn=tracking,
        )
        assert resumed == rid
        assert len(called) == 2
        # Only the last two symbols (indexes 3 and 4) were fetched.
        expected = [ticker_list[3]["symbol"], ticker_list[4]["symbol"]]
        assert called == expected

    def test_resume_rejects_wrong_phase(self, db_engine, nse_tickers):
        """Can't resume an 'industry' run as if it were a 'shares' run."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="industry", exchange="NSE")
            r.update_progress(
                rid, succeeded=0, failed=0, skipped=0,
                progress_json={"ticker_list": [], "cursor_index": 0},
            )
            r.close(rid, status="PARTIAL", succeeded=0, failed=0, skipped=0)
        with pytest.raises(ValueError, match="phase 'industry'"):
            run_shares_refresh(
                db_engine, "NSE",
                resume_run_id=rid,
                request_delay_s=0,
                fetch_fn=lambda s, e: SharesInfo(1, "YFINANCE"),
            )


# ---------- edge cases ---------- #


class TestEdgeCases:

    def test_empty_ticker_universe_writes_success_run(self, db_engine):
        run_id = run_shares_refresh(
            db_engine, "BSE",
            request_delay_s=0,
            fetch_fn=lambda s, e: SharesInfo(1, "YFINANCE"),
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "SUCCESS"
        assert r.phase == "shares"
        assert r.succeeded == 0

    def test_unexpected_exception_counts_as_failed(self, db_engine, nse_tickers):
        """Belt-and-braces: any exception not in our typed hierarchy
        still counts as a failure rather than crashing the run."""
        def explody(symbol, exchange):
            if symbol == "TCS":
                raise RuntimeError("simulated programming error")
            return SharesInfo(1, "YFINANCE")
        run_id = run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0,
            fetch_fn=explody,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.succeeded == 4
        assert r.failed == 1
        assert r.status == "PARTIAL"

    def test_etfs_get_shares_too(self, db_engine, nse_tickers, now):
        """ETFs DO have meaningful sharesOutstanding (units issued).
        The runner should NOT skip them the way industry does."""
        # Make TCS an ETF and verify it still gets fetched.
        with transaction(db_engine) as conn:
            conn.execute(text(
                "UPDATE tickers SET instrument_type='ETF' "
                "WHERE ticker_id=:tid"
            ), {"tid": nse_tickers["TCS"]})
        called = []
        def tracking(symbol, exchange):
            called.append(symbol)
            return SharesInfo(1_000, "YFINANCE")
        run_shares_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=tracking,
        )
        assert "TCS" in called  # ETF was fetched
        with db_engine.connect() as conn:
            row = InstrumentSharesRepository(conn).get_latest(nse_tickers["TCS"])
        assert row.shares_outstanding == 1_000
