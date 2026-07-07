"""Integration tests for Phase 4B-2 runners.

Tests run_rebalance, run_index_value_backfill, and
run_industry_index_build against a real Postgres fixture. The
picker and value computer are already tested separately in
test_index_compute.py; these tests focus on the orchestration
layer — sequencing, transactions, fundamentals_run lifecycle,
resume.
"""
from __future__ import annotations

from datetime import date, datetime, timezone, UTC

import pytest
from sqlalchemy import text

from trip_ema.domain import TickerRef
from trip_ema.fundamentals.runners import (
    RebalanceResult,
    _list_trading_days_in_range,
    _next_semiannual_rebalance_date,
    run_index_value_backfill,
    run_industry_index_build,
    run_rebalance,
)
from trip_ema.persistence import transaction
from trip_ema.persistence.fundamentals_repo import (
    DailyMarketCapRepository,
    FundamentalsRunRepository,
    IndustryIndexConstituentRepository,
    IndustryIndexRepository,
    IndustryIndexValueRepository,
    IndustryIndexPeriodRepository,
    InstrumentIndustryRepository,
)
from trip_ema.persistence.tickers_repo import TickersRepository
from trip_ema.fundamentals.index_compute import (
    compute_index_value_for_date,
)


# ---------- fixtures ---------- #


@pytest.fixture
def now():
    return datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def itservices_universe(db_engine, now):
    """5 IT-Services tickers + 5 Banks tickers + 1 ETF.

    All NSE EQ. Designed to satisfy min_constituents=5 for IT and
    Banks separately, and to exclude ETFs.
    """
    refs = []
    for sym in ("TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"):
        refs.append(TickerRef("NSE", sym, "EQ"))
    for sym in ("HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"):
        refs.append(TickerRef("NSE", sym, "EQ"))
    refs.append(TickerRef("NSE", "NIFTYBEES", "EQ"))  # ETF

    ids: dict[str, int] = {}
    with transaction(db_engine) as conn:
        repo = TickersRepository(conn)
        for ref in refs:
            ids[ref.symbol] = repo.insert(ref, created_at=now)
        # ETF.
        conn.execute(text(
            "UPDATE tickers SET instrument_type='ETF' WHERE ticker_id=:tid"
        ), {"tid": ids["NIFTYBEES"]})
    # Classify them.
    with transaction(db_engine) as conn:
        ic = InstrumentIndustryRepository(conn)
        for sym in ("TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"):
            ic.upsert(ids[sym], sector="Technology",
                      industry="IT - Services")
        for sym in ("HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"):
            ic.upsert(ids[sym], sector="Financials",
                      industry="Banks - Regional")
    return ids


def _set_mcap(db_engine, ticker_id: int, trade_date: date,
              close: float, shares: int):
    with transaction(db_engine) as conn:
        DailyMarketCapRepository(conn).upsert_snapshot(
            ticker_id, trade_date=trade_date,
            close_price=close, shares_used=shares,
            market_cap=close * shares, source="COMPUTED",
        )


def _mcap_for_all(db_engine, ids: dict, symbols: list[str],
                  trade_date: date, close: float = 100.0):
    """Helper to populate mcaps for a batch of symbols on one date."""
    for sym in symbols:
        _set_mcap(db_engine, ids[sym], trade_date, close, 1_000_000_000)


# ============================================================
# run_rebalance
# ============================================================


