"""Integration tests for run_financials_refresh (Phase 3).

Uses real Postgres + stub fetcher. Mirrors test_shares_runner.py
structure.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, UTC

import pytest

from trip_ema.domain import TickerRef
from trip_ema.fundamentals.financials_fetch import (
    FinancialsFetchPermanent,
    QuarterlyFinancialInfo,
)
from trip_ema.fundamentals.runners import run_financials_refresh
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    FundamentalsRunRepository,
    QuarterlyFinancialsRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ---------- fixtures ---------- #


@pytest.fixture
def now():
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def nse_tickers(db_engine, now):
    refs = [
        TickerRef("NSE", "RELIANCE", "EQ"),
        TickerRef("NSE", "TCS", "EQ"),
        TickerRef("NSE", "HDFCBANK", "EQ"),
    ]
    ids = {}
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        for ref in refs:
            ids[ref.symbol] = repo.insert(ref, created_at=now)
    return ids


def _four_quarters_for(rev_seq, ebt_seq=None, currency="INR"):
    """Helper: build a 4-element list of QuarterlyFinancialInfo, newest
    first. `rev_seq` and `ebt_seq` are 4-element lists; ebt_seq may
    contain None values to test the partial-coverage case."""
    if ebt_seq is None:
        ebt_seq = [None] * 4
    dates = [date(2025, 12, 31), date(2025, 9, 30),
             date(2025, 6, 30),  date(2025, 3, 31)]
    return [
        QuarterlyFinancialInfo(
            fiscal_quarter=d, revenue=r, ebitda=e, currency=currency,
        )
        for d, r, e in zip(dates, rev_seq, ebt_seq)
    ]


def _stub_fetcher(mapping: dict[str, list[QuarterlyFinancialInfo]]):
    """Builds a fetcher fn. Symbols not in `mapping` raise
    FinancialsFetchPermanent."""
    def _fn(symbol: str, exchange: str, *, max_quarters: int = 4):
        if symbol not in mapping:
            raise FinancialsFetchPermanent(f"{symbol}: not in stub")
        return mapping[symbol][:max_quarters]
    return _fn


# ---------- happy path ---------- #


class TestFinancialsHappyPath:

    def test_all_tickers_get_all_quarters_written(
        self, db_engine, nse_tickers,
    ):
        fetcher = _stub_fetcher({
            "RELIANCE": _four_quarters_for(
                [2500e9, 2400e9, 2300e9, 2200e9],
                [350e9, 340e9, 330e9, 320e9],
            ),
            "TCS": _four_quarters_for(
                [600e9, 590e9, 580e9, 570e9],
                [150e9, 145e9, 140e9, 135e9],
            ),
            "HDFCBANK": _four_quarters_for(
                [800e9, 780e9, 760e9, 740e9],
                [200e9, 195e9, 190e9, 185e9],
            ),
        })
        run_id = run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "SUCCESS"
        assert r.phase == "financials"
        assert r.succeeded == 3
        assert r.failed == 0

        # Verify 4 quarters per ticker = 12 rows total.
        with db_engine.connect() as conn:
            qs = QuarterlyFinancialsRepository(conn).get_latest_n_quarters(
                nse_tickers["RELIANCE"], n=4,
            )
        assert len(qs) == 4
        # Newest first.
        assert qs[0].fiscal_quarter == date(2025, 12, 31)
        assert qs[0].revenue == 2500e9
        assert qs[0].ebitda == 350e9

    def test_null_ebitda_quarters_still_written(self, db_engine, nse_tickers):
        """Common Indian mid/small-cap case: revenue present, EBITDA
        absent. Both still get written; EBITDA is just NULL."""
        fetcher = _stub_fetcher({
            "RELIANCE": _four_quarters_for(
                [100, 110, 120, 130], [None, None, None, None],
            ),
            "TCS": _four_quarters_for(
                [200, 210, 220, 230], [None, None, None, None],
            ),
            "HDFCBANK": _four_quarters_for(
                [300, 310, 320, 330], [None, None, None, None],
            ),
        })
        run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            qs = QuarterlyFinancialsRepository(conn).get_latest_n_quarters(
                nse_tickers["RELIANCE"], n=4,
            )
        assert len(qs) == 4
        assert all(q.ebitda is None for q in qs)
        assert all(q.revenue is not None for q in qs)

    def test_partial_status_when_some_tickers_fail(
        self, db_engine, nse_tickers,
    ):
        fetcher = _stub_fetcher({
            "RELIANCE": _four_quarters_for([100, 110, 120, 130]),
            # TCS, HDFCBANK missing → 2 failures
        })
        run_id = run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "PARTIAL"
        assert r.succeeded == 1
        assert r.failed == 2

    def test_failed_status_when_all_fail(self, db_engine, nse_tickers):
        fetcher = _stub_fetcher({})
        run_id = run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "FAILED"
        assert r.failed == 3


# ---------- time-series shape ---------- #


class TestTimeSeriesShape:

    def test_two_refreshes_produce_history(self, db_engine, nse_tickers):
        """Two successive refreshes (with force=True the second time
        to bypass the skip-if-recent guard) produce two snapshots per
        (ticker, quarter)."""
        import time as _time

        # First refresh.
        fetcher1 = _stub_fetcher({
            sym: _four_quarters_for([100, 100, 100, 100])
            for sym in nse_tickers
        })
        run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher1,
        )

        # Tiny sleep so fetched_at differs.
        _time.sleep(0.01)

        # Second refresh, restated revenue.
        fetcher2 = _stub_fetcher({
            sym: _four_quarters_for([99, 99, 99, 99])  # all restated
            for sym in nse_tickers
        })
        run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, force=True, fetch_fn=fetcher2,
        )

        with db_engine.connect() as conn:
            history = QuarterlyFinancialsRepository(conn).get_history(
                nse_tickers["RELIANCE"], date(2025, 12, 31),
            )
        # Two snapshots for the same (ticker, quarter) pair.
        assert len(history) == 2
        # Newest first → restated value.
        assert history[0].revenue == 99
        assert history[1].revenue == 100

    def test_get_latest_returns_restated_value(self, db_engine, nse_tickers):
        import time as _time
        fetcher1 = _stub_fetcher({
            sym: _four_quarters_for([100, 100, 100, 100])
            for sym in nse_tickers
        })
        run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher1,
        )
        _time.sleep(0.01)
        fetcher2 = _stub_fetcher({
            sym: _four_quarters_for([99, 99, 99, 99])
            for sym in nse_tickers
        })
        run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, force=True, fetch_fn=fetcher2,
        )
        with db_engine.connect() as conn:
            row = QuarterlyFinancialsRepository(conn).get_latest(
                nse_tickers["RELIANCE"], date(2025, 12, 31),
            )
        # get_latest returns the most recent fetched_at.
        assert row.revenue == 99


# ---------- skip-if-recent ---------- #


class TestSkipIfRecent:

    def test_skips_when_recent_success_exists(self, db_engine, nse_tickers):
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            prior = r.open(phase="financials", exchange="NSE")
            r.close(prior, status="SUCCESS",
                    succeeded=3, failed=0, skipped=0)
        from unittest.mock import MagicMock
        fetcher = MagicMock()
        returned = run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher,
        )
        assert returned == prior
        assert fetcher.call_count == 0

    def test_force_overrides_skip(self, db_engine, nse_tickers):
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            prior = r.open(phase="financials", exchange="NSE")
            r.close(prior, status="SUCCESS",
                    succeeded=3, failed=0, skipped=0)
        fetcher = _stub_fetcher({
            sym: _four_quarters_for([100, 100, 100, 100])
            for sym in nse_tickers
        })
        new_rid = run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, force=True, fetch_fn=fetcher,
        )
        assert new_rid != prior

    def test_industry_recent_does_not_block_financials(
        self, db_engine, nse_tickers,
    ):
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            ind = r.open(phase="industry", exchange="NSE")
            r.close(ind, status="SUCCESS",
                    succeeded=3, failed=0, skipped=0)
        fetcher = _stub_fetcher({
            sym: _four_quarters_for([100, 100, 100, 100])
            for sym in nse_tickers
        })
        new_rid = run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher,
        )
        assert new_rid != ind


# ---------- resumability ---------- #


class TestResumability:

    def test_resume_picks_up_at_cursor(self, db_engine, nse_tickers):
        tids_in_order = sorted(nse_tickers.items())
        ticker_list = [
            {"id": tid, "symbol": sym, "instrument_type": "EQ"}
            for sym, tid in tids_in_order
        ]
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="financials", exchange="NSE")
            r.update_progress(
                rid, succeeded=2, failed=0, skipped=0,
                progress_json={"ticker_list": ticker_list, "cursor_index": 2},
            )
            r.close(rid, status="PARTIAL",
                    succeeded=2, failed=0, skipped=0)

        called = []
        def tracking(symbol, exchange, *, max_quarters=4):
            called.append(symbol)
            return _four_quarters_for([100, 100, 100, 100])

        run_financials_refresh(
            db_engine, "NSE",
            resume_run_id=rid,
            request_delay_s=0, fetch_fn=tracking,
        )
        # Only the last ticker (index 2) should have been fetched.
        assert len(called) == 1
        assert called == [ticker_list[2]["symbol"]]

    def test_resume_rejects_wrong_phase(self, db_engine, nse_tickers):
        """Can't resume an 'industry' run as if it were 'financials'."""
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="industry", exchange="NSE")
            r.update_progress(
                rid, succeeded=0, failed=0, skipped=0,
                progress_json={"ticker_list": [], "cursor_index": 0},
            )
            r.close(rid, status="PARTIAL",
                    succeeded=0, failed=0, skipped=0)
        with pytest.raises(ValueError, match="phase 'industry'"):
            run_financials_refresh(
                db_engine, "NSE",
                resume_run_id=rid,
                request_delay_s=0,
                fetch_fn=lambda s, e, **kw: _four_quarters_for([1]*4),
            )


