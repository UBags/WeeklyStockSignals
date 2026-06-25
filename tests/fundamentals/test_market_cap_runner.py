"""Integration tests for run_market_cap_backfill (Phase 4A).

Uses real Postgres + stub fetcher. Per-ticker outer loop means the
runner makes one fetch call per ticker, and the resume cursor
advances by ticker (not by date).
"""
from __future__ import annotations

from datetime import date, datetime, timezone, UTC
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.fundamentals.market_cap_fetch import (
    HistoricalMcapFetchPermanent,
    HistoricalMcapPoint,
)
from trip_ema.fundamentals.runners import run_market_cap_backfill
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    DailyMarketCapRepository,
    FundamentalsRunRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository


# ---------- fixtures ---------- #


@pytest.fixture
def now():
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def nse_tickers(db_engine, now):
    """3 NSE EQ + 1 NSE ETF (negative control — ETF should be
    excluded from market cap backfill)."""
    refs = [
        TickerRef("NSE", "RELIANCE", "EQ"),
        TickerRef("NSE", "TCS",      "EQ"),
        TickerRef("NSE", "HDFCBANK", "EQ"),
        TickerRef("NSE", "GOLDBEES", "EQ"),  # marked ETF below
    ]
    ids: dict[str, int] = {}
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        for ref in refs:
            ids[ref.symbol] = repo.insert(ref, created_at=now)
        conn.execute(text(
            "UPDATE tickers SET instrument_type='ETF' WHERE ticker_id=:tid"
        ), {"tid": ids["GOLDBEES"]})
    return ids


def _points_for(close_seq: list[float], shares_seq: list[int],
                start: date, day_step: int = 30) -> list[HistoricalMcapPoint]:
    """Helper: build a list of HistoricalMcapPoint over a sequence
    of dates."""
    from datetime import timedelta
    out = []
    for i, (c, s) in enumerate(zip(close_seq, shares_seq)):
        out.append(HistoricalMcapPoint(
            trade_date=start + timedelta(days=i * day_step),
            close_price=c,
            shares_outstanding=s,
        ))
    return out


def _stub_hist_fetcher(mapping: dict[str, list[HistoricalMcapPoint]]):
    """Build a fetch_fn that returns mapped points per symbol.
    Symbols not in mapping raise HistoricalMcapFetchPermanent."""
    def _fn(symbol: str, exchange: str, from_d, to_d):
        if symbol not in mapping:
            raise HistoricalMcapFetchPermanent(f"{symbol}: not stubbed")
        return mapping[symbol]
    return _fn


# ---------- happy path ---------- #