class TestRunRebalanceHappyPath:

    def test_first_rebalance_creates_constituents_and_value(
        self, db_engine, itservices_universe,
    ):
        """Brand-new index: pick top-5, no prior constituents to
        close, value at rebalance should equal base_value."""
        rd = date(2025, 10, 1)
        # Set mcaps so we have a deterministic top-5 ordering.
        for sym, close in [
            ("TCS",     4100.0),
            ("INFY",    1500.0),
            ("HCLTECH", 1200.0),
            ("WIPRO",    300.0),
            ("TECHM",    900.0),
        ]:
            _set_mcap(db_engine, itservices_universe[sym],
                      rd, close, 1_000_000_000)

        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=rd, min_constituents=5,
            )
        result = run_rebalance(
            db_engine, "IT - Services", rd,
            n=5, exchange="NSE", min_constituents=5,
        )
        assert result.rebalance_skipped is False
        assert result.constituents_picked == 5
        assert result.constituents_closed == 0  # no prior

        with db_engine.connect() as conn:
            constituents = IndustryIndexConstituentRepository(conn).list_active_for_index(
                "IT - Services"
            )
            value_row = IndustryIndexValueRepository(conn).get_at(
                "IT - Services", rd,
            )
        assert len(constituents) == 5
        # Weights sum to 1.
        assert sum(c.weight_at_rebalance for c in constituents) == pytest.approx(1.0)
        # Rebalance-day value equals base.
        assert value_row is not None
        assert value_row.value == pytest.approx(1000.0, rel=1e-9)
        assert value_row.renormalized is False

    def test_subsequent_rebalance_closes_prior_constituents(
        self, db_engine, itservices_universe,
    ):
        """Second rebalance: any prior active constituents get
        closed (REBALANCE_EXIT) and new ones inserted."""
        oct_rd = date(2025, 10, 1)
        apr_rd = date(2026, 4, 1)

        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=oct_rd, min_constituents=5,
            )
        # October rebalance.
        _mcap_for_all(
            db_engine, itservices_universe,
            ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"],
            oct_rd, close=100.0,
        )
        run_rebalance(
            db_engine, "IT - Services", oct_rd,
            n=5, exchange="NSE", min_constituents=5,
        )

        # April rebalance: changed mcaps, ordering may change but
        # the same 5 EQ IT tickers re-qualify.
        _mcap_for_all(
            db_engine, itservices_universe,
            ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"],
            apr_rd, close=200.0,
        )
        result = run_rebalance(
            db_engine, "IT - Services", apr_rd,
            n=5, exchange="NSE", min_constituents=5,
        )
        assert result.constituents_picked == 5
        assert result.constituents_closed == 5  # all prior closed

        # Confirm the old ones are now closed (effective_to = apr_rd - 1).
        with db_engine.connect() as conn:
            cr = IndustryIndexConstituentRepository(conn)
            history = cr.list_history_for_ticker(
                "IT - Services", itservices_universe["TCS"],
            )
        # Two entries: old (closed) and new (active).
        assert len(history) == 2
        old = next(h for h in history if h.rebalance_date == oct_rd)
        assert old.effective_to == date(2026, 3, 31)
        assert old.removed_reason == "REBALANCE_EXIT"

    def test_skip_when_below_min_constituents(
        self, db_engine, itservices_universe,
    ):
        """Only 3 IT tickers have mcap; min_constituents=5 → skip,
        no writes."""
        rd = date(2025, 10, 1)
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=rd, min_constituents=5,
            )
        _mcap_for_all(
            db_engine, itservices_universe,
            ["TCS", "INFY", "WIPRO"],  # only 3
            rd,
        )
        result = run_rebalance(
            db_engine, "IT - Services", rd,
            n=5, exchange="NSE", min_constituents=5,
        )
        assert result.rebalance_skipped is True
        assert result.constituents_picked == 3
        assert "min_constituents 5" in (result.skip_reason or "")

        # No DB writes.
        with db_engine.connect() as conn:
            constituents = IndustryIndexConstituentRepository(conn).list_active_for_index(
                "IT - Services"
            )
            value_row = IndustryIndexValueRepository(conn).get_at(
                "IT - Services", rd,
            )
        assert constituents == []
        assert value_row is None

    def test_unknown_index_raises(self, db_engine):
        """No industry_index row → raise ValueError."""
        with pytest.raises(ValueError, match="does not exist"):
            run_rebalance(
                db_engine, "Made Up Industry", date(2025, 10, 1),
                n=10, min_constituents=10,
            )


# ============================================================
# helpers: _next_semiannual_rebalance_date,
# _list_trading_days_in_range
# ============================================================