# ---------- edge cases ---------- #


class TestEdgeCases:

    def test_empty_ticker_universe(self, db_engine):
        run_id = run_financials_refresh(
            db_engine, "BSE",
            request_delay_s=0,
            fetch_fn=lambda s, e, **kw: _four_quarters_for([1]*4),
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "SUCCESS"
        assert r.phase == "financials"
        assert r.succeeded == 0

    def test_fetcher_returning_fewer_than_4_quarters_is_OK(
        self, db_engine, nse_tickers,
    ):
        """yfinance returning 2 quarters instead of 4 is normal for
        younger companies. The runner should write all available
        quarters without complaint."""
        from trip_ema.fundamentals.financials_fetch import QuarterlyFinancialInfo
        fetcher = _stub_fetcher({
            "RELIANCE": [
                QuarterlyFinancialInfo(
                    fiscal_quarter=date(2025, 12, 31),
                    revenue=100, ebitda=15, currency="INR",
                ),
                QuarterlyFinancialInfo(
                    fiscal_quarter=date(2025, 9, 30),
                    revenue=110, ebitda=17, currency="INR",
                ),
            ],
            "TCS": _four_quarters_for([200, 210, 220, 230]),
            "HDFCBANK": _four_quarters_for([300, 310, 320, 330]),
        })
        run_id = run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
            qs = QuarterlyFinancialsRepository(conn).get_latest_n_quarters(
                nse_tickers["RELIANCE"], n=4,
            )
        assert r.status == "SUCCESS"
        # RELIANCE has only 2 quarters; the others have 4.
        assert len(qs) == 2

    def test_unexpected_exception_counts_as_failed(self, db_engine, nse_tickers):
        def boom(symbol, exchange, *, max_quarters=4):
            if symbol == "TCS":
                raise RuntimeError("simulated programming error")
            return _four_quarters_for([1, 2, 3, 4])
        run_id = run_financials_refresh(
            db_engine, "NSE",
            request_delay_s=0, fetch_fn=boom,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.succeeded == 2
        assert r.failed == 1
        assert r.status == "PARTIAL"