class TestBackfillHappyPath:

    def test_all_tickers_get_points_written(self, db_engine, nse_tickers):
        """3 NSE EQ tickers, each with 4 points → 12 rows total.
        ETF should not appear."""
        fetcher = _stub_hist_fetcher({
            "RELIANCE": _points_for(
                [1300.0, 1350.0, 1400.0, 1450.0],
                [6_700_000_000] * 4,
                start=date(2025, 6, 1),
            ),
            "TCS": _points_for(
                [3800.0, 3900.0, 4000.0, 4100.0],
                [3_600_000_000] * 4,
                start=date(2025, 6, 1),
            ),
            "HDFCBANK": _points_for(
                [1500.0, 1550.0, 1600.0, 1700.0],
                [7_600_000_000] * 4,
                start=date(2025, 6, 1),
            ),
        })
        run_id = run_market_cap_backfill(
            db_engine, "NSE",
            from_date=date(2025, 5, 1),
            to_date=date(2026, 5, 1),
            request_delay_s=0,
            hist_fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "SUCCESS"
        assert r.phase == "market_cap"
        assert r.succeeded == 3
        assert r.failed == 0

        with db_engine.connect() as conn:
            r = DailyMarketCapRepository(conn)
            ric_history = r.list_history(nse_tickers["RELIANCE"])
        assert len(ric_history) == 4
        # Math check on one row.
        ric_first = sorted(ric_history, key=lambda p: p.trade_date)[0]
        assert ric_first.close_price == 1300.0
        assert ric_first.shares_used == 6_700_000_000
        assert ric_first.market_cap == pytest.approx(1300.0 * 6_700_000_000)
        assert ric_first.source == "COMPUTED"

    def test_etfs_excluded_from_backfill(self, db_engine, nse_tickers):
        """The ETF should not be in the fetcher's call list."""
        called = []
        def tracker(symbol, exchange, from_d, to_d):
            called.append(symbol)
            return _points_for(
                [100.0, 110.0],
                [1_000_000] * 2,
                start=date(2025, 6, 1),
            )
        run_market_cap_backfill(
            db_engine, "NSE",
            from_date=date(2025, 5, 1),
            to_date=date(2026, 5, 1),
            request_delay_s=0,
            hist_fetch_fn=tracker,
        )
        assert "GOLDBEES" not in called
        # All 3 EQ tickers were called.
        assert set(called) == {"RELIANCE", "TCS", "HDFCBANK"}

    def test_partial_status_when_some_tickers_fail(
        self, db_engine, nse_tickers,
    ):
        fetcher = _stub_hist_fetcher({
            "RELIANCE": _points_for([1300.0], [6_700_000_000],
                                     start=date(2025, 6, 1)),
            # TCS, HDFCBANK absent → 2 failures
        })
        run_id = run_market_cap_backfill(
            db_engine, "NSE",
            from_date=date(2025, 5, 1),
            to_date=date(2026, 5, 1),
            request_delay_s=0,
            hist_fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "PARTIAL"
        assert r.succeeded == 1
        assert r.failed == 2

    def test_failed_status_when_all_fail(self, db_engine, nse_tickers):
        fetcher = _stub_hist_fetcher({})
        run_id = run_market_cap_backfill(
            db_engine, "NSE",
            from_date=date(2025, 5, 1),
            to_date=date(2026, 5, 1),
            request_delay_s=0,
            hist_fetch_fn=fetcher,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "FAILED"
        assert r.failed == 3


# ---------- skip-if-recent ---------- #


class TestSkipIfRecent:

    def test_skips_when_recent_success_exists(self, db_engine, nse_tickers):
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            prior = r.open(phase="market_cap", exchange="NSE")
            r.close(prior, status="SUCCESS",
                    succeeded=3, failed=0, skipped=0)
        fetcher = MagicMock()
        returned = run_market_cap_backfill(
            db_engine, "NSE",
            from_date=date(2025, 5, 1),
            to_date=date(2026, 5, 1),
            request_delay_s=0,
            hist_fetch_fn=fetcher,
        )
        assert returned == prior
        assert fetcher.call_count == 0

    def test_force_overrides_skip(self, db_engine, nse_tickers):
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            prior = r.open(phase="market_cap", exchange="NSE")
            r.close(prior, status="SUCCESS",
                    succeeded=3, failed=0, skipped=0)
        fetcher = _stub_hist_fetcher({
            sym: _points_for([100.0], [1_000], start=date(2025, 6, 1))
            for sym in ("RELIANCE", "TCS", "HDFCBANK")
        })
        new_rid = run_market_cap_backfill(
            db_engine, "NSE",
            from_date=date(2025, 5, 1),
            to_date=date(2026, 5, 1),
            request_delay_s=0,
            force=True,
            hist_fetch_fn=fetcher,
        )
        assert new_rid != prior


# ---------- resumability ---------- #


class TestResumability:

    def test_resume_picks_up_at_ticker_cursor(self, db_engine, nse_tickers):
        """Simulate a run that processed 2 of 3 EQ tickers then died.
        Resume should fetch only the remaining 1."""
        # Build the same ticker_list shape the runner uses.
        eq_tickers = [
            (tid, sym) for sym, tid in nse_tickers.items() if sym != "GOLDBEES"
        ]
        eq_tickers.sort(key=lambda p: p[1])  # by symbol, like the runner
        ticker_list_json = [
            {"id": tid, "symbol": sym, "instrument_type": "EQ"}
            for tid, sym in eq_tickers
        ]
        with transaction(db_engine) as conn:
            r = FundamentalsRunRepository(conn)
            rid = r.open(phase="market_cap", exchange="NSE")
            r.update_progress(
                rid, succeeded=2, failed=0, skipped=0,
                progress_json={
                    "ticker_list": ticker_list_json,
                    "cursor_index": 2,
                    "from_date": "2025-05-01",
                    "to_date":   "2026-05-01",
                },
            )
            r.close(rid, status="PARTIAL",
                    succeeded=2, failed=0, skipped=0)

        called = []
        def tracking(symbol, exchange, from_d, to_d):
            called.append(symbol)
            return _points_for(
                [100.0], [1_000_000], start=date(2025, 6, 1),
            )

        run_market_cap_backfill(
            db_engine, "NSE",
            from_date=date(2025, 5, 1),
            to_date=date(2026, 5, 1),
            resume_run_id=rid,
            request_delay_s=0,
            hist_fetch_fn=tracking,
        )
        # Only the last ticker (index 2 of 0..2) was fetched.
        assert len(called) == 1
        assert called == [ticker_list_json[2]["symbol"]]

    def test_resume_rejects_wrong_phase(self, db_engine, nse_tickers):
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
            run_market_cap_backfill(
                db_engine, "NSE",
                from_date=date(2025, 5, 1),
                to_date=date(2026, 5, 1),
                resume_run_id=rid,
                request_delay_s=0,
                hist_fetch_fn=lambda s, e, f, t: [],
            )


# ---------- edge cases ---------- #


class TestEdgeCases:

    def test_empty_universe(self, db_engine):
        """No active EQ tickers → SUCCESS with 0 succeeded."""
        run_id = run_market_cap_backfill(
            db_engine, "BSE",
            from_date=date(2025, 5, 1),
            to_date=date(2026, 5, 1),
            request_delay_s=0,
            hist_fetch_fn=lambda s, e, f, t: [],
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.status == "SUCCESS"
        assert r.phase == "market_cap"
        assert r.succeeded == 0

    def test_rejects_inverted_date_range(self, db_engine, nse_tickers):
        with pytest.raises(ValueError, match="from_date"):
            run_market_cap_backfill(
                db_engine, "NSE",
                from_date=date(2026, 5, 1),
                to_date=date(2025, 5, 1),  # inverted
                request_delay_s=0,
                hist_fetch_fn=lambda s, e, f, t: [],
            )

    def test_unexpected_exception_counts_as_failed(self, db_engine, nse_tickers):
        def boomy(symbol, exchange, from_d, to_d):
            if symbol == "TCS":
                raise RuntimeError("simulated bug")
            return _points_for(
                [100.0], [1_000_000], start=date(2025, 6, 1),
            )
        run_id = run_market_cap_backfill(
            db_engine, "NSE",
            from_date=date(2025, 5, 1),
            to_date=date(2026, 5, 1),
            request_delay_s=0,
            hist_fetch_fn=boomy,
        )
        with db_engine.connect() as conn:
            r = FundamentalsRunRepository(conn).get(run_id)
        assert r.succeeded == 2
        assert r.failed == 1
        assert r.status == "PARTIAL"