class TestSemiannualHelper:

    def test_finds_oct_when_starting_in_may(
        self, db_engine, itservices_universe,
    ):
        """earliest mcap in May → first semiannual = Oct 1."""
        # Put mcap rows in May.
        _set_mcap(
            db_engine, itservices_universe["TCS"],
            date(2025, 5, 22), 100.0, 1_000_000_000,
        )
        # And a mcap row on Oct 1 to confirm it's a trading day.
        _set_mcap(
            db_engine, itservices_universe["TCS"],
            date(2025, 10, 1), 100.0, 1_000_000_000,
        )
        result = _next_semiannual_rebalance_date(
            db_engine, date(2025, 5, 22),
        )
        assert result == date(2025, 10, 1)

    def test_walks_forward_if_target_is_holiday(
        self, db_engine, itservices_universe,
    ):
        """Oct 1 has no mcap (holiday); Oct 2 does → returns Oct 2."""
        _set_mcap(
            db_engine, itservices_universe["TCS"],
            date(2025, 10, 2), 100.0, 1_000_000_000,
        )
        result = _next_semiannual_rebalance_date(
            db_engine, date(2025, 5, 22),
        )
        assert result == date(2025, 10, 2)

    def test_returns_None_if_no_trading_days_found(
        self, db_engine, itservices_universe,
    ):
        """No mcap rows anywhere → returns None."""
        result = _next_semiannual_rebalance_date(
            db_engine, date(2025, 5, 22),
        )
        assert result is None


class TestTradingDaysInRange:

    def test_returns_only_dates_with_mcap_data(
        self, db_engine, itservices_universe,
    ):
        for d in (date(2025, 10, 1), date(2025, 10, 5),
                  date(2025, 10, 15)):
            _set_mcap(
                db_engine, itservices_universe["TCS"],
                d, 100.0, 1_000_000_000,
            )
        result = _list_trading_days_in_range(
            db_engine,
            from_date=date(2025, 9, 1),
            to_date=date(2025, 11, 1),
        )
        assert result == [
            date(2025, 10, 1),
            date(2025, 10, 5),
            date(2025, 10, 15),
        ]

    def test_excludes_dates_outside_range(
        self, db_engine, itservices_universe,
    ):
        for d in (date(2025, 9, 1), date(2025, 10, 1), date(2025, 11, 1)):
            _set_mcap(
                db_engine, itservices_universe["TCS"],
                d, 100.0, 1_000_000_000,
            )
        result = _list_trading_days_in_range(
            db_engine,
            from_date=date(2025, 10, 1),
            to_date=date(2025, 10, 31),
        )
        assert result == [date(2025, 10, 1)]


# ============================================================
# run_index_value_backfill
# ============================================================


class TestValueBackfillHappyPath:

    def _setup_index_with_oct_rebalance(self, db_engine, ids, base_date):
        """Set up an IT-Services index with 5 constituents, all
        with close=100 at base_date, weights equal."""
        # mcap rows on base_date for selector.
        for sym in ("TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"):
            _set_mcap(db_engine, ids[sym], base_date, 100.0, 1_000_000_000)
        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=base_date, min_constituents=5,
            )
        run_rebalance(
            db_engine, "IT - Services", base_date,
            n=5, exchange="NSE", min_constituents=5,
        )

    def test_writes_value_per_trading_day(
        self, db_engine, itservices_universe,
    ):
        rd = date(2025, 10, 1)
        self._setup_index_with_oct_rebalance(
            db_engine, itservices_universe, rd,
        )
        # mcap rows on 3 more trading days, all closes flat.
        for d in (date(2025, 10, 2), date(2025, 10, 3), date(2025, 10, 6)):
            _mcap_for_all(
                db_engine, itservices_universe,
                ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"],
                d, close=100.0,
            )

        run_id = run_index_value_backfill(
            db_engine, "IT - Services",
            from_date=rd, to_date=date(2025, 10, 6),
        )
        with db_engine.connect() as conn:
            run = FundamentalsRunRepository(conn).get(run_id)
        assert run.status == "SUCCESS"
        # 4 trading days in range (1, 2, 3, 6); all should have values.
        assert run.succeeded == 4
        assert run.skipped == 0

        with db_engine.connect() as conn:
            history = IndustryIndexValueRepository(conn).get_history(
                "IT - Services",
            )
        assert len(history) == 4
        # All values should be 1000 (closes flat).
        for h in history:
            assert h.value == pytest.approx(1000.0, rel=1e-9)

    def test_skips_pre_base_dates_silently(
        self, db_engine, itservices_universe,
    ):
        """Trading days before base_date have no defined value;
        backfill should skip them and report skipped, not failed."""
        rd = date(2025, 10, 1)
        self._setup_index_with_oct_rebalance(
            db_engine, itservices_universe, rd,
        )
        # Trading days BEFORE rebalance.
        for d in (date(2025, 9, 28), date(2025, 9, 29)):
            _mcap_for_all(
                db_engine, itservices_universe,
                ["TCS", "INFY"],
                d, close=100.0,
            )

        run_id = run_index_value_backfill(
            db_engine, "IT - Services",
            from_date=date(2025, 9, 1),
            to_date=date(2025, 10, 1),
        )
        with db_engine.connect() as conn:
            run = FundamentalsRunRepository(conn).get(run_id)
        # 1 succeeded (Oct 1 rebalance day), 2 skipped (Sep 28, 29).
        assert run.succeeded == 1
        assert run.skipped == 2
        assert run.status == "SUCCESS"

    def test_unknown_index_raises(self, db_engine):
        with pytest.raises(ValueError, match="does not exist"):
            run_index_value_backfill(
                db_engine, "Made Up Industry",
                from_date=date(2025, 10, 1),
                to_date=date(2025, 10, 31),
            )

    def test_rejects_inverted_range(self, db_engine, itservices_universe):
        rd = date(2025, 10, 1)
        self._setup_index_with_oct_rebalance(
            db_engine, itservices_universe, rd,
        )
        with pytest.raises(ValueError, match="from_date"):
            run_index_value_backfill(
                db_engine, "IT - Services",
                from_date=date(2025, 11, 1),
                to_date=date(2025, 10, 1),
            )

    def test_empty_range_succeeds_with_zero_counts(
        self, db_engine, itservices_universe,
    ):
        rd = date(2025, 10, 1)
        self._setup_index_with_oct_rebalance(
            db_engine, itservices_universe, rd,
        )
        # Date range that has NO trading days (other than rd itself,
        # but we exclude that by picking a later range with no mcap).
        run_id = run_index_value_backfill(
            db_engine, "IT - Services",
            from_date=date(2026, 1, 1),
            to_date=date(2026, 1, 31),
        )
        with db_engine.connect() as conn:
            run = FundamentalsRunRepository(conn).get(run_id)
        assert run.status == "SUCCESS"
        assert run.succeeded == 0


# ============================================================
# run_industry_index_build
# ============================================================


class TestIndustryIndexBuildHappyPath:

    def test_builds_only_qualifying_industries(
        self, db_engine, itservices_universe,
    ):
        """5 IT + 5 Banks tickers with mcap data → both industries
        qualify at min=5 and get indices. min=6 → neither qualifies."""
        rd = date(2025, 10, 1)
        # mcap data for all 10 EQ tickers on the rebalance date.
        for sym in ("TCS", "INFY", "WIPRO", "HCLTECH", "TECHM",
                    "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"):
            _set_mcap(db_engine, itservices_universe[sym],
                      rd, 100.0, 1_000_000_000)

        # min=5 → both qualify.
        report = run_industry_index_build(
            db_engine, "NSE",
            min_constituents=5, n=5,
        )
        assert {q[0] for q in report["qualified"]} == {
            "IT - Services", "Banks - Regional",
        }
        assert {i[0] for i in report["indexed"]} == {
            "IT - Services", "Banks - Regional",
        }
        # Each indexed at the auto-detected first rebalance date.
        for industry, base_date, picked in report["indexed"]:
            assert base_date == rd
            assert picked == 5

        # Confirm both indices exist.
        with db_engine.connect() as conn:
            ir = IndustryIndexRepository(conn)
            assert ir.get("IT - Services") is not None
            assert ir.get("Banks - Regional") is not None

    def test_reports_unqualified_industries(
        self, db_engine, itservices_universe,
    ):
        """min=6 → neither qualifies (each has 5)."""
        rd = date(2025, 10, 1)
        for sym in ("TCS", "INFY", "WIPRO", "HCLTECH", "TECHM",
                    "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK"):
            _set_mcap(db_engine, itservices_universe[sym],
                      rd, 100.0, 1_000_000_000)
        report = run_industry_index_build(
            db_engine, "NSE",
            min_constituents=6, n=6,
        )
        assert {q[0] for q in report["unqualified"]} == {
            "IT - Services", "Banks - Regional",
        }
        assert report["qualified"] == []
        assert report["indexed"] == []
        # No indices were created.
        with db_engine.connect() as conn:
            assert IndustryIndexRepository(conn).list_active() == []

    def test_idempotent_does_not_re_rebalance_existing_indices(
        self, db_engine, itservices_universe,
    ):
        """Second call to run_industry_index_build should not
        re-rebalance indices that already have constituents."""
        rd = date(2025, 10, 1)
        for sym in ("TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"):
            _set_mcap(db_engine, itservices_universe[sym],
                      rd, 100.0, 1_000_000_000)
        # First call: builds IT index.
        run_industry_index_build(
            db_engine, "NSE", min_constituents=5, n=5,
        )
        with db_engine.connect() as conn:
            cr = IndustryIndexConstituentRepository(conn)
            first_rebalances = cr.list_rebalances("IT - Services")
        assert first_rebalances == [rd]

        # Second call: should not re-rebalance.
        run_industry_index_build(
            db_engine, "NSE", min_constituents=5, n=5,
        )
        with db_engine.connect() as conn:
            cr = IndustryIndexConstituentRepository(conn)
            second_rebalances = cr.list_rebalances("IT - Services")
        assert second_rebalances == [rd]  # still just the one

    def test_no_mcap_data_returns_failed(
        self, db_engine, itservices_universe,
    ):
        """If there's no mcap data at all, can't determine base_date
        → all qualified industries reported as failed."""
        # Classifications exist but no mcap rows.
        report = run_industry_index_build(
            db_engine, "NSE", min_constituents=5, n=5,
        )
        # Both industries qualified (have ≥5 classified tickers)
        # but the build failed.
        assert {q[0] for q in report["qualified"]} == {
            "IT - Services", "Banks - Regional",
        }
        assert report["indexed"] == []
        assert {f[0] for f in report["failed"]} == {
            "IT - Services", "Banks - Regional",
        }


# ============================================================
# end-to-end smoke test
# ============================================================


class TestEndToEnd:
    """One realistic flow: build an index, walk a backfill date
    range, observe daily values."""

    def test_build_then_backfill_produces_value_history(
        self, db_engine, itservices_universe,
    ):
        rd = date(2025, 10, 1)
        # Establish a price series over 5 trading days. Day 1 is
        # rebalance; days 2-5 are post-rebalance with price moves.
        # All flat → values flat at 1000.
        for d in (rd, date(2025, 10, 2), date(2025, 10, 3),
                  date(2025, 10, 6), date(2025, 10, 7)):
            for sym in ("TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"):
                _set_mcap(
                    db_engine, itservices_universe[sym],
                    d, 100.0, 1_000_000_000,
                )

        # Build the index (creates IT-Services with rebalance on Oct 1).
        report = run_industry_index_build(
            db_engine, "NSE", min_constituents=5, n=5,
        )
        assert any(i[0] == "IT - Services" for i in report["indexed"])

        # Backfill values over the 5-day range.
        run_index_value_backfill(
            db_engine, "IT - Services",
            from_date=rd, to_date=date(2025, 10, 7),
        )

        with db_engine.connect() as conn:
            history = IndustryIndexValueRepository(conn).get_history(
                "IT - Services",
            )
        # We expect 5 values total.
        assert len(history) == 5
        # All flat at 1000.
        for h in history:
            assert h.value == pytest.approx(1000.0, rel=1e-9)
            assert h.constituents_used == 5
            assert h.renormalized is False


class TestChainLinkContinuity:
    """Golden test: chain-linking yields a CONTINUOUS index across rebalances.

    Asserts the numerical no-jump property — on rebalance R2 the NEW basket's
    value equals the OLD basket's value on R2. Without chain-linking the index
    would reset to base_value (1000) at R2.
    """

    def test_no_jump_across_annual_rebalance(
        self, db_engine, itservices_universe,
    ):
        r1 = date(2025, 4, 1)
        r2 = date(2026, 4, 1)
        names = ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"]

        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=r1, min_constituents=4,
            )
        _mcap_for_all(db_engine, itservices_universe, names, r1, close=100.0)
        run_rebalance(
            db_engine, "IT - Services", r1,
            n=5, exchange="NSE", min_constituents=4,
        )

        with db_engine.connect() as conn:
            lf1 = IndustryIndexPeriodRepository(conn).get_link_factor(
                "IT - Services", r1,
            )
        assert lf1 == pytest.approx(1.0)
        v_r1 = compute_index_value_for_date(db_engine, "IT - Services", r1)
        assert v_r1 is not None
        assert v_r1.value == pytest.approx(1000.0)

        # Old basket drifts to R2 (same tickers, new closes).
        _mcap_for_all(db_engine, itservices_universe, names, r2, close=130.0)
        old_on_r2 = compute_index_value_for_date(db_engine, "IT - Services", r2)
        assert old_on_r2 is not None
        assert old_on_r2.value == pytest.approx(1300.0)

        run_rebalance(
            db_engine, "IT - Services", r2,
            n=5, exchange="NSE", min_constituents=4,
        )

        # GOLDEN: new basket on R2 == old basket on R2 → no jump.
        new_on_r2 = compute_index_value_for_date(db_engine, "IT - Services", r2)
        assert new_on_r2 is not None
        assert new_on_r2.value == pytest.approx(old_on_r2.value)
        assert new_on_r2.value == pytest.approx(1300.0)

        with db_engine.connect() as conn:
            lf2 = IndustryIndexPeriodRepository(conn).get_link_factor(
                "IT - Services", r2,
            )
        assert lf2 == pytest.approx(1.3)

    def test_off_schedule_rebalance_also_chain_links(
        self, db_engine, itservices_universe,
    ):
        """A non-April (mid-year) rebalance — the delisting edge case — must
        chain-link identically. run_rebalance is date-arbitrary."""
        r1 = date(2025, 4, 1)
        r_mid = date(2025, 7, 15)
        names = ["TCS", "INFY", "WIPRO", "HCLTECH", "TECHM"]

        with transaction(db_engine) as conn:
            IndustryIndexRepository(conn).upsert(
                "IT - Services", sector_name="Technology",
                base_date=r1, min_constituents=4,
            )
        _mcap_for_all(db_engine, itservices_universe, names, r1, close=100.0)
        run_rebalance(
            db_engine, "IT - Services", r1,
            n=5, exchange="NSE", min_constituents=4,
        )
        _mcap_for_all(db_engine, itservices_universe, names, r_mid, close=110.0)
        old_mid = compute_index_value_for_date(
            db_engine, "IT - Services", r_mid,
        )
        assert old_mid.value == pytest.approx(1100.0)

        run_rebalance(
            db_engine, "IT - Services", r_mid,
            n=5, exchange="NSE", min_constituents=4,
        )
        new_mid = compute_index_value_for_date(
            db_engine, "IT - Services", r_mid,
        )
        assert new_mid.value == pytest.approx(old_mid.value)
